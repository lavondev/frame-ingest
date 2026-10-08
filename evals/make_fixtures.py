"""Generate the eval videos described in evals/cases.json (nothing binary is kept in git).

    uv run python evals/make_fixtures.py               # into evals/out/, real speech if possible
    uv run python evals/make_fixtures.py --tts none    # tone stand-ins (what the offline tests use)

Slides are drawn with Pillow and encoded with the ffmpeg that ships with frame-ingest. Speech
comes from a text-to-speech program on this machine (`say` on macOS, `espeak-ng` or `espeak`
elsewhere). Without one, a tone stands in for the voice: fine for the CLI-side tests, which
script the speech-to-text, but useless for the agent-side eval, which needs real speech.

This is a developer tool run on our own inputs, so it calls ffmpeg directly; the product code
only ever starts processes through guard/subproc.py.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases.json"
SLIDE_S = 8.0
SIZE = (1280, 720)
TTS_PROGRAMS = ("say", "espeak-ng", "espeak")


def load_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    return cases


def available_tts() -> str | None:
    return next((p for p in TTS_PROGRAMS if shutil.which(p)), None)


def _ffmpeg() -> str:
    import imageio_ffmpeg

    return str(imageio_ffmpeg.get_ffmpeg_exe())


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError):  # pragma: no cover - very old Pillow or no FreeType
        return ImageFont.load_default()


def draw_slide(slide: dict[str, Any], out: Path) -> None:
    img = Image.new("RGB", SIZE, (16, 24, 32))
    draw = ImageDraw.Draw(img)
    draw.text((80, 120), slide["title"], fill=(255, 255, 255), font=_font(72))
    for i, line in enumerate(slide.get("lines", [])):
        draw.text((80, 280 + i * 80), line, fill=(134, 216, 230), font=_font(48))
    img.save(out)


def speak(text: str, out: Path, program: str) -> Path:
    """One sentence of speech as an audio file (AIFF from `say`, WAV from espeak)."""
    if program == "say":
        target = out.with_suffix(".aiff")
        subprocess.run([program, "-o", str(target), text], check=True)
    else:
        target = out.with_suffix(".wav")
        subprocess.run([program, "-w", str(target), text], check=True)
    return target


def build_case(case: dict[str, Any], out_dir: Path, tts: str | None) -> Path:
    """Write <out_dir>/<id>.mp4 for one case and return its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{case['id']}.mp4"
    duration = SLIDE_S * len(case["slides"])
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        args: list[str] = []
        for i, slide in enumerate(case["slides"]):
            png = work / f"slide-{i}.png"
            draw_slide(slide, png)
            args += ["-loop", "1", "-t", f"{SLIDE_S}", "-i", str(png)]
        n = len(case["slides"])
        graph = [f"{''.join(f'[{i}:v]' for i in range(n))}concat=n={n}:v=1:a=0,format=yuv420p[v]"]
        voices: list[str] = []
        k = n
        if case["audio"] in ("speech", "speech+music"):
            if tts is None:  # a tone stands in for the voice
                args += ["-f", "lavfi", "-i", f"sine=frequency=300:duration={duration}"]
                voices.append(f"[{k}:a]")
                k += 1
            else:
                for i, line in enumerate(case["speech"]):
                    args += ["-i", str(speak(line, work / f"line-{i}", tts))]
                    delay = int((i * SLIDE_S + 0.5) * 1000)
                    graph.append(f"[{k}:a]aresample=16000,adelay={delay}:all=1[s{i}]")
                    voices.append(f"[s{i}]")
                    k += 1
        if case["audio"] == "speech+music":
            args += ["-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.35:d={duration}"]
            args += ["-f", "lavfi", "-i", f"sine=frequency=110:duration={duration}"]
            voices += [f"[{k}:a]", f"[{k + 1}:a]"]
            k += 2
        if case["audio"] == "silence":
            args += ["-f", "lavfi", "-t", f"{duration}", "-i", "anullsrc=r=16000:cl=mono"]
            voices.append(f"[{k}:a]")
        mix = f"{''.join(voices)}amix=inputs={len(voices)}:normalize=0:duration=longest"
        graph.append(f"{mix},apad,atrim=0:{duration}[a]")
        cmd = [
            _ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *args,
            "-filter_complex",
            ";".join(graph),
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-t",
            f"{duration}",
            "-r",
            "10",
            "-c:v",
            "mpeg4",
            "-q:v",
            "4",
            "-c:a",
            "aac",
            str(target),
        ]
        subprocess.run(cmd, check=True)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=HERE / "out", help="output folder")
    parser.add_argument(
        "--tts",
        choices=("auto", *TTS_PROGRAMS, "none"),
        default="auto",
        help="text-to-speech program for the voice (none: a tone stands in)",
    )
    args = parser.parse_args(argv)
    tts = available_tts() if args.tts == "auto" else None if args.tts == "none" else args.tts
    if tts is None and args.tts == "auto":
        print(
            "note: no text-to-speech program found; a tone stands in for the voice, so these "
            "videos only suit the offline tests, not the agent-side eval.",
            file=sys.stderr,
        )
    for case in load_cases():
        print(build_case(case, args.out, tts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
