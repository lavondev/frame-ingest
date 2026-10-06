"""Command-line entry point.

Pre-alpha: only ``--version`` exists. The planned command surface is documented in
docs/PLAN.md (section 3.2). No command may run an external process until the guard layer
(docs/PLAN.md, milestone M2) exists.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from frame_ingest import __version__

PLANNED_COMMANDS = (
    "doctor",
    "fetch",
    "probe",
    "prepare",
    "estimate",
    "run",
    "assemble",
    "validate",
    "scan",
    "clean",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="frame-ingest",
        description="Turn a video (file or URL) into a structured, citable Markdown document.",
        epilog=(
            "Pre-alpha: no commands are implemented yet. Planned: " + ", ".join(PLANNED_COMMANDS)
        ),
    )
    parser.add_argument("--version", action="version", version=f"frame-ingest {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
