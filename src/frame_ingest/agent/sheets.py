"""Contact sheets: several frames tiled into one image with burned-in index and timestamp.

One image per batch cuts the agent's image tokens and call count; the full-size frames stay on
disk for drill-down.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from frame_ingest.models import FrameInfo
from frame_ingest.pipeline.timefmt import fmt_ts

CELL_W = 480
COLUMNS = 3
LABEL_H = 26
PAD = 6


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError):  # pragma: no cover - very old Pillow or no FreeType
        return ImageFont.load_default()


def build_sheet(
    frames: Sequence[FrameInfo],
    frames_dir: Path,
    out: Path,
    *,
    start_index: int,
    duration: float,
) -> None:
    """Write `out` (JPEG). Cell labels read `#<index>  HH:MM:SS`, matching the manifest."""
    rows = -(-len(frames) // COLUMNS)
    cell_h = max(
        (round(CELL_W * f.height / max(1, f.width)) for f in frames), default=CELL_W * 9 // 16
    )
    sheet = Image.new(
        "RGB",
        (COLUMNS * CELL_W + (COLUMNS + 1) * PAD, rows * (cell_h + LABEL_H) + (rows + 1) * PAD),
        (24, 24, 24),
    )
    draw = ImageDraw.Draw(sheet)
    font = _font(18)
    for i, f in enumerate(frames):
        col, row = i % COLUMNS, i // COLUMNS
        x = PAD + col * (CELL_W + PAD)
        y = PAD + row * (cell_h + LABEL_H + PAD)
        with Image.open(frames_dir / f.name) as im:
            thumb = im.convert("RGB")
            thumb.thumbnail((CELL_W, cell_h))
            sheet.paste(thumb, (x, y + LABEL_H))
        draw.text(
            (x + 2, y + 2),
            f"#{start_index + i}  {fmt_ts(f.t, duration)}",
            fill=(255, 220, 0),
            font=font,
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, "JPEG", quality=82)
