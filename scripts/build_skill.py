#!/usr/bin/env python3
"""Build `frame-ingest-<version>.skill`: a deterministic zip of skills/frame-ingest/ with the
skill directory as its single top-level folder (the layout skill installers expect).

Same inputs give the same bytes (sorted entries, fixed timestamps, normalised modes), so the
published checksum can be reproduced from the tagged source.
    python scripts/build_skill.py dist/
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "frame-ingest"
FIXED_TIME = (2026, 1, 1, 0, 0, 0)
SKIP = {"__pycache__", ".DS_Store"}


def version() -> str:
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split('"')[1]
    raise SystemExit("version not found in pyproject.toml")


def build(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"frame-ingest-{version()}.skill"
    files = sorted(p for p in SKILL.rglob("*") if p.is_file() and not (set(p.parts) & SKIP))
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            if path.is_symlink():
                raise SystemExit(f"refusing to package a symlink: {path}")
            rel = path.relative_to(SKILL.parent)  # frame-ingest/...
            info = zipfile.ZipInfo(rel.as_posix(), FIXED_TIME)
            executable = path.stat().st_mode & 0o111
            info.external_attr = (0o755 if executable else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
    return target


if __name__ == "__main__":
    print(build(Path(sys.argv[1] if len(sys.argv) > 1 else "dist")))
