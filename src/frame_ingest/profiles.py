"""Provider profiles: which providers a run uses, and what that implies for the network.

* `fake`  deterministic and offline (tests, demos).
* `cloud` OpenAI-compatible endpoints from the config (OpenAI, Groq, a self-hosted vLLM, ...).
* `local` faster-whisper in process plus an OpenAI-compatible server on this machine (Ollama).
* `agent` has no `run`: the host agent does the model work (`prepare` / `assemble`).

Every `cloud` run goes through the egress gate in `egress.py`; `local` only ever talks to
loopback and refuses a non-loopback `local.base_url`.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import SecretStr

from frame_ingest.capabilities import CapabilityMemo
from frame_ingest.config import AppConfig, ConfigError, StageModels
from frame_ingest.engine import ProviderFactory
from frame_ingest.errors import FatalProviderError, FrameIngestError
from frame_ingest.guard.netblock import is_loopback
from frame_ingest.providers.base import ProviderBundle
from frame_ingest.providers.fake import FakeText, FakeTranscriber, FakeVision

PROFILES = ("fake", "cloud", "local", "agent")
RUNNABLE = ("fake", "cloud", "local")


class ProfileUnavailable(FrameIngestError):
    code = "profile_unavailable"
    status = 501


def fake_bundle() -> ProviderBundle:
    return ProviderBundle(transcriber=FakeTranscriber(), vision=FakeVision(), text=FakeText())


@dataclass
class ResolvedProfile:
    name: str
    config: AppConfig  # the config the engine must use (models and endpoints for this profile)
    factory: ProviderFactory
    free: bool = False  # costs nothing, so cost caps do not apply


def _local_config(config: AppConfig) -> AppConfig:
    from urllib.parse import urlsplit

    loc = config.local
    if not is_loopback(urlsplit(loc.base_url).hostname):
        raise ConfigError(
            "local.base_url must point at this machine (127.0.0.1, ::1 or localhost); "
            "use --profile cloud for a remote endpoint."
        )
    return config.model_copy(
        update={
            "models": StageModels(
                transcribe=loc.whisper_model,
                vision=loc.vision_model,
                correct=loc.text_model,
                synthesize=loc.text_model,
                diarize=config.models.diarize,
            ),
            "base_urls": config.base_urls.model_copy(
                update={"transcribe": None, "vision": loc.base_url, "text": loc.base_url}
            ),
            "api_key": SecretStr("local"),  # the SDK needs a value; a local server ignores it
            "role_api_keys": {},
            "reasoning_effort": None,
        }
    )


def profile_config(profile: str, config: AppConfig) -> AppConfig:
    """The config a profile runs with (models and endpoints), without needing keys or providers."""
    return _local_config(config) if profile == "local" else config


def resolve_profile(profile: str, config: AppConfig, *, offline: bool = False) -> ResolvedProfile:
    if profile == "fake":
        return ResolvedProfile("fake", config, fake_bundle, free=True)
    if profile == "agent":
        raise ProfileUnavailable(
            "Profile 'agent' has no `run`: use `prepare`, write your outputs, then `assemble`."
        )
    memo = CapabilityMemo(config.data_root / "capabilities.json")

    if profile == "cloud":
        if not all(config.key_for(role) for role in ("transcribe", "vision", "text")):
            raise FatalProviderError(
                config.missing_key_message(), code="missing_api_key", status=400
            )

        def cloud() -> ProviderBundle:
            from frame_ingest.providers.openai_client import build_openai_providers

            return build_openai_providers(config, memo)

        return ResolvedProfile("cloud", config, cloud)

    if profile == "local":
        cfg = _local_config(config)

        def local() -> ProviderBundle:
            from frame_ingest.providers.faster_whisper import FasterWhisperTranscriber
            from frame_ingest.providers.openai_client import OpenAIText, OpenAIVision, make_client

            return ProviderBundle(
                transcriber=FasterWhisperTranscriber(
                    cfg.models.transcribe,
                    download_root=cfg.home / "models",
                    offline=offline,
                ),
                vision=OpenAIVision(make_client(cfg, "vision"), cfg, memo),
                text=OpenAIText(make_client(cfg, "text"), cfg, memo),
            )

        return ResolvedProfile("local", cfg, local, free=True)

    raise ProfileUnavailable(f"Unknown profile '{profile}'. Choose from: {', '.join(PROFILES)}.")


def provider_factory(profile: str) -> ProviderFactory:
    """Kept for callers that only need the factory (no config-dependent profiles)."""
    if profile == "fake":
        return fake_bundle
    raise ProfileUnavailable(f"Profile '{profile}' needs a configuration; use resolve_profile().")


def egress_summary(profile: str) -> dict[str, object]:
    """Static note for commands that do not compute a plan (see egress.build_plan)."""
    if profile == "agent":
        return {
            "network": False,
            "destinations": [],
            "note": (
                "The CLI sends nothing. In agent mode the frames and transcript you read are "
                "sent to whatever model backs the host agent."
            ),
        }
    if profile == "fake":
        return {"network": False, "destinations": [], "note": "Fake providers; nothing is sent."}
    return {"network": profile == "cloud", "destinations": [], "note": "See the egress plan."}
