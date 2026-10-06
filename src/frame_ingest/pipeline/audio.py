"""Stage 2: audio extraction and chunking.

Mono 16 kHz audio is extracted to a lossless FLAC intermediate (so there is exactly one lossy
generation), then each chunk is encoded separately (Opus/OGG or MP3) with timestamps reset, so
every chunk carries correct duration metadata. Chunks overlap by ~1 s (long-chunk mode) so words
on a boundary are heard whole by at least one chunk; the overlap is deduped when stitching.
Every chunk is verified well under the upload limit and re-split if it is not.
"""

from __future__ import annotations

import math
from pathlib import Path

from pydantic import BaseModel

from frame_ingest.config import AppConfig
from frame_ingest.errors import FrameIngestError, MediaError
from frame_ingest.ffmpeg import file_size_mb, run_ffmpeg
from frame_ingest.models import StageName
from frame_ingest.pipeline.context import PipelineContext, StageResult
from frame_ingest.pipeline.probe import parse_ffmpeg_info
from frame_ingest.pipeline.util import run_units

NAME = StageName.AUDIO
VERSION = 1
DEPS: list[StageName] = [StageName.PROBE]
MIN_TAIL_S = 5.0
MIN_SPLIT_S = 10.0


class AudioChunk(BaseModel):
    index: int
    start: float
    end: float
    file: str  # relative to the job dir
    size_bytes: int

    @property
    def duration(self) -> float:
        return self.end - self.start


class AudioResult(StageResult):
    chunks: list[AudioChunk] = []
    short_mode: bool = False
    audio_format: str = "ogg"


def plan_chunks(
    duration: float, chunk_s: float, overlap_s: float, min_tail: float = MIN_TAIL_S
) -> list[tuple[float, float]]:
    """Chunk windows covering [0, duration]. Chunk i>0 starts overlap_s before its nominal
    boundary. A trailing sliver shorter than min_tail is merged into the previous chunk."""
    if duration <= 0:
        return []
    if duration <= chunk_s:
        return [(0.0, duration)]
    n = math.ceil(duration / chunk_s)
    bounds = [min(i * chunk_s, duration) for i in range(n + 1)]
    windows = [(max(0.0, bounds[i] - (overlap_s if i else 0.0)), bounds[i + 1]) for i in range(n)]
    if len(windows) > 1 and (windows[-1][1] - bounds[n - 1]) < min_tail:
        prev_start = windows[-2][0]
        windows = [*windows[:-2], (prev_start, duration)]
    return windows


def split_window(start: float, end: float, overlap_s: float) -> list[tuple[float, float]]:
    mid = (start + end) / 2
    return [(start, mid), (max(start, mid - overlap_s), end)]


def _codec_args(cfg: AppConfig) -> tuple[list[str], str]:
    if cfg.audio_format == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", f"{max(cfg.audio_bitrate_kbps, 32)}k"], "mp3"
    return ["-c:a", "libopus", "-b:a", f"{cfg.audio_bitrate_kbps}k", "-application", "voip"], "ogg"


async def _encode_window(src: Path, start: float, end: float, out: Path, cfg: AppConfig) -> None:
    codec, _ = _codec_args(cfg)
    res = await run_ffmpeg(
        [
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{end - start:.3f}",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            *codec,
            "-map_metadata",
            "-1",
            "-avoid_negative_ts",
            "make_zero",
            str(out),
        ],
        timeout=600,
    )
    if res.returncode != 0 or not out.is_file():
        raise FrameIngestError(
            f"Audio encoding failed: {res.stderr.strip()[-300:]}", code="audio_failed"
        )


async def _probe_duration(path: Path) -> float | None:
    res = await run_ffmpeg(["-i", str(path)], loglevel="info", timeout=60)
    d = parse_ffmpeg_info(res.stderr).get("duration_s")
    return float(d) if isinstance(d, int | float) else None


def key_params(ctx: PipelineContext) -> dict[str, object]:
    c = ctx.config
    return {
        "short_mode": not ctx.segment_timestamps,
        "chunk_minutes": c.chunk_minutes,
        "overlap": c.chunk_overlap_s,
        "short_chunk_s": c.short_chunk_s,
        "format": c.audio_format,
        "bitrate": c.audio_bitrate_kbps,
        "max_chunk_mb": c.max_chunk_mb,
    }


def skip_reason(ctx: PipelineContext) -> str | None:
    return None if ctx.video.has_audio else "No audio track — transcription skipped."


def validate_cached(ctx: PipelineContext, result: AudioResult) -> bool:
    return all((ctx.job_dir / c.file).is_file() for c in result.chunks)


async def run(ctx: PipelineContext) -> AudioResult:
    if not ctx.video.has_audio:
        return AudioResult()
    cfg = ctx.config
    short_mode = not ctx.segment_timestamps
    chunk_s = cfg.short_chunk_s if short_mode else cfg.chunk_minutes * 60
    overlap = 0.0 if short_mode else cfg.chunk_overlap_s
    _, ext = _codec_args(cfg)

    audio_dir = ctx.job_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    for old in audio_dir.glob("*"):
        old.unlink(missing_ok=True)

    full = audio_dir / "full.flac"
    ctx.log("Extracting mono 16 kHz audio")
    res = await run_ffmpeg(
        [
            "-y",
            "-i",
            str(ctx.video_path),
            "-vn",
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "flac",
            str(full),
        ],
        timeout=3600,
    )
    if res.returncode != 0 or not full.is_file():
        raise MediaError(
            "Could not extract the audio track from this video. It may use an unsupported "
            f"audio codec. ({res.stderr.strip()[-200:]})",
            code="unsupported_media",
            status=422,
        )

    windows = plan_chunks(ctx.video.duration_s, chunk_s, overlap)
    ctx.progress(0, len(windows) + 1, f"Encoding {len(windows)} chunk(s)")
    chunks: list[AudioChunk] = []
    try:
        pending = list(windows)
        for _ in range(8):  # re-split rounds
            enc: list[tuple[int, tuple[float, float]]] = list(enumerate(pending))
            tmp_files = [audio_dir / f"tmp_{i:03d}.{ext}" for i, _ in enc]

            async def encode(
                item: tuple[int, tuple[float, float]], files: list[Path] = tmp_files
            ) -> None:
                i, (s, e) = item
                await _encode_window(full, s, e, files[i], cfg)

            await run_units(enc, encode, 3)
            oversize = [i for i, f in enumerate(tmp_files) if file_size_mb(f) > cfg.max_chunk_mb]
            if not oversize:
                for i, (s, e) in enc:
                    final = audio_dir / f"chunk_{i:03d}.{ext}"
                    tmp_files[i].replace(final)
                    chunks.append(
                        AudioChunk(
                            index=i,
                            start=s,
                            end=e,
                            file=str(final.relative_to(ctx.job_dir)),
                            size_bytes=final.stat().st_size,
                        )
                    )
                break
            next_pending: list[tuple[float, float]] = []
            for i, (s, e) in enc:
                if i in oversize and (e - s) > 2 * MIN_SPLIT_S:
                    next_pending.extend(split_window(s, e, overlap))
                elif i in oversize:
                    raise FrameIngestError(
                        f"An audio chunk is still larger than {cfg.max_chunk_mb} MB at "
                        f"{e - s:.0f}s; lower FRAME_INGEST_AUDIO_BITRATE_KBPS.",
                        code="audio_failed",
                    )
                else:
                    next_pending.append((s, e))
            for f in tmp_files:
                f.unlink(missing_ok=True)
            pending = next_pending
        else:
            raise FrameIngestError(
                "Could not split audio into small enough chunks.", code="audio_failed"
            )
    finally:
        full.unlink(missing_ok=True)

    # verify duration metadata of every chunk (reset timestamps => should match the plan)
    for c in chunks:
        actual = await _probe_duration(ctx.job_dir / c.file)
        if actual is None or abs(actual - c.duration) > 1.0:
            ctx.warn(
                "chunk_duration_mismatch",
                f"Audio chunk {c.index} reports {actual if actual is not None else 'no'} "
                f"seconds of audio, expected {c.duration:.1f}.",
                c.start,
                c.end,
            )
    ctx.progress(len(windows) + 1, len(windows) + 1, f"{len(chunks)} chunk(s) ready")
    return AudioResult(chunks=chunks, short_mode=short_mode, audio_format=ext)
