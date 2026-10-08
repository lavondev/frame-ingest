from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw, ImageFont

from frame_ingest.config import AppConfig, load_config
from frame_ingest.ffmpeg import ffmpeg_exe
from tests.helpers import FAKE


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
def quiet_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """12 s video whose audio track is digital silence (a track with nothing on it)."""
    out = tmp_path_factory.mktemp("media") / "quiet.mp4"
    _run(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc=s=320x240:d=12:r=10",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            "12",
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


TEXT_SLIDES = [
    ["Cache settings", "Default size: 256 MB"],
    ["Cache settings", "Default size: 256 MB", "Maximum size: 4 GB"],
    ["Cache settings", "Default size: 512 MB", "Maximum size: 4 GB"],
]


@pytest.fixture(scope="session")
def text_slides_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """18 s, 640x360: three 6 s slides on the same dark background whose only difference is
    their text (a line is added at 6 s, one number changes at 12 s), like evals/make_fixtures.py.
    Too subtle for the scene score; the case still-screen detection exists for."""
    root = tmp_path_factory.mktemp("media")
    inputs: list[str] = []
    for i, lines in enumerate(TEXT_SLIDES):
        img = Image.new("RGB", (640, 360), (16, 24, 32))
        draw = ImageDraw.Draw(img)
        for j, line in enumerate(lines):
            size = 40 if j == 0 else 28
            draw.text((40, 40 + j * 70), line, fill=(255, 255, 255), font=_font(size))
        png = root / f"slide-{i}.png"
        img.save(png)
        inputs += ["-loop", "1", "-t", "6", "-i", str(png)]
    out = root / "text-slides.mp4"
    _run(
        [
            *inputs,
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0,format=yuv420p[v]",
            "-map",
            "[v]",
            "-r",
            "10",
            "-c:v",
            "mpeg4",
            "-q:v",
            "4",
            str(out),
        ]
    )
    return out


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError):  # pragma: no cover - very old Pillow or no FreeType
        return ImageFont.load_default()


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
def _in_a_temp_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`finish` copies documents into ./frame-ingest-out/; never into the repository."""
    work = tmp_path / "session-folder"
    work.mkdir(exist_ok=True)
    monkeypatch.chdir(work)


@pytest.fixture(autouse=True)
def _no_real_speech_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real faster-whisper downloads model weights from the network on first use, so tests
    never see it: importing it fails (as when the `local` extra is missing) unless a test installs
    a stand-in module of its own."""
    monkeypatch.setitem(sys.modules, "faster_whisper", None)


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never resolve real hostnames; URL tests inject their own resolver or patch this."""

    async def refuse(host: str, port: int) -> list[str]:
        raise OSError("DNS is disabled in tests")

    monkeypatch.setattr("frame_ingest.fetch.policy.system_resolver", refuse)


@pytest.fixture
def fake_ytdlp(tmp_path: Path, sample_video: Path) -> Any:
    """A fake yt-dlp executable; each test scripts its behaviour through `.set(...)`."""
    root = tmp_path / "fake"
    root.mkdir()
    script = root / "fake_ytdlp.py"
    script.write_text(FAKE)

    class Fake:
        prefix = [sys.executable, str(script)]
        video = sample_video

        def set(self, **spec: Any) -> None:
            base = {
                "version": "2026.07.04",
                "files": [["abc.mp4", f"copy:{sample_video}"]],
                "exit": 0,
            }
            base.update(spec)
            (root / "fake.json").write_text(json.dumps(base))

        def argv(self) -> list[str]:
            return json.loads((root / "argv.json").read_text())

    f = Fake()
    f.set()
    return f
