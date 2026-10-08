"""The state machine: `ingest` -> fill templates -> `check` -> `finish`, with `next` able to
pick the job up again from what is on disk. Also the job-id regression: the same input gets the
same job id from every command.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import types
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

3
00:00:13,000 --> 00:00:20,000
Now tune the cache size.
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fi-home"
    for name in list(os.environ):
        if name.startswith(("FRAME_INGEST_", "OPENAI_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FRAME_INGEST_HOME", str(home))
    return home


@pytest.fixture
def srt(tmp_path: Path) -> Path:
    path = tmp_path / "sample.srt"
    path.write_text(SRT, encoding="utf-8")
    return path


@pytest.fixture
def launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """What the skill's launcher exports: its own absolute path."""
    fi = tmp_path / "My Skills" / "frame-ingest" / "scripts" / "fi"
    fi.parent.mkdir(parents=True)
    fi.write_text("#!/bin/sh\n")
    monkeypatch.setenv("FRAME_INGEST_LAUNCHER", str(fi))
    return str(fi)


def cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    code = main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def run_next(capsys: pytest.CaptureFixture[str], card: dict[str, Any]) -> tuple[int, Any]:
    """Run a card's `next` command exactly as an agent would (minus the shell)."""
    argv = shlex.split(card["next"])
    assert argv[0] == card["fi_path"] and argv[-1] == "--json"
    return cli(capsys, *argv[1:-1])


def fill(card: dict[str, Any]) -> None:
    """The judgment work, done the way a careful agent would: every TODO replaced."""

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items() if k != "todo"}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return "Seen and described." if isinstance(v, str) and v.startswith("TODO") else v

    for row in card["fill"]:
        path = Path(row["file"])
        data = walk(json.loads(path.read_text(encoding="utf-8")))
        if row["kind"] == "corrections":
            for seg in data["segments"]:
                seg["corrected_text"] = seg["corrected_text"].replace("Widjet", "Widget")
        if row["kind"] == "synthesis":
            data["chapters"][0]["quotes"] = [{"t": 6, "text": "Open the dashboard first."}]
        path.write_text(json.dumps(data), encoding="utf-8")


# ── job ids ─────────────────────────────────────────────────────────────────────
def test_the_same_input_gets_the_same_job_id_from_every_command(
    home: Path,
    sample_video: Path,
    tmp_path: Path,
    srt: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: `estimate` and `prepare` used to print different ids for one video."""
    ids = {
        cli(capsys, "estimate", str(sample_video), "--profile", "agent")[1]["job_id"],
        cli(capsys, "prepare", str(sample_video), "--captions", str(srt))[1]["job_id"],
        cli(capsys, "ingest", str(sample_video), "--captions", str(srt))[1]["job_id"],
    }
    renamed = tmp_path / "another name.mp4"
    renamed.write_bytes(sample_video.read_bytes())
    ids.add(cli(capsys, "estimate", str(renamed), "--profile", "agent")[1]["job_id"])
    assert len(ids) == 1
    assert [p.name for p in (home / "jobs").iterdir()] == list(ids)

    code, _ = cli(capsys, "probe", str(sample_video))  # probing must not delete the job
    assert code == 0 and (home / "jobs" / ids.pop() / "job.json").is_file()


def test_a_different_video_gets_a_different_job(
    home: Path, sample_video: Path, silent_video: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = cli(capsys, "estimate", str(sample_video), "--profile", "agent")[1]["job_id"]
    b = cli(capsys, "estimate", str(silent_video), "--profile", "agent")[1]["job_id"]
    assert a != b


# ── the whole loop through the task card ────────────────────────────────────────
def test_ingest_check_finish_with_next_driving(
    home: Path,
    sample_video: Path,
    srt: Path,
    launcher: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, card = cli(capsys, "ingest", str(sample_video), "--captions", str(srt))
    assert code == 0 and card["state"] == "fill" and card["fi_path"] == launcher
    assert card["mode"] == "agent" and card["coverage"]["audio"] == "yes"
    assert card["read"]["sheets"] and card["read"]["transcript"]
    assert {r["kind"] for r in card["fill"]} == {"vision-batch", "corrections", "synthesis"}
    assert all(r["status"] == "incomplete" for r in card["fill"])
    assert card["next"].startswith(shlex.quote(launcher) + " check ")  # spaces in the path quoted
    assert card["estimate"]["frames"] >= 4 and card["fill"][0]["read"]  # the sheet to look at

    code, res = run_next(capsys, card)  # check on an untouched template
    assert code == 1 and res["status"] == "incomplete"

    fill(card)
    code, card = cli(capsys, "next", card["job_id"])
    assert code == 0 and card["state"] == "finish"
    assert card["next"] == f"{shlex.quote(launcher)} finish {card['job_id']} --json"

    code, done = run_next(capsys, card)
    assert code == 0 and done["ok"] and done["state"] == "done", done
    md = Path(done["document"]).read_text(encoding="utf-8")
    assert "Welcome to the Widget Frobnicator." in md and "audio: 'yes'" in md
    assert done["coverage_line"].startswith("**Coverage:** audio yes · transcript captions")
    assert done["tldr"] and done["chapters"][0]["anchor"] == "ch-01"
    assert done["validate"] == {"ok": True, "issues": []} and done["scan"]["flags"] == {}
    assert "coverage line" in done["reply"]

    code, card = cli(capsys, "next", card["job_id"])
    assert card["state"] == "done" and card["next"] is None and card["document"] == done["document"]


def test_next_notices_an_edit_after_finish(
    home: Path, sample_video: Path, srt: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, card = cli(capsys, "ingest", str(sample_video), "--captions", str(srt))
    fill(card)
    assert cli(capsys, "finish", card["job_id"])[0] == 0
    syn = next(Path(r["file"]) for r in card["fill"] if r["kind"] == "synthesis")
    data = json.loads(syn.read_text(encoding="utf-8"))
    data["title"] = "TODO: rename"
    syn.write_text(json.dumps(data), encoding="utf-8")
    os.utime(syn)  # make sure it is newer than the document
    _, card = cli(capsys, "next", card["job_id"])
    assert card["state"] == "fill" and card["next"].endswith(f"check {syn} --json")


def test_finish_with_problems_points_at_the_file_to_check(
    home: Path, sample_video: Path, srt: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, card = cli(capsys, "ingest", str(sample_video), "--captions", str(srt))
    code, res = cli(capsys, "finish", card["job_id"])
    assert code == 1 and res["state"] == "fill" and res["problems"]
    assert {p["rule"] for p in res["problems"]} == {"placeholder"}
    assert " check " in res["next"]


def test_next_on_a_job_that_was_only_estimated_says_ingest(
    home: Path, sample_video: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = cli(capsys, "estimate", str(sample_video), "--profile", "agent")[1]["job_id"]
    code, card = cli(capsys, "next", job)
    assert code == 0 and card["state"] == "prepare"
    assert shlex.split(card["next"])[1:] == ["ingest", "--job", job, "--json"]
    assert cli(capsys, "next", "abcdef123456")[0] == 5


def test_ingest_stops_for_a_decision_and_lists_the_command_for_each_choice(
    home: Path, sample_video: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, card = cli(capsys, "ingest", str(sample_video))  # no captions, no speech extra
    assert code == 6 and card["state"] == "needs_decision" and card["next"] is None
    after = card["after_decision"]
    job = card["job_id"]
    assert shlex.split(after["frames_only"])[1:] == [
        "ingest",
        "--job",
        job,
        "--allow-frames-only",
        "--json",
    ]
    assert "--allow-egress" in after["cloud_speech"] and "install_speech" in after
    assert "fill" not in card  # nothing to fill before the user decides
    assert cli(capsys, "next", job)[1]["state"] == "needs_decision"

    code, card = run_next(capsys, {**card, "next": after["frames_only"]})
    assert code == 0 and card["state"] == "fill" and card["coverage"]["audio"] == "no"


def test_ingest_reports_a_broken_install_instead_of_running(
    home: Path, sample_video: Path, capsys: pytest.CaptureFixture[str], monkeypatch: Any
) -> None:
    from frame_ingest.doctor import DoctorReport, HealthCheck

    async def broken(*_a: Any, **_k: Any) -> DoctorReport:
        bad = HealthCheck(name="ffmpeg", ok=False, message="ffmpeg is missing")
        return DoctorReport(ok=False, key_present=False, base_url=None, checks=[bad])

    monkeypatch.setattr("frame_ingest.cli.run_doctor", broken)
    code, res = cli(capsys, "ingest", str(sample_video))
    assert code == 4 and res["state"] == "doctor" and "ffmpeg is missing" in res["error"]["message"]
    assert not (home / "jobs").exists() or not list((home / "jobs").iterdir())


def test_fi_path_ignores_a_launcher_variable_that_is_not_a_launcher(
    home: Path, sample_video: Path, srt: Path, capsys: pytest.CaptureFixture[str], monkeypatch: Any
) -> None:
    for bad in ("scripts/fi", "/bin/sh", "/nonexistent/fi"):
        monkeypatch.setenv("FRAME_INGEST_LAUNCHER", bad)
        _, card = cli(capsys, "ingest", str(sample_video), "--captions", str(srt))
        assert card["fi_path"] != bad


# ── speech end to end: the document says audio yes ──────────────────────────────
def test_a_video_with_speech_ends_with_audio_yes(
    home: Path, sample_video: Path, capsys: pytest.CaptureFixture[str], monkeypatch: Any
) -> None:
    class Whisper:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def transcribe(self, *_a: Any, **_k: Any) -> tuple[Any, Any]:
            segs = [
                types.SimpleNamespace(
                    start=1.0, end=5.0, text="Welcome to the Widjet Frobnicator."
                ),
                types.SimpleNamespace(start=6.0, end=11.0, text="Open the dashboard first."),
            ]
            return iter(segs), types.SimpleNamespace(language="en")

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = Whisper  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    code, card = cli(capsys, "ingest", str(sample_video))
    assert code == 0 and card["coverage"]["transcript_source"] == "asr"
    fill(card)
    code, done = cli(capsys, "finish", card["job_id"])
    assert code == 0 and done["coverage"]["audio"] == "yes"
    assert "audio: 'yes'" in Path(done["document"]).read_text(encoding="utf-8")


# ── stale evidence ──────────────────────────────────────────────────────────────
def test_changing_the_frame_cap_reselects_frames(
    home: Path, sample_video: Path, srt: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, card = cli(capsys, "ingest", str(sample_video), "--captions", str(srt))
    many = card["estimate"]["frames"]
    _, card = cli(capsys, "ingest", str(sample_video), "--captions", str(srt), "--frame-cap", "2")
    assert card["estimate"]["frames"] == 2 < many
