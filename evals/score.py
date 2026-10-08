"""Score an eval run: did each case end the way evals/cases.json says it should?

    uv run python evals/score.py                   # fixtures in evals/out, jobs in ~/.frame-ingest
    uv run python evals/score.py --out DIR --json

Run it after an agent (Claude Code, Codex, ...) has processed the fixtures with the skill. A job
id is the first 12 hex digits of the video's sha256, so each fixture's job is found without
asking the agent anything. Exit code 0 when every check passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # `uv run python evals/score.py` from the repo root

from evals.make_fixtures import load_cases  # noqa: E402


@dataclass
class Check:
    case: str
    name: str
    ok: bool
    detail: str = ""


def job_id(video: Path) -> str:
    return hashlib.sha256(video.read_bytes()).hexdigest()[:12]


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def score_case(case: dict[str, Any], fixtures: Path, home: Path) -> list[Check]:
    from frame_ingest.agent.validate_doc import validate_document

    name = case["id"]
    expect = case["expect"]
    video = fixtures / f"{name}.mp4"
    if not video.is_file():
        return [Check(name, "fixture exists", False, f"{video} is missing; run make_fixtures")]
    jdir = home / "jobs" / job_id(video)
    checks = [Check(name, "ingested", (jdir / "agent" / "manifest.json").is_file(), str(jdir))]
    if not checks[0].ok:
        return checks
    audio = _json(jdir / "agent" / "audio.json") or {}
    attempts = len(audio.get("attempts", []))
    job = _json(jdir / "job.json") or {}
    md_rel = (job.get("outputs") or {}).get("md")
    md = (jdir / md_rel).read_text(encoding="utf-8") if md_rel else None

    if expect["ingest_state"] == "needs_decision":
        decided = audio.get("status") == "frames_only"
        checks.append(
            Check(
                name,
                "stopped for a decision",
                audio.get("reason") == expect["decision_reason"],
                f"reason={audio.get('reason')}",
            )
        )
        checks.append(
            Check(name, "speech attempts", attempts == expect["speech_attempts"], str(attempts))
        )
        if md is not None:  # the user chose frames only and the agent finished
            want = expect["after_frames_only"]
            checks.append(Check(name, "user decision recorded", decided, audio.get("status", "")))
            checks.append(Check(name, "audio: no", "audio: 'no'" in md))
            checks.append(Check(name, "Frames only banner", f"**{want['banner']}.**" in md))
    else:
        checks.append(
            Check(name, "transcript made", audio.get("status") == "ok", audio.get("message", ""))
        )
        checks.append(Check(name, "speech attempts", attempts in (1, 2), str(attempts)))
        if md is None:
            checks.append(Check(name, "document built", False, "no finished document"))
            return checks
        checks.append(Check(name, "audio: yes", "audio: 'yes'" in md))
        checks.append(
            Check(
                name,
                f"transcript_source: {expect['transcript_source']}",
                f"transcript_source: {expect['transcript_source']}" in md,
            )
        )
    if md is not None:
        issues = validate_document(md)
        checks.append(Check(name, "document validates", not issues, json.dumps(issues)[:200]))
        for text in expect.get("on_screen_text", []):
            checks.append(Check(name, f"on-screen text {text!r}", text.lower() in md.lower()))
    return checks


def main(argv: list[str] | None = None) -> int:
    from frame_ingest.config import default_home

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=HERE / "out", help="fixture folder")
    parser.add_argument("--home", type=Path, default=None, help="frame-ingest data folder")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    args = parser.parse_args(argv)
    home = args.home or default_home()
    checks = [c for case in load_cases() for c in score_case(case, args.out, home)]
    passed = all(c.ok for c in checks)
    if args.json:
        print(json.dumps({"ok": passed, "checks": [c.__dict__ for c in checks]}, indent=2))
    else:
        for c in checks:
            print(f"{'PASS' if c.ok else 'FAIL'}  {c.case:<24} {c.name}  {c.detail}".rstrip())
        print("all passed" if passed else "some checks failed")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
