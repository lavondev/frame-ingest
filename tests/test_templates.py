"""Templates and `check`: the agent fills prefilled files and checks each one on the spot.

A template is schema-shaped from the start but holds `TODO:` strings, so it checks as
`incomplete` until filled; a filled one passes; and each typical mistake (a renamed frame, an extra
or missing segment id, a gap between chapters) comes back as exactly one problem that names the
file, the JSON path and the rule.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from frame_ingest.agent.checks import find_placeholders
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


def cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    code = main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


@pytest.fixture
def job(
    home: Path, sample_video: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> dict[str, Any]:
    srt = tmp_path / "sample.srt"
    srt.write_text(SRT, encoding="utf-8")
    code, data = cli(capsys, "prepare", str(sample_video), "--captions", str(srt))
    assert code == 0, data
    return json.loads(Path(data["manifest"]).read_text(encoding="utf-8"))


def load(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path: str | Path, data: Any) -> None:
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def filled(data: Any) -> Any:
    """What an agent does to a template: replace every TODO and drop the todo line."""
    if isinstance(data, dict):
        return {k: filled(v) for k, v in data.items() if k != "todo"}
    if isinstance(data, list):
        return [filled(v) for v in data]
    if isinstance(data, str) and data.startswith("TODO"):
        return "Filled in by the agent."
    return data


def fill_all(m: dict[str, Any]) -> None:
    t = m["templates"]
    for path in [v["file"] for v in t["vision"]] + [t["corrections"], t["synthesis"]]:
        save(path, filled(load(path)))


# ── what prepare writes ─────────────────────────────────────────────────────────
def test_templates_cover_every_frame_segment_and_the_whole_duration(job: dict[str, Any]) -> None:
    t = job["templates"]
    assert len(t["vision"]) == len(job["sheets"])
    names = [e["frame"] for v in t["vision"] for e in load(v["file"])["frames"]]
    assert names == [f["name"] for f in job["frames"]]  # every frame, in order, exactly once
    assert [v["frames"] for v in t["vision"]] == [s["frames"] for s in job["sheets"]]

    corr = load(t["corrections"])
    tr = load(job["transcript"]["file"])
    assert [(s["id"], s["corrected_text"]) for s in corr["segments"]] == [
        (s["id"], s["raw_text"]) for s in tr["segments"]
    ]
    assert corr["todo"].startswith("TODO:")

    chapters = load(t["synthesis"])["chapters"]
    assert chapters[0]["start"] == 0 and chapters[-1]["end"] == round(job["video"]["duration_s"], 2)
    assert all(a["end"] == b["start"] for a, b in zip(chapters, chapters[1:], strict=False))


def test_every_untouched_template_checks_as_incomplete(
    job: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    t = job["templates"]
    for path in [v["file"] for v in t["vision"]] + [t["corrections"], t["synthesis"]]:
        code, res = cli(capsys, "check", path)
        assert code == 1 and res["status"] == "incomplete" and res["ok"] is False, res
        assert res["problems"] == [] and res["todo"]["count"] > 0
        assert all(p.startswith("$") for p in res["todo"]["paths"])
    code, res = cli(capsys, "check", job["job_id"])
    assert code == 1 and res["status"] == "incomplete"
    code, res = cli(capsys, "assemble", job["job_id"])  # left-over placeholders are refused
    assert code == 1 and {p["rule"] for p in res["problems"]} == {"placeholder"}


def test_filled_templates_pass_check_and_assemble(
    job: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    fill_all(job)
    t = job["templates"]
    for path in [v["file"] for v in t["vision"]] + [t["corrections"], t["synthesis"]]:
        code, res = cli(capsys, "check", path)
        assert code == 0 and res["status"] == "ok" and res["problems"] == [], res
    assert cli(capsys, "check", job["job_id"])[1]["status"] == "ok"
    code, res = cli(capsys, "assemble", job["job_id"])
    assert code == 0, res


def test_check_is_fast(job: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    fill_all(job)
    start = time.perf_counter()
    cli(capsys, "check", job["templates"]["synthesis"])
    assert time.perf_counter() - start < 1.0


# ── one mistake, one precise problem ────────────────────────────────────────────
def only_problem(capsys: pytest.CaptureFixture[str], path: str) -> dict[str, str]:
    code, res = cli(capsys, "check", path)
    assert code == 1 and res["status"] == "invalid", res
    assert len(res["problems"]) == 1, res["problems"]
    return dict(res["problems"][0])


def test_a_renamed_frame_is_one_problem(
    job: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    fill_all(job)
    path = job["templates"]["vision"][0]["file"]
    data = load(path)
    real = data["frames"][1]["frame"]
    data["frames"][1]["frame"] = "frame_9999.99.jpg"
    save(path, data)
    p = only_problem(capsys, path)
    assert (p["rule"], p["path"], p["file"]) == (
        "unknown_frame",
        "$.frames[1]",
        "vision/batch-01.json",
    )
    assert real in p["message"]


@pytest.mark.parametrize("mistake", ["extra", "missing"])
def test_an_extra_or_missing_segment_id_is_one_problem(
    mistake: str, job: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    fill_all(job)
    path = job["templates"]["corrections"]
    data = load(path)
    if mistake == "extra":
        data["segments"].append({"id": 99, "corrected_text": "Invented."})
    else:
        data["segments"].pop(1)
    save(path, data)
    p = only_problem(capsys, path)
    assert p["rule"] == "bad_ids" and p["file"] == "corrections.json"
    if mistake == "extra":
        assert p["path"] == "$.segments[3].id" and "unexpected ids [99]" in p["message"]
    else:
        assert p["path"] == "$.segments" and "missing ids [1]" in p["message"]


def test_a_chapter_gap_is_one_problem(
    job: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    fill_all(job)
    path = job["templates"]["synthesis"]
    data = load(path)
    assert len(data["chapters"]) >= 2
    data["chapters"][1]["start"] += 3
    save(path, data)
    p = only_problem(capsys, path)
    assert (p["rule"], p["path"]) == ("bad_chapters", "$.chapters[1].start")
    assert "gap of 3.0s between chapters 1 and 2" in p["message"]


def test_check_warns_about_quotes_that_will_be_dropped(
    job: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    fill_all(job)
    path = job["templates"]["synthesis"]
    data = load(path)
    data["chapters"][0]["quotes"] = [
        {"t": 6, "text": "Open the dashboard first."},
        {"t": 7, "text": "Words nobody said."},
    ]
    save(path, data)
    code, res = cli(capsys, "check", path)
    assert code == 0 and res["warnings"] == [
        "1 quote(s) are not verbatim in the transcript and will be dropped"
    ]


def test_a_placeholder_typed_by_hand_is_never_accepted() -> None:
    assert find_placeholders({"a": ["TODO", "todo list", "TODO: x"], "b": " TODO: y"}) == [
        "$.a[0]",
        "$.a[2]",
        "$.b",
    ]


# ── templates never overwrite the agent's work ──────────────────────────────────
def test_edited_templates_survive_a_new_prepare_and_drill_frames_get_their_own_batch(
    job: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    t = job["templates"]
    fill_all(job)
    before = {p: Path(p).read_text() for p in [t["synthesis"], t["vision"][0]["file"]]}
    code, data = cli(
        capsys, "prepare", "--job", job["job_id"], "--dense", "--start", "6", "--end", "10"
    )
    assert code == 0 and data["drill_frames_added"] >= 2
    m = load(data["manifest"])
    assert {p: Path(p).read_text() for p in before} == before
    new = m["templates"]["vision"][-1]
    assert new["file"].endswith(f"batch-{len(t['vision']) + 1:02d}.json")
    assert len(new["frames"]) == data["drill_frames_added"]
    code, res = cli(capsys, "check", job["job_id"])
    assert res["status"] == "incomplete"  # only the new batch is left to fill
    save(new["file"], filled(load(new["file"])))
    assert cli(capsys, "check", job["job_id"])[1]["status"] == "ok"


def test_untouched_vision_templates_follow_the_sheets_after_a_drill_down(
    job: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code, data = cli(
        capsys, "prepare", "--job", job["job_id"], "--dense", "--start", "6", "--end", "10"
    )
    assert code == 0
    m = load(data["manifest"])
    assert [v["frames"] for v in m["templates"]["vision"]] == [s["frames"] for s in m["sheets"]]


# ── check reads only agent files ────────────────────────────────────────────────
def test_check_refuses_anything_outside_a_jobs_agent_out(
    job: dict[str, Any], home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = tmp_path / "synthesis.json"
    secret.write_text('{"title": "sk-live-SECRET"}')
    job_json = home / "jobs" / job["job_id"] / "job.json"
    link = Path(job["directories"]["out"]) / "vision" / "batch-77.json"
    link.symlink_to(secret)
    for target in (str(secret), str(job_json), str(link), "/etc/passwd"):
        code, res = cli(capsys, "check", target)
        assert code == 3 and res["error"]["code"] == "invalid_input", target
        assert "SECRET" not in json.dumps(res)
