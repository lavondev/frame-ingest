"""Shared state handed to every stage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from frame_ingest.budget import Budget
from frame_ingest.config import AppConfig
from frame_ingest.models import (
    EventType,
    FailedBatch,
    ProcessingWarning,
    ResolvedSettings,
    StageName,
    TokenUsage,
    VideoInfo,
    utcnow,
)
from frame_ingest.pipeline.cache import StageCache, UnitCache
from frame_ingest.providers.base import ProviderBundle, UsageDelta


class StageResult(BaseModel):
    """Base class of every stage's cached result. Carries what the Appendix needs on resume."""

    warnings: list[ProcessingWarning] = Field(default_factory=list)
    failed_batches: list[FailedBatch] = Field(default_factory=list)
    usage: list[TokenUsage] = Field(default_factory=list)


def _noop(*_: Any, **__: Any) -> None:
    return None


@dataclass
class PipelineContext:
    job_id: str
    job_dir: Path
    config: AppConfig
    settings: ResolvedSettings
    video: VideoInfo
    video_path: Path
    providers: ProviderBundle
    budget: Budget | None = None
    source_url: str | None = None
    retrieved_at: datetime | None = None
    metrics: bool = False
    emit_cb: Callable[[EventType, dict[str, Any]], None] = _noop
    progress_cb: Callable[[StageName, int, int, str | None], None] = _noop
    cache: StageCache = field(init=False)
    results: dict[StageName, Any] = field(default_factory=dict)
    keys: dict[StageName, str] = field(default_factory=dict)
    started_at: datetime = field(default_factory=utcnow)
    current_stage: StageName = StageName.PROBE
    ran: list[StageName] = field(default_factory=list)
    cached: list[StageName] = field(default_factory=list)
    skipped: list[StageName] = field(default_factory=list)
    # per-stage scratch, drained by the runner into the stage result
    _warnings: list[ProcessingWarning] = field(default_factory=list)
    _failed: list[FailedBatch] = field(default_factory=list)
    _usage: dict[tuple[str, str], TokenUsage] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cache = StageCache(self.job_dir)

    @property
    def transcribe_model(self) -> str:
        """The model that will actually transcribe (the diarization model when diarize is on)."""
        return (
            self.config.models.diarize if self.settings.diarize else self.settings.transcribe_model
        )

    @property
    def segment_timestamps(self) -> bool:
        return self.providers.transcriber.caps_for(self.transcribe_model).segment_timestamps

    # -- events / progress --------------------------------------------------------------
    def emit(self, type_: EventType, data: dict[str, Any]) -> None:
        self.emit_cb(type_, data)

    def log(self, message: str, level: str = "info") -> None:
        self.emit("log", {"level": level, "stage": self.current_stage.value, "message": message})

    def progress(self, done: int, total: int, message: str | None = None) -> None:
        self.progress_cb(self.current_stage, done, total, message)

    # -- bookkeeping --------------------------------------------------------------------
    def warn(
        self,
        code: str,
        message: str,
        t_start: float | None = None,
        t_end: float | None = None,
    ) -> None:
        w = ProcessingWarning(
            stage=self.current_stage, code=code, message=message, t_start=t_start, t_end=t_end
        )
        self._warnings.append(w)
        self.emit("warning", w.model_dump(mode="json"))
        self.log(message, "warning")

    def fail_batch(self, index: int, t_start: float, t_end: float, error: str) -> None:
        self._failed.append(
            FailedBatch(
                stage=self.current_stage, index=index, t_start=t_start, t_end=t_end, error=error
            )
        )

    def add_usage(self, model: str, delta: UsageDelta) -> None:
        key = (self.current_stage.value, model)
        u = self._usage.setdefault(key, TokenUsage(stage=self.current_stage, model=model))
        u.calls += delta.calls
        u.input_tokens += delta.input_tokens
        u.output_tokens += delta.output_tokens
        u.audio_seconds += delta.audio_seconds
        if self.budget is not None:  # after recording, so a stopped run still reports its spend
            self.budget.charge(self.current_stage, model, delta)

    def drain(self) -> tuple[list[ProcessingWarning], list[FailedBatch], list[TokenUsage]]:
        out = (list(self._warnings), list(self._failed), list(self._usage.values()))
        self._warnings.clear()
        self._failed.clear()
        self._usage.clear()
        return out

    def current_warnings(self) -> list[ProcessingWarning]:
        return list(self._warnings)

    def units(self, stage: StageName | None = None) -> UnitCache:
        s = stage or self.current_stage
        return self.cache.units(s, self.keys[s])

    # -- aggregated views ---------------------------------------------------------------
    def all_warnings(self) -> list[ProcessingWarning]:
        out: list[ProcessingWarning] = []
        for r in self.results.values():
            if isinstance(r, StageResult):
                out.extend(r.warnings)
        return out

    def all_failed_batches(self) -> list[FailedBatch]:
        out: list[FailedBatch] = []
        for r in self.results.values():
            if isinstance(r, StageResult):
                out.extend(r.failed_batches)
        return out

    def all_usage(self) -> list[TokenUsage]:
        out: list[TokenUsage] = []
        for r in self.results.values():
            if isinstance(r, StageResult):
                out.extend(r.usage)
        return out
