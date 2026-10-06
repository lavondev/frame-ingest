"""Stage 6: transcript correction.

Runs AFTER vision so jargon the speech model misspelled can be fixed from what is on screen.
Overlapping windows: each call corrects a block of target segments and sees a few segments of
read-only context on either side. The model returns {id, corrected_text}; IDs are validated
exactly (no missing, extra or duplicate ids). Any window that fails validation after retries
falls back to the raw text. raw_text is never modified; a diff log records every change.
"""

from __future__ import annotations

from pydantic import BaseModel

from frame_ingest.errors import FatalProviderError, FrameIngestError
from frame_ingest.llm_schemas import CorrectionOut
from frame_ingest.models import FrameAnalysis, Segment, StageName
from frame_ingest.pipeline.cache import digest
from frame_ingest.pipeline.context import PipelineContext, StageResult
from frame_ingest.pipeline.textutil import clip
from frame_ingest.pipeline.timefmt import fmt_ts
from frame_ingest.pipeline.transcribe import TranscribeResult
from frame_ingest.pipeline.util import run_units
from frame_ingest.pipeline.vision import VisionResult
from frame_ingest.storage import atomic_write_text

NAME = StageName.CORRECT
VERSION = 1
DEPS: list[StageName] = [StageName.TRANSCRIBE, StageName.VISION]
PROMPT_VERSION = "v1"
MAX_ATTEMPTS = 3
LEN_RATIO = (0.5, 2.0)  # corrected/raw length guard: corrections must not invent or delete

SYSTEM = (
    "You correct machine-generated speech-to-text transcripts.\n"
    "Fix misrecognised words, names, product/jargon terms, numbers, casing and punctuation.\n"
    "Rules:\n"
    "- NEVER invent, add, summarise or remove content. Keep the speaker's wording.\n"
    "- Do not merge, split, reorder or move words between segments.\n"
    "- Preserve segment ids exactly. If a segment is already correct return it unchanged.\n"
    "- Treat the glossary, the file name and the on-screen text/entities (read from the video "
    "frames at that time) as authoritative spellings.\n"
    "- Return JSON {segments: [{id, corrected_text}]} containing EXACTLY the ids listed under "
    "TARGET SEGMENTS, nothing else."
)


class CorrectionResult(StageResult):
    segments: list[Segment] = []
    changed: int = 0
    diff_log: str = ""


class WindowResult(BaseModel):
    texts: dict[int, str]


def key_params(ctx: PipelineContext) -> dict[str, object]:
    return {
        "model": ctx.settings.correct_model,
        "context": ctx.settings.context,
        "window": ctx.config.correction_window,
        "ctx": ctx.config.correction_context,
        "prompt": PROMPT_VERSION,
        "effort": ctx.config.reasoning_effort,
    }


def skip_reason(ctx: PipelineContext) -> str | None:
    return None if ctx.video.has_audio else "No transcript to correct."


def validate_cached(ctx: PipelineContext, result: CorrectionResult) -> bool:
    return True


def validate_correction(
    expected: list[Segment], out: CorrectionOut
) -> tuple[dict[int, str], str | None]:
    """Return ({id: text}, error). error is set when the id set is wrong (window must retry)."""
    want = [s.id for s in expected]
    got = [c.id for c in out.segments]
    if sorted(got) != sorted(want):
        missing = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))
        dup = sorted({i for i in got if got.count(i) > 1})
        bits = []
        if missing:
            bits.append(f"missing ids {missing[:10]}")
        if extra:
            bits.append(f"unexpected ids {extra[:10]}")
        if dup:
            bits.append(f"duplicate ids {dup[:10]}")
        return {}, "; ".join(bits) or "id mismatch"
    return {c.id: " ".join(c.corrected_text.split()) for c in out.segments}, None


def accept_text(raw: str, corrected: str) -> bool:
    """Reject empty text or corrections that grow/shrink the segment implausibly."""
    if not corrected.strip():
        return False
    ratio = len(corrected) / max(1, len(raw))
    return LEN_RATIO[0] <= ratio <= LEN_RATIO[1]


def _visual_hints(scenes: list[FrameAnalysis], t0: float, t1: float, d: float) -> str:
    lines = []
    for sc in scenes:
        if t0 - 10 <= sc.t <= t1 + 10 and (sc.on_screen_text or sc.entities):
            bits = []
            if sc.on_screen_text:
                bits.append("text: " + " | ".join(sc.on_screen_text))
            if sc.entities:
                bits.append("entities: " + ", ".join(e.name for e in sc.entities))
            lines.append(f"[{fmt_ts(sc.t, d)}] " + "; ".join(bits))
    return clip("\n".join(lines), 2500)


def _global_entities(scenes: list[FrameAnalysis], limit: int = 40) -> str:
    counts: dict[str, int] = {}
    for sc in scenes:
        for e in sc.entities:
            counts[e.name] = counts.get(e.name, 0) + 1
    ranked = sorted(counts, key=lambda n: (-counts[n], n))[:limit]
    return ", ".join(ranked)


def build_prompt(
    ctx: PipelineContext,
    before: list[Segment],
    target: list[Segment],
    after: list[Segment],
    scenes: list[FrameAnalysis],
) -> str:
    d = ctx.video.duration_s

    def fmt(segs: list[Segment]) -> str:
        return "\n".join(f"[{s.id}] {s.raw_text}" for s in segs) or "(none)"

    parts = [
        f"FILE: {ctx.video.filename}",
        "GLOSSARY / CONTEXT FROM THE USER:\n" + (ctx.settings.context or "(none)"),
        "NAMES SEEN ON SCREEN ACROSS THE VIDEO: " + (_global_entities(scenes) or "(none)"),
        "ON-SCREEN TEXT AND ENTITIES NEAR THIS PASSAGE:\n"
        + (_visual_hints(scenes, target[0].start, target[-1].end, d) or "(none)"),
        "CONTEXT BEFORE (read-only, do not return):\n" + fmt(before),
        "TARGET SEGMENTS (correct these):\n" + fmt(target),
        "CONTEXT AFTER (read-only, do not return):\n" + fmt(after),
    ]
    return "\n\n".join(parts)


def render_diff(segments: list[Segment], d: float) -> tuple[str, int]:
    lines: list[str] = []
    changed = 0
    for s in segments:
        if s.corrected_text is not None and s.corrected_text.strip() != s.raw_text.strip():
            changed += 1
            lines.append(f"[{fmt_ts(s.start, d)}] #{s.id}\n- {s.raw_text}\n+ {s.corrected_text}\n")
    header = f"# Transcript corrections: {changed} of {len(segments)} segment(s) changed\n\n"
    return header + "\n".join(lines), changed


async def run(ctx: PipelineContext) -> CorrectionResult:
    tr: TranscribeResult = ctx.results[StageName.TRANSCRIBE]
    vis: VisionResult = ctx.results[StageName.VISION]
    segs = [s.model_copy() for s in tr.transcript.segments]
    if not segs:
        return CorrectionResult()

    cfg = ctx.config
    d = ctx.video.duration_s
    size, pad = cfg.correction_window, cfg.correction_context
    starts = list(range(0, len(segs), size))
    units = ctx.units()
    total = len(starts)
    done = 0
    ctx.progress(0, total, f"Correcting {len(segs)} segment(s) in {total} window(s)")

    async def one(k: int) -> None:
        nonlocal done
        lo = starts[k]
        target = segs[lo : lo + size]
        before = segs[max(0, lo - pad) : lo]
        after = segs[lo + size : lo + size + pad]
        prompt = build_prompt(ctx, before, target, after, vis.scenes)
        ident = digest(prompt, ctx.settings.correct_model)
        cached = units.get(f"window_{k:03d}", WindowResult, ident)
        texts: dict[int, str] | None = cached.texts if cached else None
        if texts is None:
            error = ""
            for _ in range(MAX_ATTEMPTS):
                try:
                    res = await ctx.providers.text.complete_json(
                        CorrectionOut,
                        system=SYSTEM,
                        user=prompt
                        + (f"\n\nPrevious reply was rejected: {error}" if error else ""),
                        stage=StageName.CORRECT,
                        model=ctx.settings.correct_model,
                    )
                except FatalProviderError:
                    raise
                except FrameIngestError as exc:
                    error = exc.message
                    continue
                ctx.add_usage(ctx.settings.correct_model, res.usage)
                parsed, err = validate_correction(target, res.value)
                if err is None:
                    texts = parsed
                    break
                error = err
            if texts is not None:
                units.put(f"window_{k:03d}", WindowResult(texts=texts), ident)
        applied: list[dict[str, object]] = []
        if texts is None:
            ctx.warn(
                "correction_fallback",
                f"Correction failed for segments {target[0].id}–{target[-1].id}; raw text kept.",
                target[0].start,
                target[-1].end,
            )
            for s in target:
                s.corrected_text = s.raw_text
        else:
            rejected = 0
            for s in target:
                new = texts[s.id]
                if accept_text(s.raw_text, new):
                    s.corrected_text = new
                else:
                    s.corrected_text = s.raw_text
                    rejected += 1
                applied.append({"id": s.id, "corrected_text": s.corrected_text})
            if rejected:
                ctx.warn(
                    "correction_rejected",
                    f"{rejected} implausible correction(s) ignored in segments "
                    f"{target[0].id}–{target[-1].id}.",
                    target[0].start,
                    target[-1].end,
                )
        if texts is not None:
            ctx.emit("transcript", {"corrected": applied})
        done += 1
        ctx.progress(done, total, f"{done}/{total} windows")

    await run_units(list(range(total)), one, cfg.api_concurrency)

    diff, changed = render_diff(segs, d)
    atomic_write_text(ctx.cache.stage_dir(StageName.CORRECT) / "corrections.diff", diff)
    ctx.log(f"{changed} of {len(segs)} segments corrected")
    return CorrectionResult(segments=segs, changed=changed, diff_log=diff)
