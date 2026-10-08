"""Mode selection and long videos.

No profile means agent mode even when an API key is set (a key is not consent); `--profile local`
runs local models; `--profile cloud` needs a key and `--allow-egress`. Long videos get their
vision work split into groups that parallel subagents can take.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

import pytest

from frame_ingest.cli import main
from frame_ingest.doctor import HealthCheck
from frame_ingest.profiles import fake_bundle

SRT = "1\n00:00:01,000 --> 00:00:05,000\nOpen the dashboard first.\n"
KEY = "sk-test-SECRET-1234567890"


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
    path = tmp_path / "s.srt"
    path.write_text(SRT, encoding="utf-8")
    return path


@pytest.fixture
def fake_cloud(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The cloud providers, replaced by fakes; counts how often they were built."""
    built: list[int] = []

    def build(*_a: Any, **_k: Any) -> Any:
        built.append(1)
        return fake_bundle()

    monkeypatch.setattr("frame_ingest.providers.openai_client.build_openai_providers", build)
    return built


def cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    code = main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


# ── which mode runs ─────────────────────────────────────────────────────────────
def test_no_profile_is_agent_mode_even_with_a_key(
    home: Path,
    sample_video: Path,
    srt: Path,
    fake_cloud: list[int],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    code, card = cli(capsys, "ingest", str(sample_video), "--captions", str(srt))
    assert code == 0 and card["mode"] == "agent" and card["state"] == "fill"
    assert fake_cloud == []  # no cloud provider was even built


def test_cloud_needs_a_key_then_consent(
    home: Path,
    sample_video: Path,
    fake_cloud: list[int],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, res = cli(capsys, "ingest", str(sample_video), "--profile", "cloud")
    assert code == 4 and res["error"]["code"] == "missing_api_key"

    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    code, res = cli(capsys, "ingest", str(sample_video), "--profile", "cloud")
    assert code == 4 and res["error"]["code"] == "egress_denied" and fake_cloud == []
    assert KEY not in json.dumps(res)

    code, res = cli(capsys, "ingest", str(sample_video), "--profile", "cloud", "--allow-egress")
    assert code == 0 and res["mode"] == "cloud" and res["state"] == "done", res
    assert fake_cloud and res["egress"]["network"] is True
    assert res["validate"]["ok"] and res["coverage_line"].startswith("**Coverage:**")
    assert Path(res["document"]).is_file()


def test_local_reports_what_is_missing_before_running(
    home: Path,
    sample_video: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_server(_config: Any) -> list[HealthCheck]:
        return [
            HealthCheck(name="local server", ok=False, message="Start it: ollama serve"),
        ]

    monkeypatch.setattr("frame_ingest.cli.check_local", no_server)
    code, res = cli(capsys, "ingest", str(sample_video), "--profile", "local")
    assert code == 4 and res["state"] == "doctor" and res["mode"] == "local"
    assert "ollama serve" in res["error"]["message"]
    assert not (home / "jobs").exists() or not list((home / "jobs").iterdir())


@pytest.mark.parametrize(
    "argv",
    [
        ["--profile", "local", "--captions", "x.srt"],
        ["--profile", "cloud", "--allow-frames-only"],
        ["--profile", "local", "--cloud-speech"],
        ["--allow-egress"],  # consent alone selects nothing
    ],
)
def test_flags_that_do_not_fit_the_mode_are_usage_errors(
    argv: list[str], home: Path, sample_video: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, res = cli(capsys, "ingest", str(sample_video), *argv)
    assert code == 2 and res["error"]["code"] == "usage"


# ── long videos ─────────────────────────────────────────────────────────────────
def test_a_long_video_splits_vision_into_groups_for_subagents(
    home: Path,
    sample_video: Path,
    srt: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, card = cli(capsys, "ingest", str(sample_video), "--captions", str(srt))
    assert "long_video" not in card  # 24 s is not long

    monkeypatch.setenv("FRAME_INGEST_LONG_VIDEO_MINUTES", "0.2")  # 12 s: now it is
    code, card = cli(
        capsys, "ingest", "--job", card["job_id"], "--dense", "--start", "0", "--end", "23"
    )
    assert code == 0
    lv = card["long_video"]
    batches = [r["file"] for r in card["fill"] if r["kind"] == "vision-batch"]
    assert len(batches) >= 2 and [g["files"] for g in lv["groups"]] == [[b] for b in batches]
    for group in lv["groups"]:
        assert group["done"] is False and group["sheets"]
        assert group["files"][0] in group["prompt"] and "untrusted" in group["prompt"]
        assert shlex.quote(card["fi_path"]) + " check '<file>' --json" in group["prompt"]
    assert "never switch on your own" in lv["advice"]
    assert "--allow-egress" in lv["alternatives"]["cloud"]["command"]
    assert "explicitly agrees" in lv["alternatives"]["cloud"]["note"]

    monkeypatch.setenv("FRAME_INGEST_LONG_VIDEO_MAX_GROUPS", "1")
    _, card = cli(capsys, "next", card["job_id"])
    assert [g["files"] for g in card["long_video"]["groups"]] == [batches]
