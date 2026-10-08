"""`frame-ingest doctor`: checks ffmpeg, the API key and that the configured models exist, with
clear, human-readable messages. Never echoes any part of the key. `online=False` makes no network
calls at all (the CLI default). `deep=True` also runs two
near-free probes (a 1 s silent clip through transcription, a tiny image through structured vision
output) so capability fallbacks are learned up front."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import openai
from PIL import Image
from pydantic import BaseModel, Field

from frame_ingest.capabilities import CapabilityMemo
from frame_ingest.config import AppConfig, known_caps
from frame_ingest.errors import CapabilityChanged, FrameIngestError, redact
from frame_ingest.ffmpeg import ffmpeg_exe, ffmpeg_version, run_ffmpeg
from frame_ingest.guard.paths import job_jail
from frame_ingest.providers.base import ProviderBundle, VisionBatchRequest, VisionFrame
from frame_ingest.providers.openai_client import make_client


class HealthCheck(BaseModel):
    name: str
    ok: bool
    message: str


class ModelStatus(BaseModel):
    role: str
    id: str
    available: bool | None = None  # None = could not verify
    message: str | None = None


class DoctorReport(BaseModel):
    ok: bool
    key_present: bool
    base_url: str | None = None
    ffmpeg: dict[str, str] | None = None
    models: dict[str, ModelStatus] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    checks: list[HealthCheck] = Field(default_factory=list)
    capabilities: dict[str, bool] = Field(default_factory=dict)


def display_url(url: str | None) -> str | None:
    """Host only: strips credentials/query so a URL can never leak a secret."""
    if not url:
        return None
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}{parts.path}"


async def _check_model(config: AppConfig, role: str, key_role: str, model: str) -> ModelStatus:
    st = ModelStatus(role=role, id=model)
    try:
        client = make_client(config, key_role)
        await asyncio.wait_for(client.models.retrieve(model), timeout=15)
        st.available = True
    except openai.AuthenticationError:
        st.available = False
        st.message = "OpenAI rejected the API key (invalid or revoked). Check OPENAI_API_KEY."
    except openai.NotFoundError:
        st.available = False
        st.message = (
            f"Model '{model}' is not available for this key/endpoint. Change it in config.yaml "
            f"or with FRAME_INGEST_MODEL_{role.upper()}."
        )
    except openai.PermissionDeniedError:
        st.available = False
        st.message = f"This API key is not permitted to use '{model}'."
    except openai.APIConnectionError as exc:
        st.available = None
        st.message = f"Could not reach the API endpoint: {redact(str(exc))}"
    except (TimeoutError, openai.APIStatusError) as exc:
        st.available = None
        st.message = f"Could not verify '{model}' ({type(exc).__name__}); it may still work."
    except FrameIngestError as exc:
        st.available = False
        st.message = exc.message
    return st


async def _deep_probe(
    config: AppConfig,
    providers: ProviderBundle,
    model_t: str,
    model_v: str,
    checks: list[HealthCheck],
) -> None:
    with tempfile.TemporaryDirectory() as td, job_jail(Path(td)):
        audio = Path(td) / "probe.ogg"
        res = await run_ffmpeg(
            [
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=16000:cl=mono",
                "-t",
                "1",
                "-c:a",
                "libopus",
                str(audio),
            ],
            timeout=30,
        )
        if res.returncode == 0:
            try:
                await providers.transcriber.transcribe(
                    audio,
                    model=model_t,
                    duration_s=1.0,
                    prompt=None,
                    keywords=[],
                    language=None,
                    diarize=False,
                )
                ok = providers.transcriber.caps_for(model_t).segment_timestamps
                checks.append(
                    HealthCheck(
                        name="transcribe probe",
                        ok=True,
                        message=f"{model_t} works; segment timestamps: {'yes' if ok else 'no'}.",
                    )
                )
            except CapabilityChanged:
                checks.append(
                    HealthCheck(
                        name="transcribe probe",
                        ok=True,
                        message=f"{model_t} works but returns no segment timestamps; "
                        "short-chunk mode will be used.",
                    )
                )
            except FrameIngestError as exc:
                checks.append(HealthCheck(name="transcribe probe", ok=False, message=exc.message))
        img = Path(td) / "probe.jpg"
        Image.new("RGB", (64, 64), (200, 40, 40)).save(img, "JPEG")
        try:
            await providers.vision.analyze(
                VisionBatchRequest(
                    system="Describe the frame. Return JSON.",
                    preamble="Health probe.",
                    frames=[
                        VisionFrame(
                            name="frame_0000.00.jpg",
                            t=0,
                            path=img,
                            label="Frame 1: file=frame_0000.00.jpg",
                        )
                    ],
                    detail="low",
                    model=model_v,
                )
            )
            checks.append(
                HealthCheck(
                    name="vision probe",
                    ok=True,
                    message=f"{model_v} accepts images with structured output.",
                )
            )
        except FrameIngestError as exc:
            checks.append(HealthCheck(name="vision probe", ok=False, message=exc.message))


async def run_doctor(
    config: AppConfig,
    memo: CapabilityMemo,
    *,
    deep: bool = False,
    online: bool = True,
    providers: ProviderBundle | None = None,
) -> DoctorReport:
    checks: list[HealthCheck] = []
    key_present = bool(
        config.key_for("transcribe") or config.key_for("vision") or config.key_for("text")
    )
    health = DoctorReport(
        ok=True,
        key_present=key_present,
        base_url=display_url(config.base_url_for("text") or config.base_url_for("vision")),
    )

    try:
        health.ffmpeg = {"path": ffmpeg_exe(), "version": await ffmpeg_version()}
        checks.append(HealthCheck(name="ffmpeg", ok=True, message="Bundled ffmpeg is available."))
    except FrameIngestError as exc:
        checks.append(HealthCheck(name="ffmpeg", ok=False, message=exc.message))

    models = {
        "transcribe": ("transcribe", config.models.transcribe),
        "vision": ("vision", config.models.vision),
        "correct": ("text", config.models.correct),
        "synthesize": ("text", config.models.synthesize),
    }
    if not online:
        # Offline: nothing leaves the machine, so the key is only reported as present or not.
        note = (
            "An API key is set (not verified offline)."
            if key_present
            else "No API key set; only the cloud profile needs one."
        )
        checks.append(HealthCheck(name="api key", ok=True, message=note))
        for role, (_, mid) in models.items():
            health.models[role] = ModelStatus(
                role=role, id=mid, available=None, message="Not checked offline."
            )
    elif not key_present:
        checks.append(
            HealthCheck(
                name="api key",
                ok=False,
                message=config.missing_key_message(),
            )
        )
        for role, (_, mid) in models.items():
            health.models[role] = ModelStatus(role=role, id=mid, available=None)
    else:
        unique: dict[tuple[str, str], list[str]] = {}
        for role, (key_role, mid) in models.items():
            unique.setdefault((key_role, mid), []).append(role)
        results = await asyncio.gather(
            *[_check_model(config, roles[0], kr, mid) for (kr, mid), roles in unique.items()]
        )
        for ((_, _), roles), st in zip(unique.items(), results, strict=True):
            for role in roles:
                health.models[role] = st.model_copy(update={"role": role})
        bad = [m for m in health.models.values() if m.available is False]
        if bad:
            for m in {(m.id, m.message): m for m in bad}.values():
                checks.append(
                    HealthCheck(name=f"model {m.id}", ok=False, message=m.message or "unavailable")
                )
        else:
            checks.append(
                HealthCheck(
                    name="api key & models",
                    ok=True,
                    message="API key accepted; configured models are available.",
                )
            )
        unverified = [m for m in health.models.values() if m.available is None and m.message]
        for m in unverified[:1]:
            checks.append(HealthCheck(name="api reachability", ok=False, message=m.message or ""))
        if deep and not bad and providers is not None:
            await _deep_probe(
                config, providers, config.models.transcribe, config.models.vision, checks
            )

    dep = known_caps(config.models.transcribe).deprecated
    if dep:
        health.warnings.append(dep)
    health.capabilities = memo.snapshot()
    health.checks = checks
    health.ok = all(c.ok for c in checks)
    return health


async def check_sandbox() -> HealthCheck:
    from frame_ingest.guard import sandbox

    mode, kind = sandbox.mode(), await sandbox.backend()
    if kind:
        return HealthCheck(name="sandbox", ok=True, message=f"{kind} works (mode: {mode}).")
    if mode == "require":
        return HealthCheck(
            name="sandbox",
            ok=False,
            message="sandbox: require is set but no working sandbox was found "
            "(macOS: sandbox-exec; Linux: bubblewrap with user namespaces).",
        )
    note = "off by config" if mode == "off" else "none available; ffmpeg runs unsandboxed"
    return HealthCheck(name="sandbox", ok=True, message=f"{note} (mode: {mode}).")


async def check_ytdlp() -> HealthCheck:
    """yt-dlp is only needed for page URLs; if present it must meet the security floor."""
    from frame_ingest.fetch.ytdlp import (
        INSTALL_HINT,
        YtdlpUnavailable,
        check_version,
        default_prefix,
    )

    try:
        prefix = default_prefix()
    except YtdlpUnavailable:
        return HealthCheck(
            name="yt-dlp",
            ok=True,
            message=f"not installed (only needed for page URLs). {INSTALL_HINT}",
        )
    try:
        return HealthCheck(
            name="yt-dlp", ok=True, message=f"{await check_version(prefix)} (meets the floor)."
        )
    except FrameIngestError as exc:
        return HealthCheck(name="yt-dlp", ok=False, message=exc.message)


def check_speech(config: AppConfig) -> str | None:
    """A warning when agent mode cannot transcribe on this machine (the `local` extra)."""
    from frame_ingest.providers.faster_whisper import INSTALL_HINT, is_available

    if not config.agent_transcribe:
        return "local speech-to-text is switched off (agent_transcribe: false)."
    if not is_available():
        return f"local speech-to-text (faster-whisper) is not installed. {INSTALL_HINT}"
    return None


async def check_local(config: AppConfig) -> list[HealthCheck]:
    """Readiness of the `local` profile. Only ever talks to loopback."""
    from urllib.parse import urlsplit

    from frame_ingest.guard.netblock import is_loopback
    from frame_ingest.providers.faster_whisper import INSTALL_HINT, is_available

    loc = config.local
    checks = [
        HealthCheck(
            name="faster-whisper",
            ok=is_available(),
            message="installed." if is_available() else f"not installed. {INSTALL_HINT}",
        )
    ]
    if not is_loopback(urlsplit(loc.base_url).hostname):
        checks.append(
            HealthCheck(
                name="local server",
                ok=False,
                message="local.base_url must point at this machine (127.0.0.1, ::1, localhost).",
            )
        )
        return checks
    try:
        client = openai.AsyncOpenAI(
            api_key="local", base_url=loc.base_url, max_retries=0, timeout=5
        )
        listed = await asyncio.wait_for(client.models.list(), timeout=8)
        have = {m.id for m in listed.data}
    except (openai.OpenAIError, TimeoutError) as exc:
        checks.append(
            HealthCheck(
                name="local server",
                ok=False,
                message=f"Could not reach {display_url(loc.base_url)} ({type(exc).__name__}). "
                "Start your local server, for example: ollama serve",
            )
        )
        return checks
    checks.append(
        HealthCheck(name="local server", ok=True, message=f"{display_url(loc.base_url)} responds.")
    )
    for role, name in (("vision", loc.vision_model), ("text", loc.text_model)):
        ok = name in have or f"{name}:latest" in have
        checks.append(
            HealthCheck(
                name=f"local {role} model",
                ok=ok,
                message=f"'{name}' is available."
                if ok
                else f"'{name}' is not installed. Run: ollama pull {name}",
            )
        )
    return checks


def report_dict(report: DoctorReport) -> dict[str, Any]:
    return report.model_dump(mode="json")
