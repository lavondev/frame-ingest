#!/usr/bin/env python3
"""Play the host agent, with no AI: read a manifest from `frame-ingest prepare` and write valid
outputs for it, so the rest of the loop (assemble, validate, scan) can be tried by hand.

    uv run python scripts/demo_agent.py <path to manifest.json>

The descriptions are generic placeholders and the correction only fixes "Widjet", so this proves
the plumbing, not the quality of an analysis: that is what a real agent is for.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(manifest["directories"]["out"])
    (out / "vision").mkdir(parents=True, exist_ok=True)
    duration = manifest["video"]["duration_s"]

    for sheet in range(1, len(manifest["sheets"]) + 1):
        frames = [
            {
                "frame": f["name"],
                "scene_description": f"Placeholder description of the frame at {f['time']}.",
                "on_screen_text": [],
                "change_from_previous": "First frame" if f["index"] == 0 else "The scene changed.",
                "entities": [],
                "scene_type": "other",
            }
            for f in manifest["frames"]
            if f["sheet"] == sheet
        ]
        (out / "vision" / f"batch-{sheet:02d}.json").write_text(json.dumps({"frames": frames}))

    segments = []
    if manifest["transcript"]["segments"]:
        transcript = json.loads(Path(manifest["transcript"]["file"]).read_text(encoding="utf-8"))
        segments = transcript["segments"]
        fixed = [
            {"id": s["id"], "corrected_text": s["raw_text"].replace("Widjet", "Widget")}
            for s in segments
        ]
        (out / "corrections.json").write_text(json.dumps({"segments": fixed}))

    mid = round(duration / 2, 2)
    quote = segments[0]["raw_text"].replace("Widjet", "Widget") if segments else ""
    chapter = {
        "summary": "A placeholder summary written by the demo script.",
        "key_points": ["Placeholder point"],
        "entities": [],
        "decisions_claims": [],
        "visual_summary": "",
    }
    synthesis = {
        "title": "Demo walkthrough",
        "tldr": "A demo document built without an AI.",
        "abstract": "This document was produced by scripts/demo_agent.py to exercise the CLI.",
        "glossary": [],
        "open_questions": [],
        "tags": ["demo"],
        "chapters": [
            {
                "title": "First half",
                "start": 0,
                "end": mid,
                **chapter,
                "quotes": [{"t": 1.0, "text": quote}] if quote else [],
            },
            {"title": "Second half", "start": mid, "end": duration, **chapter, "quotes": []},
        ],
    }
    (out / "synthesis.json").write_text(json.dumps(synthesis))
    print(f"wrote vision, corrections and synthesis files under {out}")


if __name__ == "__main__":
    main()
