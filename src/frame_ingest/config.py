"""Configuration: the ONLY module that names models.

Precedence (low -> high):
    built-in defaults  <  <home>/config.yaml  <  FRAME_INGEST_* env vars  <  per-job options

<home> is $FRAME_INGEST_HOME, or ~/.frame-ingest. It holds config.yaml, pricing.yaml, the
capability memo and the job workspaces.

Secrets (OPENAI_API_KEY, FRAME_INGEST_<ROLE>_API_KEY) come from the process environment only and
never appear in any serialised config, log line or file. No .env file is ever read: one in the
working directory may belong to an unrelated project (docs/PLAN.md, T7).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from frame_ingest import __version__
from frame_ingest.errors import FrameIngestError
from frame_ingest.models import ImageDetail, JobSettings, ResolvedSettings
from frame_ingest.providers.base import TranscriberCaps

log = logging.getLogger("frame_ingest.config")

ENV_PREFIX = "FRAME_INGEST_"
HOME_ENV = "FRAME_INGEST_HOME"
ROLES = ("transcribe", "vision", "text")  # base-url / api-key roles
VERSION = __version__


def default_home(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get(HOME_ENV) or Path.home() / ".frame-ingest").expanduser()


class ConfigError(FrameIngestError):
    code = "invalid_config"
    status = 500


class StageModels(BaseModel):
    transcribe: str = "whisper-1"
    vision: str = "gpt-6-luna"
    correct: str = "gpt-6-luna"
    synthesize: str = "gpt-6-luna"
    diarize: str = "gpt-4o-transcribe-diarize"


class BaseUrls(BaseModel):
    transcribe: str | None = None
    vision: str | None = None
    text: str | None = None


class ImageTokenHeuristics(BaseModel):
    """Per-image input-token guesses used ONLY by the estimate; real usage comes from the API."""

    low: int = 85
    high: int = 765
    auto: int = 765


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: StageModels = Field(default_factory=StageModels)
    base_urls: BaseUrls = Field(default_factory=BaseUrls)

    # LLM call behaviour
    reasoning_effort: str | None = "low"
    image_detail: ImageDetail = "auto"
    api_concurrency: int = Field(4, ge=1, le=32)
    retry_attempts: int = Field(6, ge=1, le=12)

    # frames
    frame_cap: int = Field(100, ge=1, le=500)
    scene_threshold: float = Field(0.30, gt=0, lt=1)
    min_interval_s: float = Field(10.0, ge=1)
    max_image_px: int = Field(1024, ge=128, le=4096)
    dedupe_hash_distance: int = Field(5, ge=0, le=32)

    # audio / transcription
    audio_format: str = "ogg"  # ogg (opus) | mp3
    audio_bitrate_kbps: int = Field(24, ge=8, le=128)
    chunk_minutes: float = Field(10.0, gt=0.5, le=30)
    chunk_overlap_s: float = Field(1.0, ge=0, le=10)
    short_chunk_s: float = Field(45.0, ge=10, le=120)
    max_chunk_mb: float = Field(20.0, gt=0, lt=25)

    # LLM stages
    vision_batch_size: int = Field(8, ge=1, le=20)
    correction_window: int = Field(40, ge=5, le=200)
    correction_context: int = Field(4, ge=0, le=20)
    max_chapters: int = Field(24, ge=1, le=100)
    prompt_token_budget: int = Field(120_000, ge=4_000)

    # estimate heuristics
    image_tokens: ImageTokenHeuristics = Field(default_factory=ImageTokenHeuristics)
    speech_tokens_per_minute: int = 220

    # input caps (PLAN T3/T10)
    max_file_mb: float = Field(8192, gt=0)
    max_duration_s: float = Field(6 * 3600, gt=0)
    max_pixels: int = Field(7680 * 4320, ge=1)

    # paths
    home: Path = Field(default_factory=default_home)

    # secrets: never serialised, never in repr
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    role_api_keys: dict[str, SecretStr] = Field(default_factory=dict, exclude=True, repr=False)

    @property
    def jobs_dir(self) -> Path:
        return self.home / "jobs"

    @property
    def data_root(self) -> Path:
        return self.home

    def missing_key_message(self) -> str:
        """Actionable text for the one thing the user must do."""
        return (
            "OPENAI_API_KEY is missing. Export it in the environment that runs frame-ingest "
            "(.env files are never read), or set FRAME_INGEST_<ROLE>_API_KEY per role."
        )

    def key_for(self, role: str) -> str | None:
        key = self.role_api_keys.get(role) or self.api_key
        return key.get_secret_value() if key else None

    def base_url_for(self, role: str) -> str | None:
        return getattr(self.base_urls, role, None)


# ── known transcription models ──────────────────────────────────────────────
_DEPRECATION = (
    "{m} is deprecated by OpenAI and shuts down on 2027-02-26. "
    "Set FRAME_INGEST_MODEL_TRANSCRIBE=gpt-transcribe to migrate."
)
KNOWN_TRANSCRIBERS: dict[str, TranscriberCaps] = {
    "whisper-1": TranscriberCaps(
        segment_timestamps=True,
        prompt=True,
        deprecated=_DEPRECATION.format(m="whisper-1"),
    ),
    # Segment timestamps are undocumented for gpt-transcribe: assume yes, verify at runtime
    # (the transcriber degrades to short chunks if the API rejects verbose_json).
    "gpt-transcribe": TranscriberCaps(segment_timestamps=True, keywords=True, prompt=True),
    "gpt-4o-transcribe": TranscriberCaps(
        segment_timestamps=False,
        deprecated=_DEPRECATION.format(m="gpt-4o-transcribe"),
    ),
    "gpt-4o-mini-transcribe": TranscriberCaps(
        segment_timestamps=False,
        deprecated=_DEPRECATION.format(m="gpt-4o-mini-transcribe"),
    ),
    "gpt-4o-transcribe-diarize": TranscriberCaps(
        segment_timestamps=True,
        prompt=False,
        diarization=True,
        deprecated=_DEPRECATION.format(m="gpt-4o-transcribe-diarize"),
    ),
}
UNKNOWN_TRANSCRIBER = TranscriberCaps(segment_timestamps=True, prompt=True)


def known_caps(model: str) -> TranscriberCaps:
    return KNOWN_TRANSCRIBERS.get(model, UNKNOWN_TRANSCRIBER).model_copy()


# ── loading ─────────────────────────────────────────────────────────────────
def _scalar_fields() -> list[str]:
    skip = {
        "models",
        "base_urls",
        "image_tokens",
        "api_key",
        "role_api_keys",
        "home",
    }
    return [n for n in AppConfig.model_fields if n not in skip]


def _deep_merge(base: dict[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_overlay(env: Mapping[str, str]) -> dict[str, Any]:
    over: dict[str, Any] = {}
    for name in _scalar_fields():
        val = env.get(f"{ENV_PREFIX}{name.upper()}")
        if val not in (None, ""):
            over[name] = val
    if env.get(f"{ENV_PREFIX}REASONING_EFFORT") == "":
        over["reasoning_effort"] = None
    models: dict[str, str] = {}
    for role in StageModels.model_fields:
        val = env.get(f"{ENV_PREFIX}MODEL_{role.upper()}")
        if val:
            models[role] = val
    if models:
        over["models"] = models
    urls: dict[str, str] = {}
    for role in ROLES:
        val = env.get(f"{ENV_PREFIX}{role.upper()}_BASE_URL")
        if val:
            urls[role] = val
    if urls:
        over["base_urls"] = urls
    return over


def load_config(
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> AppConfig:
    """Build the AppConfig from <home>/config.yaml and `env` (defaults to os.environ)."""
    merged_env: dict[str, str] = dict(os.environ if env is None else env)
    root = home or default_home(merged_env)

    data: dict[str, Any] = {}
    yaml_path = root / "config.yaml"
    if yaml_path.is_file():
        try:
            loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"config.yaml is not valid YAML: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError("config.yaml must contain a mapping at the top level.")
        data = _deep_merge(data, loaded)
    data = _deep_merge(data, _env_overlay(merged_env))

    # OPENAI_BASE_URL is the general fallback; role-specific settings (yaml/env) win over it.
    general = merged_env.get("OPENAI_BASE_URL")
    if general:
        urls = dict(data.get("base_urls") or {})
        for role in ROLES:
            urls.setdefault(role, general)
        data["base_urls"] = urls

    data["home"] = root
    key = merged_env.get("OPENAI_API_KEY")
    if key:
        data["api_key"] = key.strip()
    role_keys = {
        role: merged_env[f"{ENV_PREFIX}{role.upper()}_API_KEY"].strip()
        for role in ROLES
        if merged_env.get(f"{ENV_PREFIX}{role.upper()}_API_KEY")
    }
    if role_keys:
        data["role_api_keys"] = role_keys

    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"Invalid configuration — {problems}") from exc


def resolve_settings(config: AppConfig, job: JobSettings | None = None) -> ResolvedSettings:
    """Apply per-job overrides (highest precedence) over the app configuration."""
    job = job or JobSettings()
    m = job.models
    return ResolvedSettings(
        frame_cap=job.frame_cap or config.frame_cap,
        scene_threshold=job.scene_threshold or config.scene_threshold,
        min_interval_s=job.min_interval_s or config.min_interval_s,
        image_detail=job.image_detail or config.image_detail,
        context=job.context.strip(),
        language=job.language or None,
        diarize=bool(job.diarize),
        transcribe_model=m.transcribe or config.models.transcribe,
        vision_model=m.vision or config.models.vision,
        correct_model=m.correct or config.models.correct,
        synthesize_model=m.synthesize or config.models.synthesize,
    )


# ── pricing (optional; nothing hardcoded) ───────────────────────────────────
class Pricing(BaseModel):
    transcription_per_minute: dict[str, float | None] = Field(default_factory=dict)
    text_models: dict[str, dict[str, float | None]] = Field(default_factory=dict)

    @property
    def configured(self) -> bool:
        if any(v is not None for v in self.transcription_per_minute.values()):
            return True
        return any(v is not None for p in self.text_models.values() for v in p.values())


def load_pricing(home: Path | None = None) -> Pricing:
    path = (home or default_home()) / "pricing.yaml"
    if not path.is_file():
        return Pricing()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Pricing.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"pricing.yaml is invalid: {exc}") from exc
