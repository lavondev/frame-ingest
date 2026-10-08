"""The end of a run: a previewable copy in ./frame-ingest-out/ and a reply built in code.

The reply is the same compact summary every time (title, a link the harness can open, TL;DR,
chapter table, coverage line), so it does not depend on how a model chooses to format it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from frame_ingest.cli import main

SRT = """1
00:00:01,000 --> 00:00:05,000
Welcome to the Widjet Frobnicator.

2
00:00:06,000 --> 00:00:11,000
Open the dashboard first.
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fi-home"
    for name in list(os.environ):
        if name.startswith(("FRAME_INGEST_", "OPENAI_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FRAME_INGEST_HOME", str(home))
    return home


def cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    code = main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def fill(card: dict[str, Any], title: str = "Widget Frobnicator setup") -> None:
    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items() if k != "todo"}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return "Described." if isinstance(v, str) and v.startswith("TODO") else v

    for row in card["fill"]:
        path = Path(row["file"])
        data = walk(json.loads(path.read_text(encoding="utf-8")))
        if row["kind"] == "synthesis":
            data["title"] = title
            data["tldr"] = "How to set up the Frobnicator."
            for i, c in enumerate(data["chapters"]):
                c["title"] = f"Part {i + 1}"
        path.write_text(json.dumps(data), encoding="utf-8")


def finished(
    capsys: pytest.CaptureFixture[str], video: Path, tmp_path: Path, **kw: str
) -> dict[str, Any]:
    srt = tmp_path / "s.srt"
    srt.write_text(SRT, encoding="utf-8")
    _, card = cli(capsys, "ingest", str(video), "--captions", str(srt))
    fill(card, **kw)
    code, done = cli(capsys, "finish", card["job_id"])
    assert code == 0, done
    return done


def test_the_reply_is_the_compact_summary_with_a_link_the_harness_can_open(
    home: Path, sample_video: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    done = finished(capsys, sample_video, tmp_path)
    copy = Path.cwd() / "frame-ingest-out" / "sample.md"
    assert done["preview"] == str(copy.resolve()) and done["preview_note"] is None
    assert copy.read_text(encoding="utf-8") == Path(done["document"]).read_text(encoding="utf-8")

    lines = done["reply_markdown"].splitlines()
    assert lines[0] == "## Widget Frobnicator setup"
    assert lines[2] == "[sample.md](frame-ingest-out/sample.md)"  # relative, never claude.ai/...
    assert lines[4] == "**TL;DR** How to set up the Frobnicator."
    assert lines[6:8] == ["| # | Chapter | Time |", "|---|---|---|"]
    rows = [ln for ln in lines if ln.startswith("| ") and ln[2].isdigit()]
    assert rows[0] == "| 1 | Part 1 | 00:00 - 00:12 |" and len(rows) == len(done["chapters"])
    assert lines[-1].startswith("Coverage: audio yes · transcript captions · frames ")
    assert "Note:" not in done["reply_markdown"] and "#ch-" not in done["reply_markdown"]


def test_finishing_again_replaces_its_own_copy_but_never_another_videos(
    home: Path,
    sample_video: Path,
    silent_video: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = Path.cwd() / "frame-ingest-out"
    first = finished(capsys, sample_video, tmp_path)
    again = finished(capsys, sample_video, tmp_path)
    assert again["preview"] == first["preview"] and len(list(out.iterdir())) == 1

    other = tmp_path / "sample.mp4"  # a different video with the same file name
    other.write_bytes(silent_video.read_bytes())
    _, card = cli(capsys, "ingest", str(other), "--allow-frames-only")
    fill(card)
    _, third = cli(capsys, "finish", card["job_id"])
    assert third["preview"].endswith(f"sample-{card['job_id']}.md")
    assert (out / "sample.md").read_text() == Path(first["document"]).read_text()
    assert (
        "Note: frames only, no transcript (the video has no audio track)."
        in third["reply_markdown"]
    )


def test_no_copy_through_a_symlink_or_when_switched_off(
    home: Path,
    sample_video: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (Path.cwd() / "frame-ingest-out").symlink_to(elsewhere)
    done = finished(capsys, sample_video, tmp_path)
    assert done["preview"] is None and "not a plain folder" in done["preview_note"]
    assert list(elsewhere.iterdir()) == []
    assert done["reply_markdown"].splitlines()[2] == f"`{done['document']}`"

    (Path.cwd() / "frame-ingest-out").unlink()
    monkeypatch.setenv("FRAME_INGEST_PREVIEW_COPY", "false")
    done = finished(capsys, sample_video, tmp_path)
    assert done["preview"] is None and not (Path.cwd() / "frame-ingest-out").exists()


def test_video_text_cannot_reshape_the_reply(
    home: Path, sample_video: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    done = finished(
        capsys, sample_video, tmp_path, title="x | y\n## Pwned [click](https://evil.example)"
    )
    reply = done["reply_markdown"]
    assert reply.count("\n## ") == 0 and reply.startswith("## ")
    assert "](https://evil" not in reply.replace("\\](https://evil", "")
