"""Stage 3: transcription behind the Transcriber interface.

Chunk results come back with chunk-relative timestamps. This module offsets them to absolute
video time, dedupes the overlap between neighbouring chunks, and stitches one Transcript.

Two timestamp regimes (see DESIGN.md):
  * segment mode  — models that return segment timestamps (verbose_json): long chunks (~10 min)
                    with ~1 s overlap.
  * chunk mode    — models that only return text: short chunks (30-60 s); a chunk's boundaries
                    are the timestamps (precision "chunk").
"""

from __future__ import annotations

import asyncio
import math
import re

from frame_ingest.errors import FatalProviderError
from frame_ingest.models import Segment, StageName, Transcript
from frame_ingest.pipeline.audio import AudioChunk, AudioResult
from frame_ingest.pipeline.context import PipelineContext, StageResult
from frame_ingest.pipeline.timefmt import offset_segments_t
from frame_ingest.pipeline.util import run_units
from frame_ingest.providers.base import RawTranscription

NAME = StageName.TRANSCRIBE
VERSION = 1
DEPS: list[StageName] = [StageName.AUDIO]
PROMPT_CHAR_BUDGET = 600  # ~224 tokens worst case (jargon tokenises densely)
_WORD_RE = re.compile(r"[\w']+", re.UNICODE)


class TranscribeResult(StageResult):
    transcript: Transcript


# ── glossary / prompt ───────────────────────────────────────────────────────
def parse_glossary(context: str, limit: int = 100) -> list[str]:
    """Short comma/line separated items from the user's context text, usable as keywords."""
    terms: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[\n,;]+", context):
        term = part.strip(" \t-•*·:")
        if not term or len(term) > 60 or len(term.split()) > 6:
            continue
        low = term.lower()
        if low not in seen:
            seen.add(low)
            terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def truncate_prompt(text: str, budget: int = PROMPT_CHAR_BUDGET) -> str:
    """Whisper accepts ~224 prompt tokens. Truncate at a word boundary (keeps the start)."""
    text = " ".join(text.split())
    if len(text) <= budget:
        return text
    cut = text[:budget]
    cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return cut.rstrip(",;: ")


# ── stitching ───────────────────────────────────────────────────────────────
def _norm_words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text)]


def trim_repeated_prefix(prev_text: str, next_text: str, max_words: int = 14) -> str:
    """If next_text starts by repeating the tail of prev_text (≥2 words), drop the repeat."""
    prev_words = _norm_words(prev_text)
    next_tokens = next_text.split()
    next_words = [(_norm_words(t) or [""])[0] for t in next_tokens]
    for k in range(min(max_words, len(prev_words), len(next_words)), 1, -1):
        if prev_words[-k:] == next_words[:k]:
            return " ".join(next_tokens[k:])
    return next_text


def stitch(
    chunks: list[AudioChunk],
    results: dict[int, RawTranscription],
    duration: float,
    upto: int | None = None,
) -> list[Segment]:
    """Merge chunk transcriptions into one ordered segment list.

    Overlap dedupe is plan-based: the cut point between chunk i and i+1 is the middle of their
    overlap, and each chunk only keeps segments whose midpoint falls in [cut_{i-1}, cut_i). So the
    result for a prefix of chunks equals a prefix of the full result (used for live streaming).
    """
    n = len(chunks)
    upto = n if upto is None else upto
    cuts = [(chunks[i + 1].start + chunks[i].end) / 2 for i in range(n - 1)]
    out: list[Segment] = []
    last_chunk_of_prev = -1
    for i in range(upto):
        chunk = chunks[i]
        raw = results[i]
        lo = cuts[i - 1] if i > 0 else -math.inf
        hi = cuts[i] if i < n - 1 else math.inf
        for seg in raw.segments:
            text = " ".join(seg.text.split())
            if not text:
                continue
            start, end = offset_segments_t(seg.start, seg.end, chunk.start, duration)
            mid = (start + end) / 2
            if not (lo <= mid < hi):
                continue
            if out and last_chunk_of_prev != i:
                # first kept segment of a new chunk: drop words repeated across the boundary
                text = trim_repeated_prefix(out[-1].raw_text, text)
                if not text:
                    continue
            out.append(
                Segment(id=len(out), start=start, end=end, raw_text=text, speaker=seg.speaker)
            )
            last_chunk_of_prev = i
    out.sort(key=lambda s: (s.start, s.end))
    return [s.model_copy(update={"id": idx}) for idx, s in enumerate(out)]


# ── stage ───────────────────────────────────────────────────────────────────
def key_params(ctx: PipelineContext) -> dict[str, object]:
    return {
        "model": ctx.transcribe_model,
        "context": ctx.settings.context,
        "language": ctx.settings.language,
        "diarize": ctx.settings.diarize,
    }


def skip_reason(ctx: PipelineContext) -> str | None:
    return None if ctx.video.has_audio else "No audio track — transcription skipped."


async def run(ctx: PipelineContext) -> TranscribeResult:
    audio: AudioResult = ctx.results[StageName.AUDIO]
    if not audio.chunks:
        return TranscribeResult(transcript=Transcript(timestamp_precision="none"))

    model = ctx.transcribe_model
    transcriber = ctx.providers.transcriber
    caps = transcriber.caps_for(model)
    if caps.deprecated:
        ctx.warn("model_deprecated", caps.deprecated)
    if ctx.settings.diarize:
        ctx.warn(
            "diarize_experimental",
            "Speaker diarization is experimental and its model is deprecated by OpenAI.",
        )

    terms = parse_glossary(ctx.settings.context)
    prompt = (
        truncate_prompt(ctx.settings.context) if (caps.prompt and ctx.settings.context) else None
    )
    keywords = terms if caps.keywords else []

    chunks = audio.chunks
    units = ctx.units()
    results: dict[int, RawTranscription] = {}
    lock = asyncio.Lock()
    emitted_chunks = 0
    emitted_segments = 0
    total = len(chunks)

    async def one(chunk: AudioChunk) -> None:
        nonlocal emitted_chunks, emitted_segments
        ident = f"{model}|{prompt}|{keywords}|{ctx.settings.language}|{chunk.start}|{chunk.end}"
        raw = units.get(f"chunk_{chunk.index:03d}", RawTranscription, ident)
        if raw is None:
            raw = await transcriber.transcribe(
                ctx.job_dir / chunk.file,
                model=model,
                duration_s=chunk.duration,
                prompt=prompt,
                keywords=keywords,
                language=ctx.settings.language,
                diarize=ctx.settings.diarize,
            )
            units.put(f"chunk_{chunk.index:03d}", raw, ident)
            ctx.add_usage(model, raw.usage)
        async with lock:
            results[chunk.index] = raw
            ready = 0
            while ready in results:
                ready += 1
            if ready > emitted_chunks:
                segs = stitch(chunks, results, ctx.video.duration_s, upto=ready)
                new = segs[emitted_segments:]
                if new:
                    ctx.emit("transcript", {"segments": [s.model_dump(mode="json") for s in new]})
                emitted_chunks, emitted_segments = ready, len(segs)
            ctx.progress(len(results), total, f"Transcribed {len(results)}/{total} chunks")

    ctx.progress(0, total, f"Transcribing {total} chunk(s) with {model}")
    await run_units(chunks, one, ctx.config.api_concurrency)

    segments = stitch(chunks, results, ctx.video.duration_s)
    if not segments:
        ctx.warn("empty_transcript", "The audio track produced no recognisable speech.")
    precision = (
        "chunk"
        if any(r.precision == "chunk" for r in results.values()) or audio.short_mode
        else "segment"
    )
    language = next((r.language for r in results.values() if r.language), None)
    if not segments and not any(results.values()):
        raise FatalProviderError("Transcription returned no results.")
    return TranscribeResult(
        transcript=Transcript(
            model=model,
            language=language,
            timestamp_precision=precision,
            chunk_count=total,
            segments=segments,
        )
    )
