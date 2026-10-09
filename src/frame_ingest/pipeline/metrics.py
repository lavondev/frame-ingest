"""Pacing and hook metrics, computed deterministically from the finished
analysis: no model is asked. Opt in with `--metrics`; the numbers go in the JSON sidecar and a
"Pacing and Hook" section of the document."""

from __future__ import annotations

from frame_ingest.models import (
    Analysis,
    ChapterPace,
    HookMetrics,
    PacingMetrics,
    Segment,
)

HOOK_WINDOW_S = 15.0


def _words(text: str) -> int:
    return len(text.split())


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _coverage(segs: list[Segment], start: float, end: float) -> float | None:
    span = end - start
    if span <= 0 or not segs:
        return None
    clipped = [
        (max(s.start, start), min(s.end, end)) for s in segs if s.end > start and s.start < end
    ]
    return round(sum(e - s for s, e in _merge(clipped)) / span, 4)


def compute_metrics(a: Analysis) -> PacingMetrics:
    d = a.video.duration_s
    segs = [s for s in a.transcript.segments if s.text.strip()]
    minutes = d / 60 if d > 0 else 0
    words = sum(_words(s.text) for s in segs)

    longest: float | None = None
    if segs:
        merged = _merge([(s.start, s.end) for s in segs])
        gaps = [merged[0][0], d - merged[-1][1]]
        gaps += [merged[i + 1][0] - merged[i][1] for i in range(len(merged) - 1)]
        longest = round(max(0.0, *gaps), 2)

    chapters = []
    for c in a.chapters:
        cs = [s for s in segs if c.start <= s.start < c.end]
        span = (c.end - c.start) / 60
        chapters.append(
            ChapterPace(
                chapter_id=c.id,
                words_per_minute=round(sum(_words(s.text) for s in cs) / span, 1)
                if cs and span > 0
                else None,
                speech_coverage=_coverage(cs, c.start, c.end),
            )
        )

    window = min(HOOK_WINDOW_S, d)
    early = [s for s in segs if s.start < window]
    cuts = sum(1 for f in a.frames if f.reason == "scene" and f.t < window)
    text_blocks = sum(len(sc.on_screen_text) for sc in a.scenes if sc.t < window)
    return PacingMetrics(
        words_per_minute=round(words / minutes, 1) if segs and minutes else None,
        speech_coverage=_coverage(segs, 0.0, d),
        longest_silence_s=longest,
        scene_changes_per_minute=round(sum(1 for f in a.frames if f.reason == "scene") / minutes, 2)
        if minutes
        else 0.0,
        chapters=chapters,
        hook=HookMetrics(
            window_s=window,
            first_speech_at_s=round(segs[0].start, 2) if segs else None,
            words_in_window=sum(_words(s.text) for s in early),
            scene_changes_in_window=cuts,
            on_screen_text_blocks_in_window=text_blocks,
            opening_line=early[0].text.strip() if early else None,
        ),
    )
