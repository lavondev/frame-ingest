"""Small helpers for rendering transcript/scene data into prompts and for fuzzy matching."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from frame_ingest.models import FrameAnalysis, Segment
from frame_ingest.pipeline.timefmt import fmt_ts

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize(text: str) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace (for verbatim checks)."""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(_PUNCT_RE.sub(" ", t.lower()).split())


def transcript_lines(
    segments: Iterable[Segment], *, corrected: bool, duration: float | None = None
) -> str:
    return "\n".join(
        f"[{fmt_ts(s.start, duration)}] "
        f"{s.corrected_text if corrected and s.corrected_text else s.raw_text}"
        for s in segments
    )


def window_segments(
    segments: list[Segment], start: float, end: float, pad: float = 5.0
) -> list[Segment]:
    return [s for s in segments if s.end >= start - pad and s.start <= end + pad]


def clip(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"


def scene_lines(scenes: Iterable[FrameAnalysis], duration: float | None = None) -> str:
    out = []
    for sc in scenes:
        txt = f" | on-screen: {'; '.join(sc.on_screen_text)}" if sc.on_screen_text else ""
        out.append(
            f"[{fmt_ts(sc.t, duration)}] ({sc.scene_type.value}) {sc.scene_description}{txt}"
        )
    return "\n".join(out)


def dedupe_keep_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key = normalize(it)
        if key and key not in seen:
            seen.add(key)
            out.append(it.strip())
    return out
