from __future__ import annotations

import io
import json
import os
import tomllib
from pathlib import Path

import pytest

from frame_ingest import __version__
from frame_ingest.cli import main

ROOT = Path(__file__).resolve().parent.parent


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_args_prints_help_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "frame-ingest" in capsys.readouterr().out


def test_package_version_matches_pyproject() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == __version__


# ── M1 commands ──────────────────────────────────────────────────────────────
GOLDEN = ROOT / "tests" / "golden" / "analysis.md"
FAKE_API_KEY = "sk-test-FAKE_API_KEY-1234567890"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated FRAME_INGEST_HOME; the caller's real environment cannot leak in."""
    home = tmp_path / "fi-home"
    for name in list(os.environ):
        if name.startswith(("FRAME_INGEST_", "OPENAI_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FRAME_INGEST_HOME", str(home))
    return home


def invoke(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def jobs(home: Path) -> list[Path]:
    root = home / "jobs"
    return sorted(root.iterdir()) if root.is_dir() else []


def headings(md: str) -> list[str]:
    return [line.split(" {#")[0] for line in md.splitlines() if line.startswith("## ")]


def test_run_fake_produces_a_conformant_document(
    home: Path, sample_video: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = invoke(capsys, "run", str(sample_video), "--profile", "fake", "--json")
    assert code == 0
    data = json.loads(out)  # stdout is exactly one JSON object
    assert data["ok"] is True and data["status"] == "completed" and data["profile"] == "fake"
    md_path, json_path = Path(data["outputs"]["md"]), Path(data["outputs"]["json"])
    assert md_path.is_file() and json_path.is_file()
    assert Path(data["job_dir"]).parent == home / "jobs"
    assert "[assemble] done" in err  # progress goes to stderr

    md = md_path.read_text(encoding="utf-8")
    golden = GOLDEN.read_text(encoding="utf-8")
    assert md.startswith("---\n") and md.count("\n---\n") >= 1
    # Same document skeleton as the golden file (chapter titles and counts differ per video).
    fixed = [h for h in headings(golden) if not h.startswith("## Chapter")]
    assert [h for h in headings(md) if not h.startswith("## Chapter")] == fixed
    assert any(h.startswith("## Chapter 1:") for h in headings(md))
    assert data["chapter_count"] == sum(h.startswith("## Chapter") for h in headings(md))
    assert json.loads(json_path.read_text(encoding="utf-8"))


def test_run_human_output_names_the_document(
    home: Path, sample_video: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = invoke(capsys, "run", str(sample_video), "--profile", "fake")
    assert code == 0
    assert any(line.startswith("md: ") and line.endswith(".md") for line in out.splitlines())


def test_estimate_then_resume_the_same_job(
    home: Path, sample_video: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = invoke(capsys, "estimate", str(sample_video), "--profile", "fake", "--json")
    assert code == 0
    est = json.loads(out)
    assert est["egress"]["network"] is False
    assert est["estimate"]["frames"] >= 1 and est["estimate"]["total_api_calls"] >= 1
    assert len(jobs(home)) == 1

    code, out, _ = invoke(capsys, "run", "--job", est["job_id"], "--profile", "fake", "--json")
    assert code == 0 and json.loads(out)["job_id"] == est["job_id"]
    assert len(jobs(home)) == 1  # resumed, not duplicated


def test_probe_reports_the_video_and_leaves_no_job(
    home: Path, sample_video: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = invoke(capsys, "probe", str(sample_video), "--json")
    assert code == 0
    video = json.loads(out)["video"]
    assert video["has_audio"] is True and video["width"] == 320 and video["height"] == 240
    assert 23 < video["duration_s"] < 25
    assert jobs(home) == []


def test_input_dash_reads_the_path_from_stdin(
    home: Path,
    sample_video: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{sample_video}\n"))
    code, out, _ = invoke(capsys, "probe", "-", "--json")
    assert code == 0 and json.loads(out)["ok"] is True


# ── input is untrusted ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw",
    ["https://example.com/v.mp4", "file:///etc/passwd", "ftp://host/x", "rtmp://host/live"],
)
def test_urls_are_rejected_before_anything_runs(
    raw: str, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = invoke(capsys, "run", raw, "--profile", "fake", "--json")
    assert code == 3
    assert json.loads(out)["error"]["code"] == "invalid_input"
    assert jobs(home) == []


def test_missing_file_and_directory_are_rejected(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert invoke(capsys, "probe", str(tmp_path / "nope.mp4"))[0] == 3
    assert invoke(capsys, "probe", str(tmp_path))[0] == 3


def test_symlinks_are_refused(
    home: Path, sample_video: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    link = tmp_path / "link.mp4"
    link.symlink_to(sample_video)
    code, out, _ = invoke(capsys, "probe", str(link), "--json")
    assert code == 3 and "symlink" in json.loads(out)["error"]["message"]


def test_hostile_input_is_quoted_in_errors(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = invoke(capsys, "probe", "bad\x1b[2Jname.mp4")
    assert code == 3
    assert "\x1b" not in err  # repr() escapes control characters


def test_a_media_file_that_is_not_media_is_rejected(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video at all")
    code, _, _ = invoke(capsys, "probe", str(junk))
    assert code == 3
    assert jobs(home) == []  # the failed copy is cleaned up


# ── profiles, usage, exit codes ──────────────────────────────────────────────
@pytest.mark.parametrize("profile", ["cloud", "local", "agent"])
def test_unimplemented_profiles_fail_loudly_without_egress(
    profile: str,
    home: Path,
    sample_video: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_API_KEY)
    code, out, _ = invoke(capsys, "run", str(sample_video), "--profile", profile, "--json")
    assert code == 4
    assert json.loads(out)["error"]["code"] == "profile_unavailable"
    assert jobs(home) == []  # failed before the input was even copied


def test_usage_errors_exit_2(
    home: Path, sample_video: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    both = invoke(capsys, "run", str(sample_video), "--job", "abc123", "--profile", "fake")
    assert both[0] == 2
    assert invoke(capsys, "run", "--profile", "fake")[0] == 2  # neither input nor --job
    zero = invoke(capsys, "run", str(sample_video), "--profile", "fake", "--frame-cap", "0")
    assert zero[0] == 2 and jobs(home) == []
    with pytest.raises(SystemExit) as exc:  # argparse's own usage error
        main(["run", str(sample_video)])  # --profile is required
    assert exc.value.code == 2


def test_unknown_job_exits_5(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = invoke(capsys, "run", "--job", "abcdef123456", "--profile", "fake", "--json")
    assert code == 5 and json.loads(out)["error"]["code"] == "job_not_found"


def test_job_ids_cannot_escape_the_jobs_directory(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = invoke(capsys, "run", "--job", "../../etc", "--profile", "fake")
    assert code == 5


# ── doctor ───────────────────────────────────────────────────────────────────
def test_doctor_is_offline_by_default(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(*_a: object, **_k: object) -> None:
        raise AssertionError("doctor made a network client without --online")

    monkeypatch.setattr("frame_ingest.doctor.make_client", no_network)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_API_KEY)
    code, out, _ = invoke(capsys, "doctor", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["online"] is False and data["report"]["key_present"] is True
    assert data["report"]["ffmpeg"]["version"].startswith("ffmpeg version")
    assert FAKE_API_KEY not in out and "FAKE_API_KEY" not in out


def test_doctor_without_a_key_is_still_healthy_offline(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = invoke(capsys, "doctor")
    assert code == 0 and "ok   ffmpeg" in out


def test_doctor_online_without_a_key_fails_with_guidance(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = invoke(capsys, "doctor", "--online", "--json")
    assert code == 1 and json.loads(out)["ok"] is False
    assert "OPENAI_API_KEY" in out


def test_invalid_config_exits_4(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("- not a mapping\n", encoding="utf-8")
    code, out, _ = invoke(capsys, "doctor", "--json")
    assert code == 4 and json.loads(out)["error"]["code"] == "invalid_config"
