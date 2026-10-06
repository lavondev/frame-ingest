"""Stage orchestration: ordering, cache keys, resume, skip handling, capability re-planning."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from frame_ingest.errors import CapabilityChanged, FrameIngestError, redact
from frame_ingest.models import STAGE_ORDER, StageName
from frame_ingest.pipeline import (
    assemble,
    audio,
    correct,
    frames,
    probe,
    synthesize,
    transcribe,
    vision,
)
from frame_ingest.pipeline.cache import digest
from frame_ingest.pipeline.context import PipelineContext, StageResult

log = logging.getLogger("frame_ingest.runner")


@dataclass(frozen=True)
class StageSpec:
    name: StageName
    version: int
    deps: list[StageName]
    result_type: type[StageResult]
    key_params: Callable[[PipelineContext], dict[str, object]]
    skip_reason: Callable[[PipelineContext], str | None]
    validate_cached: Callable[[PipelineContext, Any], bool]
    run: Callable[[PipelineContext], Awaitable[Any]]


def _spec(mod: Any, result_type: type[StageResult]) -> StageSpec:
    return StageSpec(
        mod.NAME,
        mod.VERSION,
        mod.DEPS,
        result_type,
        mod.key_params,
        mod.skip_reason,
        getattr(mod, "validate_cached", lambda _c, _r: True),
        mod.run,
    )


SPECS: list[StageSpec] = [
    _spec(probe, probe.ProbeResult),
    _spec(audio, audio.AudioResult),
    _spec(transcribe, transcribe.TranscribeResult),
    _spec(frames, frames.FramesResult),
    _spec(vision, vision.VisionResult),
    _spec(correct, correct.CorrectionResult),
    _spec(synthesize, synthesize.SynthesisResult),
    _spec(assemble, assemble.AssembleResult),
]
if [s.name for s in SPECS] != STAGE_ORDER:  # pragma: no cover - import-time invariant
    raise RuntimeError("SPECS must list every stage in STAGE_ORDER")


class StageFailed(Exception):
    def __init__(self, stage: StageName, code: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.message = message


class StageHooks:
    """Callbacks the job manager implements to track per-stage state."""

    def started(self, stage: StageName) -> None: ...

    def finished(
        self, stage: StageName, *, cache_hit: bool, skipped: bool, message: str | None
    ) -> None: ...

    def failed(self, stage: StageName, code: str, message: str) -> None: ...


def stage_key(ctx: PipelineContext, spec: StageSpec) -> str:
    return digest(
        ctx.video.sha256,
        spec.name.value,
        spec.version,
        spec.key_params(ctx),
        [ctx.keys[d] for d in spec.deps],
    )


def compute_keys(ctx: PipelineContext) -> None:
    for spec in SPECS:
        ctx.keys[spec.name] = stage_key(ctx, spec)


async def run_pipeline(
    ctx: PipelineContext,
    hooks: StageHooks,
    *,
    force: set[StageName] | None = None,
) -> None:
    """Run (or reuse) every stage in order. Raises StageFailed on the first failing stage."""
    force = force or set()
    replans = 0
    i = 0
    while i < len(SPECS):
        spec = SPECS[i]
        ctx.current_stage = spec.name
        base = stage_key(ctx, spec)
        ctx.keys[spec.name] = base  # unit caches inside the stage are keyed by this
        if spec.name in force:
            ctx.cache.invalidate(spec.name)

        cached = ctx.cache.load(spec.name, base, spec.result_type)
        if cached is not None and spec.validate_cached(ctx, cached):
            ctx.results[spec.name] = cached
            ctx.keys[spec.name] = digest(base, ctx.cache.out_hash(spec.name))
            skip = spec.skip_reason(ctx)
            (ctx.skipped if skip else ctx.cached).append(spec.name)
            hooks.finished(spec.name, cache_hit=True, skipped=skip is not None, message=skip)
            i += 1
            continue

        skip = spec.skip_reason(ctx)
        hooks.started(spec.name)
        ctx.drain()  # discard leftovers from a previous attempt
        try:
            result = await spec.run(ctx)
        except CapabilityChanged as exc:
            replans += 1
            if replans > 2 or spec.name not in (StageName.TRANSCRIBE, StageName.AUDIO):
                hooks.failed(spec.name, "pipeline_failed", f"Capability changed: {exc.capability}")
                raise StageFailed(spec.name, "pipeline_failed", str(exc)) from exc
            ctx.log(
                f"The transcription model does not return {exc.capability.replace('_', ' ')}; "
                "re-planning with short chunks.",
                "warning",
            )
            ctx.cache.invalidate(StageName.AUDIO)
            ctx.cache.invalidate(StageName.TRANSCRIBE)
            i = [s.name for s in SPECS].index(StageName.AUDIO)
            continue
        except FrameIngestError as exc:
            msg = redact(exc.message, ctx.config.key_for("transcribe"), ctx.config.key_for("text"))
            hooks.failed(spec.name, exc.code, msg)
            raise StageFailed(spec.name, exc.code, msg) from exc
        except Exception as exc:
            log.exception("Unexpected error in stage %s", spec.name.value)
            msg = redact(
                f"Unexpected error in the {spec.name.value} stage: {type(exc).__name__}: {exc}",
                ctx.config.key_for("transcribe"),
                ctx.config.key_for("text"),
            )
            hooks.failed(spec.name, "pipeline_failed", msg)
            raise StageFailed(spec.name, "pipeline_failed", msg) from exc

        warnings, failed, usage = ctx.drain()
        result.warnings, result.failed_batches, result.usage = warnings, failed, usage
        out = ctx.cache.save(spec.name, base, result)
        ctx.keys[spec.name] = digest(base, out)
        ctx.results[spec.name] = result
        (ctx.skipped if skip else ctx.ran).append(spec.name)
        hooks.finished(spec.name, cache_hit=False, skipped=skip is not None, message=skip)
        i += 1
