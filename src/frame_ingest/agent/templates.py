"""Fill-in-the-blank templates for the files the host agent writes.

`prepare` writes them under `agent/out/` so the agent never has to guess a shape:

* `vision/batch-NN.json`: one per contact sheet, every frame name already in place;
* `corrections.json`: every segment id with its raw text as the starting `corrected_text`;
* `synthesis.json`: chapter slots that already cover 0 to the duration.

Every field the agent must write starts as a `TODO:` string, and the checks refuse any that is
left. A template is only ever rewritten while it is untouched (its hash is recorded in
`agent/templates.json`), so the agent's work is never overwritten.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from frame_ingest.agent.checks import frame_names
from frame_ingest.models import FrameInfo, Segment
from frame_ingest.pipeline.synthesize import MIN_CHAPTER_S, chapter_target_range
from frame_ingest.pipeline.timefmt import fmt_ts
from frame_ingest.storage import atomic_write_text, read_json

INDEX_FILE = "templates.json"
SCENE_TODO = (
    "TODO: describe what is on screen (and set scene_type: slide, screen_recording, "
    "talking_head, whiteboard, demo, diagram, chart, title_card, b_roll or other)"
)
CHANGE_TODO = "TODO: what changed since the previous frame ('first frame' for the first)"
CORRECTIONS_TODO = (
    "TODO: fix misheard words (names, jargon, code) in corrected_text using what is on screen; "
    "keep every id and change nothing else; then delete this todo line"
)


def _dumps(obj: object) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TemplateIndex:
    """`agent/templates.json`: the hash of each template as written, and each vision template's
    frame list (so `check` can say which frame of the template is missing)."""

    def __init__(self, adir: Path) -> None:
        self.path = adir / INDEX_FILE
        data: dict[str, Any] = {}
        if self.path.is_file() and not self.path.is_symlink():
            try:
                data = read_json(self.path)
            except (ValueError, OSError):
                data = {}
        self.hashes: dict[str, str] = dict(data.get("hashes") or {})
        self.vision: dict[str, list[str]] = dict(data.get("vision") or {})

    def save(self) -> None:
        atomic_write_text(self.path, _dumps({"hashes": self.hashes, "vision": self.vision}))

    def untouched(self, out_dir: Path, rel: str) -> bool:
        """True when `rel` does not exist or still holds exactly the template we wrote."""
        path = out_dir / rel
        if not path.exists():
            return True
        if path.is_symlink() or not path.is_file():
            return False
        try:
            return self.hashes.get(rel) == _sha(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return False

    def write(self, out_dir: Path, rel: str, obj: object) -> None:
        text = _dumps(obj)
        atomic_write_text(out_dir / rel, text)
        self.hashes[rel] = _sha(text)

    def remove(self, out_dir: Path, rel: str) -> None:
        (out_dir / rel).unlink(missing_ok=True)
        self.hashes.pop(rel, None)
        self.vision.pop(rel, None)


def _vision_entry(f: FrameInfo, duration: float) -> dict[str, Any]:
    return {
        "frame": f.name,
        "time": fmt_ts(f.t, duration),  # a reading aid; not part of the schema
        "scene_description": SCENE_TODO,
        "on_screen_text": [],
        "change_from_previous": CHANGE_TODO,
        "entities": [],
        "scene_type": "other",
    }


def _vision_templates(
    out_dir: Path,
    index: TemplateIndex,
    frames: list[FrameInfo],
    sheets: list[dict[str, Any]],
    duration: float,
) -> None:
    vdir = out_dir / "vision"
    vdir.mkdir(parents=True, exist_ok=True)
    existing = sorted(f"vision/{p.name}" for p in vdir.glob("*.json"))
    by_name = {f.name: f for f in frames}
    if all(index.untouched(out_dir, rel) for rel in existing):
        for rel in existing:  # nothing written yet: lay the batches out on the current sheets
            index.remove(out_dir, rel)
        for n, sheet in enumerate(sheets, 1):
            rel = f"vision/batch-{n:02d}.json"
            names = list(sheet["frames"])
            index.write(
                out_dir,
                rel,
                {
                    "sheet": sheet["file"],
                    "frames": [_vision_entry(by_name[name], duration) for name in names],
                },
            )
            index.vision[rel] = names
        return
    covered = {name for rel in existing for name in frame_names(out_dir / rel)}
    new = [f for f in frames if f.name not in covered]
    if not new:
        return
    numbers = [int(r[13:15]) for r in existing if r[7:13] == "batch-" and r[13:15].isdigit()]
    rel = f"vision/batch-{max(numbers, default=0) + 1:02d}.json"
    sheet_of = {name: s["file"] for s in sheets for name in s["frames"]}
    index.write(
        out_dir,
        rel,
        {
            "sheets": sorted({sheet_of[f.name] for f in new if f.name in sheet_of}),
            "frames": [_vision_entry(f, duration) for f in new],
        },
    )
    index.vision[rel] = [f.name for f in new]


def _chapter_slots(duration: float, max_chapters: int) -> list[tuple[float, float]]:
    lo, hi = chapter_target_range(duration, max_chapters)
    n = max(1, min((lo + hi + 1) // 2, int(duration // max(MIN_CHAPTER_S, 1.0)) or 1))
    edges = [round(i * duration / n, 2) for i in range(n)] + [round(duration, 2)]
    return [(edges[i], edges[i + 1]) for i in range(n)]


def _synthesis_template(duration: float, max_chapters: int) -> dict[str, Any]:
    slots = _chapter_slots(duration, max_chapters)
    return {
        "title": "TODO: a short, specific title for the video",
        "tldr": "TODO: 1-3 sentences: what the video shows and its main point",
        "abstract": "TODO: one paragraph summary",
        "glossary": [],
        "open_questions": [],
        "tags": [],
        "chapters": [
            {
                "title": "TODO: chapter title",
                "start": start,
                "end": end,
                "summary": "TODO: what happens in this chapter",
                "key_points": [],
                "quotes": [],
                "entities": [],
                "decisions_claims": [],
                "visual_summary": "TODO: what is on screen in this chapter",
            }
            for start, end in slots
        ],
    }


def write_templates(
    adir: Path,
    *,
    frames: list[FrameInfo],
    sheets: list[dict[str, Any]],
    segments: list[Segment],
    duration: float,
    max_chapters: int,
) -> dict[str, Any]:
    """Write (or refresh, while untouched) every template. Returns what to fill, for the
    manifest: {"vision": [{file, frames, sheets}], "corrections": file|None, "synthesis": file}."""
    out_dir = adir / "out"
    index = TemplateIndex(adir)
    _vision_templates(out_dir, index, frames, sheets, duration)

    if segments:
        if index.untouched(out_dir, "corrections.json"):
            index.write(
                out_dir,
                "corrections.json",
                {
                    "todo": CORRECTIONS_TODO,
                    "segments": [{"id": s.id, "corrected_text": s.raw_text} for s in segments],
                },
            )
    elif index.hashes.get("corrections.json") and index.untouched(out_dir, "corrections.json"):
        index.remove(out_dir, "corrections.json")  # the transcript went away: so does the template

    if index.untouched(out_dir, "synthesis.json"):
        index.write(out_dir, "synthesis.json", _synthesis_template(duration, max_chapters))
    index.save()

    sheet_of = {name: s["file"] for s in sheets for name in s["frames"]}
    vision = [
        {
            "file": str(out_dir / rel),
            "frames": names,
            "sheets": sorted({sheet_of[n] for n in names if n in sheet_of}),
        }
        for rel, names in sorted(index.vision.items())
        if (out_dir / rel).is_file()
    ]
    corrections = out_dir / "corrections.json"
    return {
        "vision": vision,
        "corrections": str(corrections) if segments else None,
        "synthesis": str(out_dir / "synthesis.json"),
    }


def template_frames(adir: Path, rel: str) -> list[str] | None:
    """The frame list of a vision template, or None if `rel` is not one."""
    return TemplateIndex(adir).vision.get(rel)
