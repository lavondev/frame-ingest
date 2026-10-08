"""The audio guarantee: a transcript whenever the video has speech, or a stop and a question.

Where the transcript comes from, in order: a caption file passed with `--captions`; captions that
came with a download (manual first, auto-generated last); speech-to-text, on this machine
(faster-whisper, the `local` extra) or, only with the user's consent, a cloud speech API.

When a video has an audio track and speech-to-text returns nothing, the track's loudness is
measured (ffmpeg `volumedetect`). If it is not near-silent, the voice-activity filter probably
dropped speech (common under music), so transcription runs again with the filter off. If there is
still nothing, or speech-to-text cannot run, the job stops with status `needs_decision` and the
options to put to the user: `--captions`, `--allow-frames-only`, or cloud speech. It never quietly
carries on from frames alone. The outcome is recorded in `agent/audio.json`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from frame_ingest.agent.captions import load_captions
from frame_ingest.agent.common import agent_context, agent_dir, video_of
from frame_ingest.capabilities import CapabilityMemo
from frame_ingest.config import AppConfig
from frame_ingest.egress import EgressItem, EgressPlan, destination
from frame_ingest.engine import Engine
from frame_ingest.errors import FatalProviderError, FrameIngestError
from frame_ingest.models import Job, StageName, Transcript
from frame_ingest.pipeline import audio as audio_stage
from frame_ingest.pipeline import transcribe as transcribe_stage
from frame_ingest.pipeline.loudness import Loudness, measure
from frame_ingest.providers.base import Transcriber
from frame_ingest.storage import atomic_write_text, read_json

STATUS_FILE = "audio.json"
EXIT_HINT = "Ask the user which option they want; do not choose for them."
Reason = Literal[
    "no_audio_track",
    "no_speech_found",
    "speech_extra_missing",
    "transcription_disabled",
]
EgressGate = Callable[[EgressPlan], None]


class Attempt(BaseModel):
    method: Literal["local", "cloud"]
    model: str
    vad_filter: bool | None = None
    segments: int


class AudioStatus(BaseModel):
    """`agent/audio.json`: how the transcript was obtained, or why there is none."""

    status: Literal["ok", "frames_only", "needs_decision"]
    audio_track: bool
    method: Literal["captions", "auto-captions", "local", "cloud", "none"] = "none"
    reason: Reason | None = None
    message: str = ""
    loudness: Loudness | None = None
    attempts: list[Attempt] = Field(default_factory=list)


def load_status(job_dir: Path) -> AudioStatus | None:
    path = agent_dir(job_dir) / STATUS_FILE
    if not path.is_file() or path.is_symlink():
        return None
    try:
        return AudioStatus.model_validate(read_json(path))
    except (ValueError, OSError):
        return None


def save_status(job_dir: Path, status: AudioStatus) -> None:
    atomic_write_text(agent_dir(job_dir) / STATUS_FILE, status.model_dump_json(indent=2))


# ── the speech-to-text paths ────────────────────────────────────────────────────
def local_speech_available() -> bool:
    from frame_ingest.providers.faster_whisper import is_available

    return is_available()


def cloud_transcriber(config: AppConfig) -> Transcriber:
    """The OpenAI-compatible transcriber from the config (tests replace this)."""
    from frame_ingest.providers.openai_client import OpenAITranscriber, make_client

    memo = CapabilityMemo(config.data_root / "capabilities.json")
    return OpenAITranscriber(make_client(config, "transcribe"), config, memo)


def speech_plan(config: AppConfig, duration_s: float) -> EgressPlan:
    """What cloud speech would send, for the consent gate (egress.enforce)."""
    minutes = round(duration_s / 60, 1)
    dest, loop = destination(config.base_url_for("transcribe"))
    kbps = config.audio_bitrate_kbps
    return EgressPlan(
        profile="cloud speech",
        items=[
            EgressItem(
                role="transcribe",
                sends=f"{minutes:g} min of audio",
                destination=dest,
                loopback=loop,
                models=[config.models.transcribe],
                audio_minutes=minutes,
                approx_mb=round(duration_s * kbps / 8 / 1024, 1),
            )
        ],
        notes=["Only the audio is sent; frames stay on this machine."],
    )


async def _speech_to_text(
    engine: Engine, job: Job, emit: Any, transcriber: Transcriber, model: str, key: str
) -> Transcript:
    """Run the audio and transcribe stages with `transcriber`. The unit cache is keyed by `key`,
    so a retry with different settings never reuses an earlier empty result."""
    ctx = agent_context(engine, job, emit)
    ctx.providers.transcriber = transcriber
    ctx.settings = job.settings.model_copy(update={"transcribe_model": model, "diarize": False})
    ctx.keys[StageName.AUDIO] = ctx.keys[StageName.TRANSCRIBE] = key
    ctx.current_stage = StageName.AUDIO
    ctx.results[StageName.AUDIO] = await audio_stage.run(ctx)
    ctx.current_stage = StageName.TRANSCRIBE
    result = await transcribe_stage.run(ctx)
    return result.transcript.model_copy(update={"source": "asr"})


async def _local(engine: Engine, job: Job, emit: Any, status: AudioStatus) -> Transcript | None:
    from frame_ingest.providers.faster_whisper import FasterWhisperTranscriber

    cfg = engine.config
    model = cfg.local.whisper_model
    whisper = FasterWhisperTranscriber(model, download_root=cfg.home / "models", offline=False)
    status.method = "local"
    transcript = await _speech_to_text(engine, job, emit, whisper, model, f"agent-asr-{model}")
    status.attempts.append(
        Attempt(method="local", model=model, vad_filter=True, segments=len(transcript.segments))
    )
    if transcript.segments:
        return transcript

    status.loudness = await measure(engine.video_path(job))
    if status.loudness is not None and status.loudness.silent(cfg.silence_threshold_db):
        return None  # a near-silent track: an empty transcript is believable
    whisper.vad_filter = False
    transcript = await _speech_to_text(
        engine, job, emit, whisper, model, f"agent-asr-{model}-novad"
    )
    status.attempts.append(
        Attempt(method="local", model=model, vad_filter=False, segments=len(transcript.segments))
    )
    return transcript if transcript.segments else None


async def _cloud(
    engine: Engine, job: Job, emit: Any, gate: EgressGate, status: AudioStatus
) -> Transcript | None:
    cfg = engine.config
    if not cfg.key_for("transcribe"):
        raise FatalProviderError(cfg.missing_key_message(), code="missing_api_key", status=400)
    gate(speech_plan(cfg, video_of(job).duration_s))  # raises EgressDenied without consent
    model = cfg.models.transcribe
    status.method = "cloud"
    transcript = await _speech_to_text(
        engine, job, emit, cloud_transcriber(cfg), model, f"agent-cloud-{model}"
    )
    status.attempts.append(Attempt(method="cloud", model=model, segments=len(transcript.segments)))
    return transcript if transcript.segments else None


def fetched_captions(job_dir: Path, duration: float) -> Transcript | None:
    """Captions that came with a downloaded video (manual first, auto-generated last)."""
    for stem, source in (("captions", "captions"), ("captions-auto", "auto-captions")):
        for suffix in (".vtt", ".srt"):
            path = job_dir / f"{stem}{suffix}"
            if path.is_file() and not path.is_symlink():
                try:
                    segs = load_captions(path, duration)
                except FrameIngestError:
                    continue
                return Transcript(
                    model=source, timestamp_precision="segment", source=source, segments=segs
                )
    return None


def _no_speech_message(status: AudioStatus, threshold_db: float) -> str:
    lo = status.loudness
    level = f" (mean level {lo.mean_db:.0f} dB)" if lo else ""
    if lo is not None and lo.silent(threshold_db):
        return f"The audio track is near-silent{level}; speech-to-text found no speech."
    retried = " even with voice-activity filtering off" if len(status.attempts) > 1 else ""
    return f"The video has an audio track{level}, but speech-to-text found no speech{retried}."


def _method_of(transcript: Transcript) -> Literal["captions", "auto-captions", "local"]:
    if transcript.source in ("captions", "auto-captions"):
        return transcript.source
    return "local"


async def resolve_transcript(
    engine: Engine,
    job: Job,
    previous: Transcript | None,
    *,
    captions: Path | None = None,
    allow_frames_only: bool = False,
    cloud_gate: EgressGate | None = None,
    emit: Any = None,
) -> tuple[Transcript, AudioStatus]:
    """The transcript for this job and how it was obtained. Never raises for "no speech"; the
    status says `needs_decision` instead (unless `allow_frames_only`)."""
    from frame_ingest.providers.faster_whisper import INSTALL_HINT

    cfg = engine.config
    video = video_of(job)
    job_dir = engine.store.dir(job.id)
    prev = load_status(job_dir)
    track = video.has_audio

    if captions is not None:
        segs = load_captions(captions, video.duration_s)
        tr = Transcript(
            model="captions", timestamp_precision="segment", source="captions", segments=segs
        )
        return tr, AudioStatus(status="ok", audio_track=track, method="captions")
    if previous is not None and previous.segments and cloud_gate is None:
        return previous, prev or AudioStatus(
            status="ok", audio_track=track, method=_method_of(previous)
        )
    accepted = prev is not None and prev.status == "frames_only"
    if previous is not None and prev is not None and accepted and cloud_gate is None:
        return previous, prev
    fetched = fetched_captions(job_dir, video.duration_s)
    if fetched is not None:
        method: Literal["captions", "auto-captions"] = (
            "auto-captions" if fetched.source == "auto-captions" else "captions"
        )
        return fetched, AudioStatus(status="ok", audio_track=track, method=method)

    none = Transcript(source="none")
    if not track:
        return none, AudioStatus(
            status="frames_only",
            audio_track=False,
            reason="no_audio_track",
            message="The video has no audio track, so there is nothing to transcribe.",
        )

    status = AudioStatus(status="needs_decision", audio_track=True)
    transcript: Transcript | None = None
    if cloud_gate is not None:
        transcript = await _cloud(engine, job, emit, cloud_gate, status)
        if transcript is None:
            status.reason = "no_speech_found"
            status.message = "Cloud speech-to-text returned no speech for this audio track."
    elif prev is not None and prev.status == "needs_decision" and prev.reason == "no_speech_found":
        status = prev  # the same attempts would give the same answer; do not repeat them
    elif not cfg.agent_transcribe:
        status.reason = "transcription_disabled"
        status.message = (
            "Local speech-to-text is switched off (agent_transcribe: false in the config)."
        )
    elif not local_speech_available():
        status.reason = "speech_extra_missing"
        status.message = f"Local speech-to-text is not installed. {INSTALL_HINT}"
    else:
        transcript = await _local(engine, job, emit, status)
        if transcript is None:
            status.reason = "no_speech_found"
            status.message = _no_speech_message(status, cfg.silence_threshold_db)

    if transcript is not None:
        status.status, status.reason, status.message = "ok", None, ""
        return transcript, status
    if allow_frames_only:
        status.status = "frames_only"
    return none, status


def frames_only_note(status: AudioStatus) -> str:
    """Why a frames-only document has no transcript, for its banner."""
    lo = status.loudness
    level = f", mean level {lo.mean_db:.0f} dB" if lo else ""
    return {
        "no_audio_track": "the video has no audio track",
        "no_speech_found": f"speech-to-text found no speech in the audio track{level}; "
        "the user chose to continue from the frames",
        "speech_extra_missing": "speech-to-text was not installed; the user chose to continue "
        "from the frames",
        "transcription_disabled": "speech-to-text is switched off in the config; the user chose "
        "to continue from the frames",
    }.get(status.reason or "", "no transcript could be made")


def decision(status: AudioStatus, config: AppConfig, duration_s: float) -> dict[str, Any]:
    """The options to put to the user when the audio could not be turned into a transcript."""
    from frame_ingest.providers.faster_whisper import INSTALL_HINT

    plan = speech_plan(config, duration_s)
    item = plan.items[0]
    has_key = bool(config.key_for("transcribe"))
    options: list[dict[str, Any]] = []
    if status.reason == "speech_extra_missing":
        options.append(
            {
                "id": "install_speech",
                "flags": [],
                "description": f"Install local speech-to-text, then run the same command again. "
                f"{INSTALL_HINT}",
            }
        )
    options += [
        {
            "id": "captions",
            "flags": ["--captions", "<file.srt|file.vtt>"],
            "description": "Use a caption or subtitle file the user has for this video.",
        },
        {
            "id": "frames_only",
            "flags": ["--allow-frames-only"],
            "description": "Continue from the frames alone. The document is marked 'Frames "
            "only' and cannot say what was said or quote anyone.",
        },
        {
            "id": "cloud_speech",
            "flags": ["--cloud-speech", "--allow-egress"],
            "available": has_key,
            "description": f"Send {item.sends} (about {item.approx_mb} MB) to {item.destination} "
            f"for speech-to-text with {config.models.transcribe}. Needs OPENAI_API_KEY in the "
            "environment"
            + ("" if has_key else " (not set)")
            + "; pass --allow-egress only after the user says yes.",
        },
    ]
    return {
        "reason": status.reason,
        "message": status.message,
        "ask_user": EXIT_HINT,
        "options": options,
    }
