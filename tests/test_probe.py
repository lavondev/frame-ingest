from pathlib import Path

import pytest

from frame_ingest.errors import MediaError
from frame_ingest.pipeline.probe import parse_ffmpeg_info, probe_video

STDERR = """\
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'x.mp4':
  Duration: 00:01:02.50, start: 0.000000, bitrate: 1200 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709), 1920x1080 [SAR 1:1 DAR 16:9], 1100 kb/s, 29.97 fps, 29.97 tbr, 30k tbn (default)
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, stereo, fltp, 96 kb/s (default)
"""


def test_parse_ffmpeg_info_video_and_audio() -> None:
    info = parse_ffmpeg_info(STDERR)
    assert info["duration_s"] == 62.5
    assert (info["width"], info["height"]) == (1920, 1080)
    assert info["fps"] == 29.97 and info["video_codec"] == "h264"
    assert info["has_audio"] is True and info["audio_codec"] == "aac"


def test_parse_ffmpeg_info_does_not_confuse_codec_tag_for_resolution() -> None:
    info = parse_ffmpeg_info(STDERR)
    assert info["width"] != 0 and info["height"] == 1080


def test_parse_ffmpeg_info_no_audio_and_rotation() -> None:
    err = (
        "  Duration: 00:00:10.00, start: 0.0, bitrate: 1 kb/s\n"
        "  Stream #0:0(und): Video: h264, yuv420p, 1280x720, 30 fps, 30 tbr\n"
        "      displaymatrix: rotation of -90.00 degrees\n"
    )
    info = parse_ffmpeg_info(err)
    assert info["has_audio"] is False
    assert (info["width"], info["height"]) == (720, 1280)  # rotated portrait video


def test_parse_ffmpeg_info_ignores_cover_art() -> None:
    err = (
        "  Duration: 00:03:00.00\n"
        "  Stream #0:0: Audio: mp3, 44100 Hz, stereo\n"
        "  Stream #0:1: Video: mjpeg, yuvj420p, 500x500, 90k tbr (attached pic)\n"
    )
    assert "video_codec" not in parse_ffmpeg_info(err)


async def test_probe_real_video(sample_video: Path) -> None:
    info = await probe_video(sample_video, filename="sample.mp4", size_bytes=10, sha256="abc")
    assert 23.5 < info.duration_s < 24.5
    assert (info.width, info.height) == (320, 240)
    assert info.has_audio and info.fps == 10


async def test_probe_corrupt_file_is_a_clear_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"this is definitely not a video" * 100)
    with pytest.raises(MediaError) as ei:
        await probe_video(bad, filename="bad.mp4", size_bytes=3000, sha256="x")
    assert ei.value.code == "corrupt_media" and "bad.mp4" in ei.value.message


async def test_probe_audio_only_is_rejected(tmp_path: Path) -> None:
    from frame_ingest.ffmpeg import run_ffmpeg

    wav = tmp_path / "a.wav"
    await run_ffmpeg(["-y", "-f", "lavfi", "-i", "sine=duration=1", str(wav)])
    with pytest.raises(MediaError, match="no video stream"):
        await probe_video(wav, filename="a.wav", size_bytes=1, sha256="x")
