"""Caption files (SRT, WebVTT) to transcript segments.

Captions are untrusted input. The parser is strict: only lines that parse as timed cues survive,
so a file that is not really a caption file yields no cues instead of being echoed anywhere.
Rolling auto-captions (each cue repeating the previous cue's last line) are de-duplicated.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from frame_ingest.errors import MediaError
from frame_ingest.guard.paths import PathRejected, read_bytes_nofollow, require_regular_file
from frame_ingest.guard.text import clean
from frame_ingest.models import Segment

MAX_CAPTION_BYTES = 5 * 1024 * 1024
MAX_CUES = 20_000
SUFFIXES = {".srt", ".vtt"}
_TIME = re.compile(
    r"^\s*(?:(\d{1,3}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*"
    r"(?:(\d{1,3}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)
_TAGS = re.compile(r"<[^>]*>|\{\\[^}]*\}")


def _seconds(h: str | None, m: str, s: str, ms: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _cues(text: str) -> list[tuple[float, float, list[str]]]:
    cues: list[tuple[float, float, list[str]]] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = block.split("\n")
        for i, line in enumerate(lines):
            m = _TIME.match(line)
            if m:
                g = m.groups()
                start, end = _seconds(*g[0:4]), _seconds(*g[4:8])
                body = [" ".join(html.unescape(_TAGS.sub("", ln)).split()) for ln in lines[i + 1 :]]
                cues.append((start, end, [b for b in body if b]))
                break
        if len(cues) > MAX_CUES:
            raise MediaError(
                f"The caption file has more than {MAX_CUES} cues.", code="limit_exceeded"
            )
    return cues


def _dedupe(cues: list[tuple[float, float, list[str]]]) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    prev: list[str] = []
    for start, end, lines in cues:
        fresh = [ln for ln in lines if ln not in prev]
        prev = lines or prev
        if not fresh:
            if out:  # a pure repeat only extends the previous cue
                out[-1] = (out[-1][0], max(out[-1][1], end), out[-1][2])
            continue
        out.append((start, max(end, start), " ".join(fresh)))
    return out


def parse_captions(text: str, duration_s: float) -> list[Segment]:
    """Segments with ids 0..n-1, ordered, clamped to the video. Raises MediaError if none."""
    cleaned = clean(text)
    segments: list[Segment] = []
    for start, end, body in sorted(_dedupe(_cues(cleaned)), key=lambda c: (c[0], c[1])):
        if start > duration_s + 1:
            continue
        segments.append(
            Segment(
                id=len(segments),
                start=min(start, duration_s),
                end=min(max(end, start), duration_s),
                raw_text=body,
            )
        )
    if not segments:
        raise MediaError(
            "No timed caption cues were found. Expected an SRT or WebVTT file.",
            code="invalid_captions",
            status=415,
        )
    return segments


def load_captions(path: Path, duration_s: float) -> list[Segment]:
    """Read a caption file the caller named: .srt/.vtt only, regular file, size-capped."""
    if path.suffix.lower() not in SUFFIXES:
        raise MediaError("Captions must be a .srt or .vtt file.", code="invalid_captions")
    try:
        require_regular_file(path, what="captions")
    except PathRejected as exc:
        raise MediaError(exc.message, code="invalid_captions") from None
    if path.stat().st_size > MAX_CAPTION_BYTES:
        raise MediaError("The caption file is too large.", code="limit_exceeded", status=413)
    data = read_bytes_nofollow(path)
    return parse_captions(data.decode("utf-8-sig", errors="replace"), duration_s)
