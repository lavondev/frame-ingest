"""Stage 1: probe. Duration, resolution, fps and audio presence parsed from `ffmpeg -i` output."""

from __future__ import annotations

import re
from pathlib import Path

from frame_ingest.errors import MediaError
from frame_ingest.ffmpeg import run_ffmpeg
from frame_ingest.models import StageName, VideoInfo
from frame_ingest.pipeline.context import PipelineContext, StageResult

NAME = StageName.PROBE
VERSION = 1
DEPS: list[StageName] = []


class ProbeResult(StageResult):
    video: VideoInfo


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_STREAM_RE = re.compile(r"^\s*Stream #\d+:\d+[^:]*:\s*(Video|Audio):\s*([^\s,(]+)(.*)$")
_RES_RE = re.compile(r",\s*(\d{2,5})x(\d{2,5})(?=[\s,\[])")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s+fps")
_TBR_RE = re.compile(r"(\d+(?:\.\d+)?)k?\s+tbr")
_ROT_RE = re.compile(r"rotation of (-?\d+(?:\.\d+)?) degrees")
_TIME_RE = re.compile(r"time=(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")

_CORRUPT_HINTS = (
    "Invalid data found when processing input",
    "moov atom not found",
    "End of file",
    "could not find codec parameters",
    "Error opening input",
)


def parse_ffmpeg_info(stderr: str) -> dict[str, object]:
    """Pure parser for `ffmpeg -i` stderr. Returns a dict suitable for VideoInfo fields."""
    info: dict[str, object] = {"has_audio": False}
    m = _DURATION_RE.search(stderr)
    if m:
        h, mi, s = m.groups()
        info["duration_s"] = int(h) * 3600 + int(mi) * 60 + float(s)
    rotation = 0.0
    rot = _ROT_RE.search(stderr)
    if rot:
        rotation = abs(float(rot.group(1))) % 360
    for line in stderr.splitlines():
        sm = _STREAM_RE.match(line)
        if not sm:
            continue
        kind, codec, rest = sm.groups()
        if kind == "Video" and "(attached pic)" not in line and "video_codec" not in info:
            info["video_codec"] = codec
            rm = _RES_RE.search(rest)
            if rm:
                w, h2 = int(rm.group(1)), int(rm.group(2))
                if rotation in (90.0, 270.0):
                    w, h2 = h2, w
                info["width"], info["height"] = w, h2
            fm = _FPS_RE.search(rest) or _TBR_RE.search(rest)
            if fm:
                info["fps"] = float(fm.group(1)) * (1000 if "k tbr" in rest else 1)
        elif kind == "Audio" and not info["has_audio"]:
            info["has_audio"] = True
            info["audio_codec"] = codec
    return info


async def probe_video(path: Path, *, filename: str, size_bytes: int, sha256: str) -> VideoInfo:
    res = await run_ffmpeg(["-i", str(path)], loglevel="info", timeout=60)
    text = res.stderr
    info = parse_ffmpeg_info(text)

    if "video_codec" not in info:
        if any(h in text for h in _CORRUPT_HINTS) or "Duration" not in text:
            raise MediaError(
                f"'{filename}' could not be read as a video. The file may be corrupt or in an "
                "unsupported format. Try re-exporting it as MP4 (H.264/AAC).",
                code="corrupt_media",
                status=422,
            )
        raise MediaError(
            f"'{filename}' has no video stream. Upload a video file (MP4, MOV, MKV, WebM, ...).",
            code="unsupported_media",
            status=415,
        )

    duration = info.get("duration_s")
    if duration is None:  # "Duration: N/A" (some live captures/streams): decode to measure
        dec = await run_ffmpeg(["-i", str(path), "-f", "null", "-"], loglevel="info", timeout=900)
        times = _TIME_RE.findall(dec.stderr)
        if not times:
            raise MediaError(
                f"Could not determine the duration of '{filename}'. The file may be corrupt.",
                code="corrupt_media",
                status=422,
            )
        h, mi, s = times[-1]
        duration = int(h) * 3600 + int(mi) * 60 + float(s)
    if not isinstance(duration, float) or duration <= 0.05:
        raise MediaError(
            f"'{filename}' appears to have zero length.", code="corrupt_media", status=422
        )

    return VideoInfo(
        filename=filename,
        size_bytes=size_bytes,
        sha256=sha256,
        duration_s=duration,
        width=_opt_int(info.get("width")),
        height=_opt_int(info.get("height")),
        fps=_opt_float(info.get("fps")),
        video_codec=_opt_str(info.get("video_codec")),
        has_audio=bool(info.get("has_audio")),
        audio_codec=_opt_str(info.get("audio_codec")),
    )


def _opt_int(v: object) -> int | None:
    return int(v) if isinstance(v, int | float) else None


def _opt_float(v: object) -> float | None:
    return float(v) if isinstance(v, int | float) else None


def _opt_str(v: object) -> str | None:
    return v if isinstance(v, str) else None


# ── stage adapter: the file is probed at upload time; the stage records/validates it ───────
def key_params(ctx: PipelineContext) -> dict[str, object]:
    return {}


def skip_reason(ctx: PipelineContext) -> str | None:
    return None


def validate_cached(ctx: PipelineContext, result: ProbeResult) -> bool:
    return True


async def run(ctx: PipelineContext) -> ProbeResult:
    ctx.log(
        f"{ctx.video.duration_s:.1f}s, {ctx.video.width}x{ctx.video.height}, "
        f"audio: {'yes' if ctx.video.has_audio else 'no'}"
    )
    return ProbeResult(video=ctx.video)
