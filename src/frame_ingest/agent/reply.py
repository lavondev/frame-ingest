"""What the user sees at the end: a previewable copy of the document and the chat reply.

The reply is built in code, like the document, so it looks the same on every run and in every
harness; the agent pastes it unchanged. Every string in it that came from the video or from a
model goes through the same neutraliser as the document.

The copy goes to `frame-ingest-out/` in the current directory (the folder the agent's session
works in), the one place a harness can preview it from. It is the only default write outside
<home>, so it follows the export rules: a fixed folder name (never an argument), not a system
location or a hidden folder in the home directory, no symlinks, and a file from a different
video is never overwritten. `preview_copy: false` in the config turns it off. A failed copy is
only a note: the document always stays in the job directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from frame_ingest.export import ExportRefused, _check_root
from frame_ingest.guard.paths import is_within, job_jail
from frame_ingest.models import Analysis
from frame_ingest.pipeline.assemble import inline
from frame_ingest.pipeline.timefmt import fmt_ts
from frame_ingest.storage import atomic_write_text, file_stem

PREVIEW_DIR = "frame-ingest-out"


def _ours(path: Path, sha256: str) -> bool:
    """True when `path` is a frame-ingest document of the same video (safe to replace)."""
    try:
        head = path.read_text(encoding="utf-8")[:4000]
    except (OSError, UnicodeDecodeError):
        return False
    return head.startswith("---\n") and f"input_sha256: {sha256}" in head


def preview_copy(
    md_path: Path, analysis: Analysis, job_id: str, cwd: Path
) -> tuple[Path | None, str]:
    """Copy the document into <cwd>/frame-ingest-out/. Returns (path, note)."""
    try:
        base = _check_root(cwd)
    except ExportRefused as exc:
        return None, f"no preview copy: the current folder is not suitable ({exc.message})"
    dest_dir = base / PREVIEW_DIR
    if dest_dir.is_symlink() or (dest_dir.exists() and not dest_dir.is_dir()):
        return None, f"no preview copy: {PREVIEW_DIR} is not a plain folder"
    stem = file_stem(md_path.name.removesuffix(".analysis.md"))
    sha = analysis.video.sha256
    target = None
    for name in (f"{stem}.md", f"{stem}-{job_id}.md"):
        path = dest_dir / name
        if path.is_symlink():
            continue
        if not path.exists() or _ours(path, sha):
            target = path
            break
    if target is None:
        return None, f"no preview copy: {PREVIEW_DIR}/{stem}.md belongs to another video"
    try:
        dest_dir.mkdir(exist_ok=True)
        with job_jail(dest_dir):
            if not is_within(target, dest_dir):  # pragma: no cover - names are sanitised
                return None, "no preview copy: refusing a path outside the folder"
            atomic_write_text(target, md_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, f"no preview copy: {type(exc).__name__}"
    return target, ""


def _clock(t: float, duration: float) -> str:
    """The document's HH:MM:SS (same rounding), shortened to MM:SS under an hour."""
    stamp = fmt_ts(t, duration)
    return stamp if duration >= 3600 else stamp[3:]


def _cell(text: str) -> str:
    return inline(text).replace("|", "\\|")


def _link(path: Path, cwd: Path) -> str:
    real_cwd = Path(os.path.realpath(cwd))
    if is_within(path, real_cwd):
        rel = path.relative_to(real_cwd).as_posix()
        return f"[{path.name}]({rel})"
    return f"`{path}`"


def reply_markdown(
    analysis: Analysis,
    document: Path,
    copy: Path | None,
    cwd: Path,
    flags: dict[str, int],
) -> str:
    """The compact summary the agent sends back, verbatim."""
    d = analysis.video.duration_s
    syn = analysis.synthesis
    title = inline(syn.title) or inline(file_stem(analysis.video.filename))
    out = [f"## {title}", "", _link(copy, cwd) if copy else f"`{document}`", ""]
    if syn.tldr.strip():
        out += [f"**TL;DR** {inline(syn.tldr)}", ""]
    out += ["| # | Chapter | Time |", "|---|---|---|"]
    out += [
        f"| {c.index} | {_cell(c.title)} | {_clock(c.start, d)} - {_clock(c.end, d)} |"
        for c in analysis.chapters
    ]
    cov = analysis.coverage
    if cov is not None:
        out += [
            "",
            f"Coverage: audio {cov.audio} · transcript {cov.transcript_source} · "
            f"frames {cov.frames_analyzed} · quotes {cov.quotes_verified}",
        ]
    notes = []
    if cov is not None and cov.audio == "no":
        notes.append(f"frames only, no transcript ({inline(cov.note or 'no speech')})")
    if flags:
        notes.append(
            "the video contains text that looks like instructions to an AI "
            f"({', '.join(sorted(flags))}); it was treated as data and not acted on"
        )
    if notes:
        out += ["", "Note: " + "; ".join(notes) + "."]
    return "\n".join(out) + "\n"


def finish_reply(
    md: Path, analysis: Analysis, job_id: str, flags: dict[str, int], enabled: bool
) -> dict[str, Any]:
    cwd = Path.cwd()
    copy, note = preview_copy(md, analysis, job_id, cwd) if enabled else (None, "")
    return {
        "preview": str(copy) if copy else None,
        "preview_note": note or None,
        "reply_markdown": reply_markdown(analysis, md, copy, cwd, flags),
    }
