"""The audio guarantee: a video with speech gets a transcript, or the run stops and asks.

Speech-to-text is a stand-in `faster_whisper` module whose behaviour each test scripts: plain
speech, speech under music (the voice-activity filter drops everything; the retry without it
works), and nothing at all. Loudness is measured for real by ffmpeg on generated media.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from frame_ingest.agent.validate_doc import validate_document
from frame_ingest.cli import main
from frame_ingest.pipeline.loudness import measure, parse_volumedetect
from frame_ingest.providers.fake import FakeTranscriber

SPEECH = [(0.5, 3.5, " Welcome to the Widjet Frobnicator. "), (7.0, 10.0, "Open the dashboard.")]


class Whisper:
    """A scripted faster_whisper.WhisperModel. `mode`: speech | music | nothing."""

    mode = "speech"
    calls: list[bool] = []  # vad_filter of every transcribe call

    def __init__(self, name: str, **_kw: Any) -> None:
        self.name = name

    def transcribe(self, _samples: Any, **kw: Any) -> tuple[Any, Any]:
        Whisper.calls.append(kw["vad_filter"])
        speak = Whisper.mode == "speech" or (Whisper.mode == "music" and not kw["vad_filter"])
        segs = (
            [types.SimpleNamespace(start=a, end=b, text=t) for a, b, t in SPEECH] if speak else []
        )
        return iter(segs), types.SimpleNamespace(language="en")


@pytest.fixture
def whisper(monkeypatch: pytest.MonkeyPatch) -> type[Whisper]:
    Whisper.mode, Whisper.calls = "speech", []
    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = Whisper  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    return Whisper


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fi-home"
    for name in list(os.environ):
        if name.startswith(("FRAME_INGEST_", "OPENAI_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FRAME_INGEST_HOME", str(home))
    return home


def cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    code = main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def manifest(data: dict[str, Any]) -> dict[str, Any]:
    return json.loads(Path(data["manifest"]).read_text(encoding="utf-8"))


def fill_and_assemble(capsys: pytest.CaptureFixture[str], data: dict[str, Any]) -> str:
    """Write minimal valid agent outputs, assemble, and return the document text."""
    m = manifest(data)
    out = Path(m["directories"]["out"])
    frames = [
        {
            "frame": f["name"],
            "scene_description": "A test pattern.",
            "on_screen_text": [],
            "change_from_previous": "",
            "entities": [],
            "scene_type": "other",
        }
        for f in m["frames"]
    ]
    (out / "vision" / "batch-01.json").write_text(json.dumps({"frames": frames}))
    tr = json.loads(Path(m["transcript"]["file"]).read_text(encoding="utf-8"))
    if tr["segments"]:
        segs = [{"id": s["id"], "corrected_text": s["raw_text"]} for s in tr["segments"]]
        (out / "corrections.json").write_text(json.dumps({"segments": segs}))
    chapter = {
        "title": "All",
        "start": 0,
        "end": m["video"]["duration_s"],
        "summary": "Everything.",
        "key_points": [],
        "quotes": [{"t": 7.0, "text": "Open the dashboard."}] if tr["segments"] else [],
        "entities": [],
        "decisions_claims": [],
        "visual_summary": "",
    }
    syn = {
        "title": "T",
        "tldr": "Short.",
        "abstract": "Abstract.",
        "glossary": [],
        "open_questions": [],
        "tags": [],
        "chapters": [chapter],
    }
    (out / "synthesis.json").write_text(json.dumps(syn))
    code, res = cli(capsys, "assemble", data["job_id"])
    assert code == 0, res
    md = Path(res["outputs"]["md"]).read_text(encoding="utf-8")
    assert validate_document(md) == []
    side = json.loads(Path(res["outputs"]["json"]).read_text(encoding="utf-8"))
    assert side["coverage"]["audio"] == ("yes" if tr["segments"] else "no")
    return md


# ── loudness ────────────────────────────────────────────────────────────────────
def test_volumedetect_output_is_parsed_including_digital_silence() -> None:
    loud = parse_volumedetect("[x] mean_volume: -18.3 dB\n[x] max_volume: -1.0 dB\n")
    assert loud is not None and (loud.mean_db, loud.max_db) == (-18.3, -1.0)
    quiet = parse_volumedetect("mean_volume: -inf dB\nmax_volume: -inf dB")
    assert quiet is not None and quiet.silent(-50) and quiet.mean_db == -120.0
    assert parse_volumedetect("no audio here") is None


async def test_loudness_is_measured_by_ffmpeg(sample_video: Path, quiet_video: Path) -> None:
    tone = await measure(sample_video)  # the autouse jail is the pytest temp root
    silence = await measure(quiet_video)
    assert tone is not None and not tone.silent(-50)
    assert silence is not None and silence.silent(-50)


# ── the five cases ──────────────────────────────────────────────────────────────
def test_speech_is_transcribed_once_and_the_document_says_audio_yes(
    home: Path, sample_video: Path, whisper: type[Whisper], capsys: pytest.CaptureFixture[str]
) -> None:
    code, data = cli(capsys, "prepare", str(sample_video))
    assert code == 0 and data["status"] == "ready" and data["coverage"]["audio"] == "yes"
    m = manifest(data)
    assert m["transcript"]["method"] == "local" and m["transcript"]["loudness"] is None
    assert whisper.calls == [True]  # one pass, with the voice filter
    md = fill_and_assemble(capsys, data)
    assert "audio: 'yes'" in md and "> **Coverage:** audio yes · transcript asr" in md
    assert "Frames only" not in md and "quotes verified 1/1" in md


def test_speech_under_music_is_found_by_the_retry_without_vad(
    home: Path, sample_video: Path, whisper: type[Whisper], capsys: pytest.CaptureFixture[str]
) -> None:
    whisper.mode = "music"
    code, data = cli(capsys, "prepare", str(sample_video))
    assert code == 0 and data["transcript_segments"] == 2, data
    m = manifest(data)
    assert whisper.calls == [True, False]
    assert [a["vad_filter"] for a in m["transcript"]["attempts"]] == [True, False]
    assert m["transcript"]["loudness"]["mean_db"] > -50
    assert "retried without voice-activity filtering" in m["transcript"]["note"]
    assert "audio: 'yes'" in fill_and_assemble(capsys, data)


def test_a_silent_track_stops_for_a_decision_and_frames_only_needs_consent(
    home: Path, quiet_video: Path, whisper: type[Whisper], capsys: pytest.CaptureFixture[str]
) -> None:
    whisper.mode = "nothing"
    code, data = cli(capsys, "prepare", str(quiet_video))
    assert code == 6 and data["ok"] is False and data["status"] == "needs_decision"
    assert whisper.calls == [True]  # near-silent: no retry
    decision = data["decision"]
    assert decision["reason"] == "no_speech_found" and "near-silent" in decision["message"]
    assert {o["id"] for o in decision["options"]} == {"captions", "frames_only", "cloud_speech"}
    assert "Ask the user" in decision["ask_user"]
    job = data["job_id"]

    code, res = cli(capsys, "assemble", job)  # nothing is built before the user decides
    assert code == 1 and res["error"]["code"] == "needs_decision"

    code, _ = cli(capsys, "prepare", "--job", job)  # asking again does not re-transcribe
    assert code == 6 and whisper.calls == [True]

    code, data = cli(capsys, "prepare", "--job", job, "--allow-frames-only")
    assert code == 0 and data["status"] == "ready" and data["coverage"]["audio"] == "no"
    md = fill_and_assemble(capsys, data)
    assert "> **Frames only.** No transcript: speech-to-text found no speech" in md
    assert "audio: 'no'" in md and "the user chose to continue from the frames" in md
    assert cli(capsys, "prepare", "--job", job)[0] == 0  # the choice is remembered


def test_a_loud_track_with_no_speech_is_retried_then_stops(
    home: Path, sample_video: Path, whisper: type[Whisper], capsys: pytest.CaptureFixture[str]
) -> None:
    whisper.mode = "nothing"
    code, data = cli(capsys, "prepare", str(sample_video))
    assert code == 6 and whisper.calls == [True, False]
    assert "even with voice-activity filtering off" in data["decision"]["message"]


def test_no_audio_track_continues_frames_only_without_asking(
    home: Path, silent_video: Path, whisper: type[Whisper], capsys: pytest.CaptureFixture[str]
) -> None:
    code, data = cli(capsys, "prepare", str(silent_video))
    assert code == 0 and data["coverage"] == {
        "audio": "no",
        "audio_track": False,
        "transcript_source": "none",
        "frames_analyzed": f"0/{data['frames']}",
        "chapters": None,
        "quotes_verified": None,
    }
    assert whisper.calls == []
    md = fill_and_assemble(capsys, data)
    assert "> **Frames only.** No transcript: the video has no audio track." in md


def test_missing_speech_extra_stops_with_the_exact_fix(
    home: Path, sample_video: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, data = cli(capsys, "prepare", str(sample_video))  # conftest hides faster_whisper
    assert code == 6 and data["decision"]["reason"] == "speech_extra_missing"
    first = data["decision"]["options"][0]
    assert first["id"] == "install_speech" and "uv sync --extra local" in first["description"]


def test_captions_settle_a_pending_decision(
    home: Path, sample_video: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, data = cli(capsys, "prepare", str(sample_video))
    assert code == 6
    srt = tmp_path / "subs.srt"
    srt.write_text("1\n00:00:07,000 --> 00:00:10,000\nOpen the dashboard.\n", encoding="utf-8")
    code, data = cli(capsys, "prepare", "--job", data["job_id"], "--captions", str(srt))
    assert code == 0 and data["transcript_source"] == "captions"
    assert "transcript captions" in fill_and_assemble(capsys, data)


# ── cloud speech is opt-in twice: a key and an explicit --allow-egress ──────────
def test_cloud_speech_needs_a_key_and_consent(
    home: Path,
    sample_video: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, data = cli(capsys, "prepare", str(sample_video))
    job = data["job_id"]
    assert cli(capsys, "prepare", "--job", job, "--allow-egress")[0] == 2  # needs --cloud-speech
    code, res = cli(capsys, "prepare", "--job", job, "--cloud-speech")
    assert code == 4 and res["error"]["code"] == "missing_api_key"

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-SECRET-1234567890")
    fake = FakeTranscriber()
    monkeypatch.setattr("frame_ingest.agent.audio.cloud_transcriber", lambda _cfg: fake)
    code, res = cli(capsys, "prepare", "--job", job, "--cloud-speech")
    assert code == 4 and res["error"]["code"] == "egress_denied" and fake.calls == []
    assert "api.openai.com" in res["error"]["message"] and "sk-test" not in res["error"]["message"]

    code, data = cli(capsys, "prepare", "--job", job, "--cloud-speech", "--allow-egress")
    assert code == 0 and data["transcript_segments"] > 0 and fake.calls
    assert manifest(data)["transcript"]["method"] == "cloud"


# ── the validator holds the line ────────────────────────────────────────────────
def test_validate_requires_coverage_and_a_truthful_banner(
    home: Path, silent_video: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, data = cli(capsys, "prepare", str(silent_video))
    md = fill_and_assemble(capsys, data)
    hidden = "\n".join(ln for ln in md.splitlines() if not ln.startswith("> **Frames only.**"))
    assert "frames_only" in {i["code"] for i in validate_document(hidden)}
    lying = md.replace("audio: 'no'", "audio: 'yes'")
    assert "frames_only" in {i["code"] for i in validate_document(lying)}
    bare = md.replace("coverage:", "coverage_was:")
    assert any("coverage" in i["message"] for i in validate_document(bare))
