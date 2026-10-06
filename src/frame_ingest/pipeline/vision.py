"""Stage 5: vision analysis.

Frames go to a vision model in batches (default 8), each labelled with its timestamp and
accompanied by the transcript window for that time range. Output is a strict JSON schema; each
frame becomes scene_description / on_screen_text / change_from_previous / entities / scene_type.
Fail-soft: a batch that cannot be analysed is recorded as a gap, not a job failure — unless every
batch fails (then something systemic is wrong and the stage fails).
"""

from __future__ import annotations

from frame_ingest.errors import FatalProviderError, FrameIngestError, ProviderError
from frame_ingest.llm_schemas import VisionFrameOut
from frame_ingest.models import (
    Entity,
    FrameAnalysis,
    FrameInfo,
    SceneType,
    StageName,
)
from frame_ingest.pipeline.cache import digest
from frame_ingest.pipeline.context import PipelineContext, StageResult
from frame_ingest.pipeline.frames import FramesResult
from frame_ingest.pipeline.textutil import clip, transcript_lines, window_segments
from frame_ingest.pipeline.timefmt import fmt_ts
from frame_ingest.pipeline.transcribe import TranscribeResult
from frame_ingest.pipeline.util import run_units
from frame_ingest.providers.base import VisionBatchRequest, VisionFrame

NAME = StageName.VISION
VERSION = 1
DEPS: list[StageName] = [StageName.FRAMES, StageName.TRANSCRIBE]
PROMPT_VERSION = "v1"
MAX_ROUNDS = 2

SYSTEM = (
    "You are a precise video-frame analyst. You are shown frames sampled from a video, each "
    "preceded by a label with its file name and timestamp. Return one object per labelled frame.\n"
    "Rules:\n"
    "- Describe only what is visible. Never invent names, numbers or text you cannot see.\n"
    "- on_screen_text: transcribe visible text exactly (spelling, casing, code, numbers); one "
    "string per distinct text block; empty list if there is none.\n"
    "- entities: people, organizations, products, technologies, places or concepts clearly "
    "visible or named on screen; keep names exactly as shown.\n"
    "- change_from_previous: one sentence on what changed compared with the previous frame; for "
    "the first frame of the whole video write 'First frame'.\n"
    f"- scene_type: one of {', '.join(s.value for s in SceneType)}.\n"
    "- The `frame` field must be the exact file name from the label."
)


class VisionResult(StageResult):
    scenes: list[FrameAnalysis] = []


def make_batches(frames: list[FrameInfo], size: int) -> list[list[FrameInfo]]:
    return [frames[i : i + size] for i in range(0, len(frames), size)]


def key_params(ctx: PipelineContext) -> dict[str, object]:
    return {
        "model": ctx.settings.vision_model,
        "detail": ctx.settings.image_detail,
        "context": ctx.settings.context,
        "batch": ctx.config.vision_batch_size,
        "prompt": PROMPT_VERSION,
        "effort": ctx.config.reasoning_effort,
    }


def skip_reason(ctx: PipelineContext) -> str | None:
    return None


def validate_cached(ctx: PipelineContext, result: VisionResult) -> bool:
    return True


def _label(i: int, f: FrameInfo, duration: float) -> str:
    return f"Frame {i}: file={f.name} time={fmt_ts(f.t, duration)} ({f.t:.1f}s)"


def build_request(
    ctx: PipelineContext,
    batch: list[FrameInfo],
    prev: FrameInfo | None,
    window_text: str,
    index: int,
) -> VisionBatchRequest:
    d = ctx.video.duration_s
    fdir = ctx.job_dir / "frames"
    ctx_text = (
        f"\nUser-provided context / glossary:\n{ctx.settings.context}\n"
        if ctx.settings.context
        else ""
    )
    preamble = f"Video file: {ctx.video.filename}\nDuration: {fmt_ts(d)}\n{ctx_text}\n" + (
        "A REFERENCE FRAME (the frame just before this batch) is shown first; use it only to "
        "describe the change for the first frame; do NOT output an entry for it."
        if prev
        else "The first frame below is the first sampled frame of the whole video."
    )
    return VisionBatchRequest(
        system=SYSTEM,
        preamble=preamble,
        context_frame=(
            VisionFrame(
                name=prev.name,
                t=prev.t,
                path=fdir / prev.name,
                label=f"REFERENCE FRAME: file={prev.name} time={fmt_ts(prev.t, d)}",
            )
            if prev
            else None
        ),
        frames=[
            VisionFrame(name=f.name, t=f.t, path=fdir / f.name, label=_label(i + 1, f, d))
            for i, f in enumerate(batch)
        ],
        postamble=(
            "Transcript around these frames (machine-recognised; may contain errors):\n"
            + (window_text or "(no speech in this range)")
            + "\n\nReturn one entry per labelled frame, in order, with the exact file names."
        ),
        detail=ctx.settings.image_detail,
        model=ctx.settings.vision_model,
    )


def to_analysis(out: VisionFrameOut, info: FrameInfo) -> FrameAnalysis:
    return FrameAnalysis(
        frame=info.name,
        t=info.t,
        scene_description=out.scene_description.strip(),
        on_screen_text=[t.strip() for t in out.on_screen_text if t.strip()],
        change_from_previous=out.change_from_previous.strip(),
        entities=[Entity(name=e.name.strip(), kind=e.kind) for e in out.entities if e.name.strip()],
        scene_type=out.scene_type,
    )


async def run(ctx: PipelineContext) -> VisionResult:
    frames_res: FramesResult = ctx.results[StageName.FRAMES]
    tres: TranscribeResult = ctx.results[StageName.TRANSCRIBE]
    segments = tres.transcript.segments
    frames = frames_res.frames
    if not frames:
        ctx.warn("no_frames", "No frames available; visual analysis skipped.")
        return VisionResult()

    d = ctx.video.duration_s
    batches = make_batches(frames, ctx.config.vision_batch_size)
    units = ctx.units()
    analyses: dict[str, FrameAnalysis] = {}
    failed = 0
    total = len(batches)
    done = 0
    ctx.progress(0, total, f"Analysing {len(frames)} frame(s) in {total} batch(es)")

    async def one(idx: int) -> None:
        nonlocal failed, done
        batch = batches[idx]
        by_name = {f.name: f for f in batch}
        t0 = batch[0].t
        t1 = batches[idx + 1][0].t if idx + 1 < total else d
        window = clip(
            transcript_lines(window_segments(segments, t0, t1), corrected=False, duration=d), 6000
        )
        prev = batches[idx - 1][-1] if idx > 0 else None
        ident = digest(
            [f.name for f in batch], window, ctx.settings.context, prev.name if prev else ""
        )
        cached = units.get(f"batch_{idx:03d}", VisionResult, ident)
        if cached is not None:
            got = {a.frame: a for a in cached.scenes}
        else:
            got = {}
            missing = list(batch)
            error = ""
            for _ in range(MAX_ROUNDS):
                req = build_request(ctx, missing, prev if not got else None, window, idx)
                try:
                    res = await ctx.providers.vision.analyze(req)
                except FatalProviderError:
                    raise
                except FrameIngestError as exc:
                    error = exc.message
                    break
                ctx.add_usage(ctx.settings.vision_model, res.usage)
                for out in res.value.frames:
                    info = by_name.get(out.frame)
                    if info and out.frame not in got:
                        got[out.frame] = to_analysis(out, info)
                missing = [f for f in batch if f.name not in got]
                if not missing:
                    break
                error = f"model omitted {len(missing)} of {len(batch)} frame(s)"
            if got:
                units.put(f"batch_{idx:03d}", VisionResult(scenes=list(got.values())), ident)
            if missing:
                failed += 1 if not got else 0
                ctx.fail_batch(idx, missing[0].t, missing[-1].t, error or "no analysis returned")
                ctx.warn(
                    "vision_gap",
                    f"Vision analysis failed for {len(missing)} frame(s) "
                    f"({fmt_ts(missing[0].t, d)}–{fmt_ts(missing[-1].t, d)}): {error}",
                    missing[0].t,
                    missing[-1].t,
                )
        for name in (f.name for f in batch):
            if name in got:
                analyses[name] = got[name]
                ctx.emit("scene", got[name].model_dump(mode="json"))
        done += 1
        ctx.progress(done, total, f"{done}/{total} batches")

    await run_units(list(range(total)), one, ctx.config.api_concurrency)

    if failed == total:
        raise ProviderError(
            "Vision analysis failed for every batch. Check the vision model setting and your "
            "API access, then retry (nothing paid is repeated)."
        )
    scenes = [analyses[f.name] for f in frames if f.name in analyses]
    return VisionResult(scenes=scenes)
