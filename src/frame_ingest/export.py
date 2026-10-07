"""`export`: copy a finished document out of the job directory (PLAN T6).

The destination is the one place the CLI writes outside <home>, so it is the most constrained:
it must sit inside a root the *user* listed in config (`export_roots`, never an argument),
roots themselves may not be system paths or hidden directories in the home directory, hidden
path components are refused, symlinks are refused, and nothing is overwritten.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml

from frame_ingest.config import AppConfig
from frame_ingest.errors import FrameIngestError
from frame_ingest.guard.paths import is_within, job_jail
from frame_ingest.storage import atomic_write_text, file_stem

Style = Literal["markdown", "obsidian"]
_SYSTEM = (
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/System",
    "/Library",
    "/private/etc",
    "/dev",
    "/proc",
    "/sys",
    "/boot",
    "/root",
    "/var/root",
)


class ExportRefused(FrameIngestError):
    code = "export_refused"
    status = 403


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path.expanduser()))


def _check_root(root: Path) -> Path:
    real = _real(root)
    home = _real(Path.home())
    if real == Path(real.anchor) or any(
        real == Path(s) or Path(s) in real.parents for s in _SYSTEM
    ):
        raise ExportRefused(f"export root {str(root)[:80]!r} is a system location.")
    if home in real.parents and real.relative_to(home).parts[0].startswith("."):
        raise ExportRefused(f"export root {str(root)[:80]!r} is a hidden directory in your home.")
    return real


def resolve_destination(config: AppConfig, target: str) -> Path:
    if not config.export_roots:
        raise ExportRefused(
            "No export location is configured. Add one to config.yaml first, for example:\n"
            "  export_roots: [~/Documents/Notes]\n"
            "(a location you choose is never taken from a command-line argument)."
        )
    roots = [_check_root(r) for r in config.export_roots]
    raw = Path(target).expanduser()
    dest = raw if raw.is_absolute() else roots[0] / raw
    real = _real(dest)
    if not any(real == r or r in real.parents for r in roots):
        raise ExportRefused("The destination is outside the configured export_roots.")
    root = next(r for r in roots if real == r or r in real.parents)
    if any(part.startswith(".") for part in real.relative_to(root).parts):
        raise ExportRefused("Hidden directories are not valid export destinations.")
    return real


def render_obsidian(md: str) -> str:
    """Add Obsidian properties (tags, aliases) to the existing YAML frontmatter."""
    if not md.startswith("---\n") or "\n---\n" not in md[4:]:
        return md
    head, body = md[4:].split("\n---\n", 1)
    front = yaml.safe_load(head) or {}
    if not isinstance(front, dict):
        return md
    tags = [str(t) for t in front.get("tags") or []]
    front["tags"] = [*tags, "frame-ingest"] if "frame-ingest" not in tags else tags
    title = front.get("title")
    if isinstance(title, str):
        front["aliases"] = [title]
    front["cssclasses"] = ["frame-ingest"]
    return (
        "---\n"
        + yaml.safe_dump(front, sort_keys=False, allow_unicode=True).rstrip()
        + "\n---\n"
        + body
    )


def export_document(
    config: AppConfig, md_path: Path, json_path: Path | None, target: str, style: Style
) -> list[Path]:
    dest_dir = resolve_destination(config, target)
    stem = file_stem(md_path.name.removesuffix(".analysis.md"))
    text = md_path.read_text(encoding="utf-8")
    if style == "obsidian":
        text = render_obsidian(text)
    files = [(dest_dir / f"{stem}.md", text)]
    if json_path is not None:
        files.append((dest_dir / f"{stem}.json", json_path.read_text(encoding="utf-8")))
    written: list[Path] = []
    dest_dir.mkdir(parents=True, exist_ok=True)
    with job_jail(dest_dir):
        for path, content in files:
            if path.exists() or path.is_symlink():
                raise ExportRefused(
                    f"{path.name} already exists in the destination; not overwriting."
                )
            if not is_within(path, dest_dir):  # pragma: no cover - names are sanitised
                raise ExportRefused("Refusing a path outside the destination.")
            atomic_write_text(path, content)
            written.append(path)
    return written
