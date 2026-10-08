"""Stage 7: synthesis (map-reduce).

1. Propose chapters from the corrected transcript + scene data, then validate them:
   contiguous, covering [0, duration], real timestamps. Invalid proposals are retried with
   feedback, repaired deterministically (the chapters' start times define the partition), and as
   a last resort replaced with an even split.
2. Map: one call per chapter (summary, key points, verbatim quotes, entities, decisions/claims).
   Quotes are verified against the transcript; anything not verbatim is dropped.
3. Reduce: one call for the whole video (title, TL;DR, abstract, glossary, open questions, tags).
Chapter structure, visuals, on-screen text and the entity index are derived deterministically.
"""

from __future__ import annotations

import math
import re

from pydantic import BaseModel

from frame_ingest.errors import FatalProviderError, FrameIngestError
from frame_ingest.llm_schemas import (
    ChapterDetailOut,
    ChapterProposalOut,
    GlobalSynthesisOut,
)
from frame_ingest.models import (
    Chapter,
    Entity,
    FrameAnalysis,
    GlossaryEntry,
    Quote,
    Segment,
    StageName,
    VideoSynthesis,
)
from frame_ingest.pipeline.cache import digest
from frame_ingest.pipeline.context import PipelineContext, StageResult
from frame_ingest.pipeline.correct import CorrectionResult
from frame_ingest.pipeline.textutil import (
    clip,
    dedupe_keep_order,
    normalize,
    scene_lines,
    transcript_lines,
)
from frame_ingest.pipeline.timefmt import fmt_range
from frame_ingest.pipeline.util import approx_tokens, run_units
from frame_ingest.pipeline.vision import VisionResult
from frame_ingest.storage import file_stem

NAME = StageName.SYNTHESIZE
VERSION = 1
DEPS: list[StageName] = [StageName.CORRECT, StageName.VISION]
PROMPT_VERSION = "v1"
TOL = 0.5
MIN_CHAPTER_S = 1.0
MAX_PROPOSAL_TRIES = 3

Bound = tuple[str, float, float]


class SynthesisResult(StageResult):
    chapters: list[Chapter] = []
    synthesis: VideoSynthesis = VideoSynthesis()
    quotes_dropped: int = 0  # proposed quotes that were not verbatim in the transcript


class ProposalUnit(BaseModel):
    bounds: list[tuple[str, float, float]]


# ── chapter validation / repair (pure) ──────────────────────────────────────
def chapter_problems(
    bounds: list[Bound], duration: float, tol: float = TOL
) -> list[tuple[str, str]]:
    """Problems with a chapter list as (JSON path under the chapter list, message)."""
    if not bounds:
        return [("chapters", "no chapters were proposed")]
    problems: list[tuple[str, str]] = []
    for i, (title, s, e) in enumerate(bounds, 1):
        at = f"chapters[{i - 1}]"
        if not (math.isfinite(s) and math.isfinite(e)):
            problems.append((at, f"chapter {i} has a non-numeric timestamp"))
            continue
        if s < -tol or e > duration + tol:
            problems.append(
                (
                    at,
                    f"chapter {i} ('{title}') [{s:.1f}-{e:.1f}] exceeds the video "
                    f"(0-{duration:.1f})",
                )
            )
        if e - s < MIN_CHAPTER_S - tol:
            problems.append((at, f"chapter {i} ('{title}') is shorter than {MIN_CHAPTER_S:.0f}s"))
    if abs(bounds[0][1]) > tol:
        problems.append(
            (
                "chapters[0].start",
                f"the first chapter must start at 0 (starts at {bounds[0][1]:.1f})",
            )
        )
    if abs(bounds[-1][2] - duration) > tol:
        problems.append(
            (
                f"chapters[{len(bounds) - 1}].end",
                f"the last chapter must end at {duration:.1f} (ends at {bounds[-1][2]:.1f})",
            )
        )
    for i in range(len(bounds) - 1):
        gap = bounds[i + 1][1] - bounds[i][2]
        if abs(gap) > tol:
            word = "gap" if gap > 0 else "overlap"
            problems.append(
                (
                    f"chapters[{i + 1}].start",
                    f"{word} of {abs(gap):.1f}s between chapters {i + 1} and {i + 2}",
                )
            )
    return problems


def validate_chapters(bounds: list[Bound], duration: float, tol: float = TOL) -> list[str]:
    """Problems with a chapter list; empty list means valid."""
    return [message for _, message in chapter_problems(bounds, duration, tol)]


def repair_chapters(
    bounds: list[Bound], duration: float, min_len: float = MIN_CHAPTER_S
) -> list[Bound]:
    """Deterministic repair: the sorted, clamped *start* times define a contiguous partition of
    [0, duration]. Chapters starting within min_len of the previous one are merged into it.
    Raises ValueError when nothing usable remains."""
    usable = [
        (title, min(max(s, 0.0), duration))
        for title, s, _e in bounds
        if math.isfinite(s) and s < duration
    ]
    if not usable:
        raise ValueError("no chapter has a usable start time")
    usable.sort(key=lambda x: x[1])
    usable[0] = (usable[0][0], 0.0)
    merged: list[tuple[str, float]] = [usable[0]]
    for title, s in usable[1:]:
        if s - merged[-1][1] >= min_len and duration - s >= min_len:
            merged.append((title, s))
    return [
        (title, s, merged[i + 1][1] if i + 1 < len(merged) else duration)
        for i, (title, s) in enumerate(merged)
    ]


def fallback_chapters(duration: float, max_chapters: int, target_s: float = 300.0) -> list[Bound]:
    n = max(1, min(max_chapters, round(duration / target_s)))
    step = duration / n
    return [
        (f"Part {i + 1}", i * step, duration if i == n - 1 else (i + 1) * step) for i in range(n)
    ]


def chapter_target_range(duration: float, max_chapters: int) -> tuple[int, int]:
    lo = max(1, round(duration / 600))
    hi = min(max_chapters, max(2, round(duration / 120)))
    return lo, max(lo, hi)


# ── per-chapter helpers (pure) ──────────────────────────────────────────────
def segments_in(segments: list[Segment], start: float, end: float, last: bool) -> list[Segment]:
    return [s for s in segments if start <= s.start < end or (last and s.start == end)]


def frames_in(
    scenes: list[FrameAnalysis], start: float, end: float, last: bool
) -> list[FrameAnalysis]:
    return [s for s in scenes if start <= s.t < end or (last and s.t == end)]


def verify_quotes(
    quotes: list[Quote], segments: list[Segment], start: float, end: float, limit: int = 5
) -> tuple[list[Quote], int]:
    """Keep only quotes that appear verbatim (modulo case/punctuation) in the transcript; snap
    each timestamp to the segment that contains it. Returns (kept, dropped)."""
    norm_segs = [(normalize(s.text), s) for s in segments]
    kept: list[Quote] = []
    seen: set[str] = set()
    dropped = 0
    for q in quotes:
        nq = normalize(q.text)
        if not nq or nq in seen:
            dropped += 0 if nq in seen else 1
            continue
        hit = next((s for n, s in norm_segs if nq in n), None)
        if hit is None:  # quote may span consecutive segments
            joined = " ".join(n for n, _ in norm_segs)
            if nq in joined:
                idx = joined.index(nq)
                acc = 0
                for n, s in norm_segs:
                    if acc + len(n) >= idx:
                        hit = s
                        break
                    acc += len(n) + 1
        if hit is None:
            dropped += 1
            continue
        seen.add(nq)
        kept.append(Quote(t=min(max(hit.start, start), end), text=q.text.strip()))
        if len(kept) >= limit:
            break
    return kept, dropped


def merge_entities(*groups: list[Entity]) -> list[Entity]:
    seen: set[str] = set()
    out: list[Entity] = []
    for g in groups:
        for e in g:
            key = normalize(e.name)
            if key and key not in seen:
                seen.add(key)
                out.append(e)
    return out


_TAG_RE = re.compile(r"[^a-z0-9]+")


def clean_tags(tags: list[str], limit: int = 12) -> list[str]:
    out: list[str] = []
    for t in tags:
        tag = _TAG_RE.sub("-", t.lower()).strip("-")
        if tag and tag not in out:
            out.append(tag)
    return out[:limit]


# ── prompts ─────────────────────────────────────────────────────────────────
PROPOSE_SYSTEM = (
    "You divide a video into chapters. You are given a time-stamped transcript and descriptions "
    "of sampled video frames. Propose chapters that follow the topic and visual structure.\n"
    "Rules:\n"
    "- Chapters must be contiguous and cover the whole range: the first starts at the range start, "
    "each starts exactly where the previous ends, the last ends at the range end.\n"
    "- Use only real timestamps in seconds taken from the material; never exceed the range.\n"
    "- Titles are short, specific and descriptive (no numbering, no timestamps).\n"
    "- Respect the requested number of chapters unless the content clearly demands otherwise."
)
DETAIL_SYSTEM = (
    "You summarise one chapter of a video from its transcript and frame descriptions.\n"
    "Rules:\n"
    "- Use only information in the material; do not invent facts.\n"
    "- summary: 2-4 sentences. key_points: 3-8 concise bullet strings.\n"
    "- quotes: up to 4 notable VERBATIM sentences copied exactly from the transcript, with the "
    "timestamp (seconds) of the line they appear in. Do not paraphrase.\n"
    "- entities: people, organizations, products, technologies, places, concepts discussed.\n"
    "- decisions_claims: decisions made or factual claims asserted (empty if none).\n"
    "- visual_summary: 1-3 sentences on what is shown on screen (empty if no frames)."
)
GLOBAL_SYSTEM = (
    "You write the top-level summary of a video from its chapter summaries.\n"
    "Rules:\n"
    "- Use only the provided material; do not invent facts.\n"
    "- title: a clear descriptive title. tldr: 1-3 sentences. abstract: one paragraph "
    "(120-200 words).\n"
    "- glossary: domain terms or jargon that a reader needs, each with a short definition "
    "grounded in the material; first_seen_s is the timestamp in seconds when it first appears, "
    "or null.\n"
    "- open_questions: unresolved questions, ambiguities or things the video leaves unclear.\n"
    "- tags: 3-10 short lowercase topical tags."
)


def _material(ctx: PipelineContext, segs: list[Segment], scenes: list[FrameAnalysis]) -> str:
    d = ctx.video.duration_s
    t = (
        transcript_lines(segs, corrected=True, duration=d)
        if segs
        else "(no transcript: this video has no speech or audio)"
    )
    v = scene_lines(scenes, d) or "(no frame descriptions)"
    return f"TRANSCRIPT:\n{t}\n\nFRAME DESCRIPTIONS:\n{v}"


def _propose_windows(
    ctx: PipelineContext, segs: list[Segment], scenes: list[FrameAnalysis]
) -> list[tuple[float, float]]:
    """Split the timeline only if the material would not fit one prompt."""
    d = ctx.video.duration_s
    total = approx_tokens(_material(ctx, segs, scenes))
    budget = ctx.config.prompt_token_budget
    if total <= budget:
        return [(0.0, d)]
    n = math.ceil(total / (budget * 0.8))
    step = d / n
    return [(i * step, d if i == n - 1 else (i + 1) * step) for i in range(n)]


async def _propose(
    ctx: PipelineContext,
    segs: list[Segment],
    scenes: list[FrameAnalysis],
    w0: float,
    w1: float,
    widx: int,
) -> list[Bound]:
    d = ctx.video.duration_s
    units = ctx.units()
    wsegs = [s for s in segs if w0 <= s.start < w1 or (w1 >= d and s.start == w1)]
    wscenes = [s for s in scenes if w0 <= s.t < w1]
    lo, hi = chapter_target_range(w1 - w0, ctx.config.max_chapters)
    base = (
        f"VIDEO_FILE: {ctx.video.filename}\nVIDEO_DURATION_SECONDS: {d:.2f}\n"
        f"WINDOW_RANGE: {w0:.2f}-{w1:.2f}\n"
        f"TARGET_CHAPTER_COUNT: {lo}-{hi}\n"
        f"USER_CONTEXT: {ctx.settings.context or '(none)'}\n\n" + _material(ctx, wsegs, wscenes)
    )
    ident = digest(base, ctx.settings.synthesize_model, PROMPT_VERSION)
    cached = units.get(f"proposal_{widx:02d}", ProposalUnit, ident)
    if cached:
        return list(cached.bounds)

    error = ""
    last: list[Bound] = []
    for _ in range(MAX_PROPOSAL_TRIES):
        user = base + (
            f"\n\nYour previous proposal was invalid: {error}\nFix these problems." if error else ""
        )
        try:
            res = await ctx.providers.text.complete_json(
                ChapterProposalOut,
                system=PROPOSE_SYSTEM,
                user=user,
                stage=StageName.SYNTHESIZE,
                model=ctx.settings.synthesize_model,
            )
        except FatalProviderError:
            raise
        except FrameIngestError as exc:
            error = exc.message
            continue
        ctx.add_usage(ctx.settings.synthesize_model, res.usage)
        bounds: list[Bound] = [
            (c.title.strip() or "Untitled", c.start, c.end) for c in res.value.chapters
        ]
        probs = _validate_window(bounds, w0, w1)
        last = bounds
        if not probs:
            units.put(f"proposal_{widx:02d}", ProposalUnit(bounds=bounds), ident)
            return bounds
        error = "; ".join(probs[:6])
        major = any(("exceeds" in p or "no chapters" in p) for p in probs)
        if not major:
            break  # minor (gaps/overlaps): deterministic repair is cheaper than another call
    if last:
        ctx.warn(
            "chapters_repaired",
            f"Chapter proposal needed repair: {error}",
            w0,
            w1,
        )
        try:
            repaired = _repair_window(last, w0, w1)
            units.put(f"proposal_{widx:02d}", ProposalUnit(bounds=repaired), ident)
            return repaired
        except ValueError:
            pass
    ctx.warn(
        "chapters_fallback",
        "Could not get valid chapters from the model; using an even split.",
        w0,
        w1,
    )
    fb = [(t, s + w0, e + w0) for t, s, e in fallback_chapters(w1 - w0, ctx.config.max_chapters)]
    return fb


def _validate_window(bounds: list[Bound], w0: float, w1: float) -> list[str]:
    shifted = [(t, s - w0, e - w0) for t, s, e in bounds]
    return validate_chapters(shifted, w1 - w0)


def _repair_window(bounds: list[Bound], w0: float, w1: float) -> list[Bound]:
    shifted = [(t, s - w0, e - w0) for t, s, e in bounds]
    return [(t, s + w0, e + w0) for t, s, e in repair_chapters(shifted, w1 - w0)]


def key_params(ctx: PipelineContext) -> dict[str, object]:
    return {
        "model": ctx.settings.synthesize_model,
        "context": ctx.settings.context,
        "max_chapters": ctx.config.max_chapters,
        "budget": ctx.config.prompt_token_budget,
        "prompt": PROMPT_VERSION,
        "effort": ctx.config.reasoning_effort,
    }


def skip_reason(ctx: PipelineContext) -> str | None:
    return None


def validate_cached(ctx: PipelineContext, result: SynthesisResult) -> bool:
    return True


async def run(ctx: PipelineContext) -> SynthesisResult:
    d = ctx.video.duration_s
    corr: CorrectionResult = ctx.results[StageName.CORRECT]
    vis: VisionResult = ctx.results[StageName.VISION]
    segs, scenes = corr.segments, vis.scenes
    units = ctx.units()

    # 1) propose + validate chapters
    windows = _propose_windows(ctx, segs, scenes)
    ctx.progress(0, 3, "Proposing chapters")
    parts: list[Bound] = []
    for i, (w0, w1) in enumerate(windows):
        parts.extend(await _propose(ctx, segs, scenes, w0, w1, i))
    problems = validate_chapters(parts, d)
    if problems:
        try:
            parts = repair_chapters(parts, d)
            ctx.warn(
                "chapters_repaired", "Chapter boundaries were repaired: " + "; ".join(problems[:5])
            )
        except ValueError:
            parts = fallback_chapters(d, ctx.config.max_chapters)
            ctx.warn("chapters_fallback", "Chapters replaced with an even split.")
    n = len(parts)
    chapters = [
        Chapter(
            index=i + 1,
            id=f"ch-{i + 1:02d}",
            title=title,
            start=s,
            end=e,
            frames=[f.frame for f in frames_in(scenes, s, e, i == n - 1)],
        )
        for i, (title, s, e) in enumerate(parts)
    ]
    ctx.emit("chapters", {"chapters": [c.model_dump(mode="json") for c in chapters]})

    # 2) map: per-chapter detail
    done = 0
    ctx.progress(0, n + 1, f"Summarising {n} chapter(s)")

    quotes_dropped = 0

    async def detail(idx: int) -> None:
        nonlocal done, quotes_dropped
        ch = chapters[idx]
        last = idx == n - 1
        csegs = segments_in(segs, ch.start, ch.end, last)
        cscenes = frames_in(scenes, ch.start, ch.end, last)
        budget_chars = int(ctx.config.prompt_token_budget * 3.6 * 0.8)
        material = clip(_material(ctx, csegs, cscenes), budget_chars)
        prompt = (
            f"VIDEO_FILE: {ctx.video.filename}\nVIDEO_DURATION_SECONDS: {d:.2f}\n"
            f"CHAPTER: {ch.title}\nCHAPTER_RANGE: {ch.start:.2f}-{ch.end:.2f} "
            f"({fmt_range(ch.start, ch.end, d)})\n"
            f"USER_CONTEXT: {ctx.settings.context or '(none)'}\n\n{material}"
        )
        ident = digest(prompt, ctx.settings.synthesize_model, PROMPT_VERSION)
        out = units.get(f"chapter_{idx:02d}", ChapterDetailOut, ident)
        if out is None:
            try:
                res = await ctx.providers.text.complete_json(
                    ChapterDetailOut,
                    system=DETAIL_SYSTEM,
                    user=prompt,
                    stage=StageName.SYNTHESIZE,
                    model=ctx.settings.synthesize_model,
                )
                ctx.add_usage(ctx.settings.synthesize_model, res.usage)
                out = res.value
                units.put(f"chapter_{idx:02d}", out, ident)
            except FatalProviderError:
                raise
            except FrameIngestError as exc:
                ctx.warn(
                    "chapter_summary_failed",
                    f"Summary for chapter {ch.index} ('{ch.title}') failed: {exc.message}",
                    ch.start,
                    ch.end,
                )
        vis_entities = [e for sc in cscenes for e in sc.entities]
        ch.on_screen_text = dedupe_keep_order(t for sc in cscenes for t in sc.on_screen_text)
        if out is not None:
            quotes, dropped = verify_quotes(
                [Quote(t=min(max(q.t, 0.0), d), text=q.text) for q in out.quotes],
                csegs,
                ch.start,
                ch.end,
            )
            quotes_dropped += dropped
            if dropped:
                ctx.warn(
                    "quotes_dropped",
                    f"{dropped} non-verbatim quote(s) removed from chapter {ch.index}.",
                    ch.start,
                    ch.end,
                )
            ch.summary = out.summary.strip()
            ch.key_points = [k.strip() for k in out.key_points if k.strip()][:8]
            ch.quotes = quotes
            ch.entities = merge_entities(
                [Entity(name=e.name.strip(), kind=e.kind) for e in out.entities if e.name.strip()],
                vis_entities,
            )
            ch.decisions_claims = [c.strip() for c in out.decisions_claims if c.strip()]
            ch.visual_description = out.visual_summary.strip()
        else:
            ch.summary = "(Summary unavailable — see processing warnings.)"
            ch.entities = merge_entities(vis_entities)
        if not ch.visual_description and cscenes:
            ch.visual_description = " ".join(sc.scene_description for sc in cscenes[:3])
        done += 1
        ctx.progress(done, n + 1, f"{done}/{n} chapters")

    await run_units(list(range(n)), detail, ctx.config.api_concurrency)
    ctx.emit("chapters", {"chapters": [c.model_dump(mode="json") for c in chapters]})

    # 3) reduce: whole-video synthesis
    digest_lines = []
    for c in chapters:
        pts = "\n".join(f"  - {k}" for k in c.key_points)
        digest_lines.append(
            f"## {c.title} [{fmt_range(c.start, c.end, d)}] (starts at {c.start:.0f}s)\n"
            f"{c.summary}\n{pts}"
        )
    material = clip("\n\n".join(digest_lines), int(ctx.config.prompt_token_budget * 3.6 * 0.8))
    gprompt = (
        f"VIDEO_FILE: {ctx.video.filename}\nVIDEO_DURATION_SECONDS: {d:.2f}\n"
        f"USER_CONTEXT: {ctx.settings.context or '(none)'}\n"
        f"HAS_AUDIO: {str(ctx.video.has_audio).lower()}\n\nCHAPTERS:\n{material}"
    )
    gident = digest(gprompt, ctx.settings.synthesize_model, PROMPT_VERSION)
    gout = units.get("global", GlobalSynthesisOut, gident)
    if gout is None:
        try:
            res_g = await ctx.providers.text.complete_json(
                GlobalSynthesisOut,
                system=GLOBAL_SYSTEM,
                user=gprompt,
                stage=StageName.SYNTHESIZE,
                model=ctx.settings.synthesize_model,
            )
            ctx.add_usage(ctx.settings.synthesize_model, res_g.usage)
            gout = res_g.value
            units.put("global", gout, gident)
        except FatalProviderError:
            raise
        except FrameIngestError as exc:
            ctx.warn("global_summary_failed", f"Whole-video summary failed: {exc.message}")
    stem = file_stem(ctx.video.filename)
    if gout is None:
        synthesis = VideoSynthesis(
            title=stem,
            tldr=chapters[0].summary if chapters else "",
            abstract=" ".join(c.title for c in chapters),
        )
    else:
        synthesis = VideoSynthesis(
            title=gout.title.strip() or stem,
            tldr=gout.tldr.strip(),
            abstract=gout.abstract.strip(),
            glossary=[
                GlossaryEntry(
                    term=g.term.strip(),
                    definition=g.definition.strip(),
                    first_seen=min(max(g.first_seen_s, 0.0), d)
                    if g.first_seen_s is not None
                    else None,
                )
                for g in gout.glossary
                if g.term.strip()
            ],
            open_questions=[q.strip() for q in gout.open_questions if q.strip()],
            tags=clean_tags(gout.tags),
        )
    ctx.emit("synthesis", synthesis.model_dump(mode="json"))
    ctx.progress(n + 1, n + 1, "Synthesis complete")
    return SynthesisResult(chapters=chapters, synthesis=synthesis, quotes_dropped=quotes_dropped)
