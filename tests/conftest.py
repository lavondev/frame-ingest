from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from frame_ingest.config import AppConfig, load_config
from frame_ingest.ffmpeg import ffmpeg_exe


def _run(args: list[str]) -> None:
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """24 s, 320x240, 4 distinct 6 s scenes (cuts at 6/12/18 s) with an audio track."""
    out = tmp_path_factory.mktemp("media") / "sample.mp4"
    _run(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x240:d=6:r=10",
            "-f",
            "lavfi",
            "-i",
            "testsrc=s=320x240:d=6:r=10",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x240:d=6:r=10",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x240:d=6:r=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=24",
            "-filter_complex",
            "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[v]",
            "-map",
            "[v]",
            "-map",
            "4:a",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-shortest",
            str(out),
        ]
    )
    return out


@pytest.fixture(scope="session")
def silent_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """12 s video without an audio track."""
    out = tmp_path_factory.mktemp("media") / "silent.mp4"
    _run(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=320x240:d=6:r=10",
            "-f",
            "lavfi",
            "-i",
            "testsrc=s=320x240:d=6:r=10",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "mpeg4",
            str(out),
        ]
    )
    return out


@pytest.fixture(scope="session")
def long_audio_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """80 s mostly-static video with audio, for chunking tests."""
    out = tmp_path_factory.mktemp("media") / "long.mp4"
    _run(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=gray:s=160x120:d=80:r=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=300:duration=80",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-shortest",
            str(out),
        ]
    )
    return out


@pytest.fixture
def config(tmp_path: Path) -> Iterator[AppConfig]:
    """Isolated config: temp home, fixed fake key, process environment ignored."""
    home = tmp_path / "home"
    home.mkdir()
    yield load_config(home, env={"OPENAI_API_KEY": "sk-test-SECRET-1234567890"})


@pytest.fixture(autouse=True)
def _job_jail(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Tests call pipeline functions directly on generated media, so the jail is the whole pytest
    temp root. The security tests set a tighter jail of their own."""
    from frame_ingest.guard.paths import job_jail

    with job_jail(tmp_path_factory.getbasetemp()):
        yield


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never resolve real hostnames; URL tests inject their own resolver or patch this."""

    async def refuse(host: str, port: int) -> list[str]:
        raise OSError("DNS is disabled in tests")

    monkeypatch.setattr("frame_ingest.fetch.policy.system_resolver", refuse)
