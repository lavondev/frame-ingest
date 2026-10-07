"""Container allowlist by magic bytes (PLAN T3).

ffmpeg picks a demuxer by probing content, so a "video.mp4" that is really an HLS playlist, a
concat list or an SDP file would make it open other files or URLs. We sniff the first bytes
ourselves, accept only real media containers, and `ffmpeg_args` then forces that exact demuxer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from frame_ingest.errors import MediaError

_HEAD = 640
_TEXT_PLAYLISTS = (b"#ext", b"ffconcat", b"v=0", b"<?xml", b"<mpd", b"<!doctype", b"{")  # lowercase


@dataclass(frozen=True)
class Container:
    name: str
    demuxer: str  # value for ffmpeg's `-f` input option


def sniff_container(head: bytes) -> Container | None:
    if len(head) >= 12 and head[4:8] in {b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip"}:
        return Container("mp4/mov", "mov")
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return Container("matroska/webm", "matroska")
    if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
        return Container("avi", "avi")
    if head.startswith(b"FLV\x01"):
        return Container("flv", "flv")
    if head.startswith(b"OggS"):
        return Container("ogg", "ogg")
    if head.startswith(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11"):
        return Container("asf/wmv", "asf")
    if head.startswith(b"\x00\x00\x01\xba"):
        return Container("mpeg-ps", "mpeg")
    if head.startswith(b"fLaC"):
        return Container("flac", "flac")
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return Container("mp3", "mp3")
    if len(head) > 376 and head[0] == head[188] == head[376] == 0x47:
        return Container("mpeg-ts", "mpegts")
    if len(head) > 388 and head[4] == head[196] == head[388] == 0x47:
        return Container("m2ts", "mpegts")
    return None


def check_container(path: Path, *, what: str | None = None) -> Container:
    """Raise MediaError unless `path` starts like a real media container."""
    name = what or path.name
    with path.open("rb") as fh:
        head = fh.read(_HEAD)
    if not head:
        raise MediaError(f"'{name}' is empty.", code="corrupt_media", status=422)
    found = sniff_container(head)
    if found is not None:
        return found
    if head.lstrip(b"\xef\xbb\xbf \t\r\n").lower().startswith(_TEXT_PLAYLISTS):
        raise MediaError(
            f"'{name}' is a playlist or text manifest, not a media file. Playlist-like inputs "
            "are refused because they can make ffmpeg open other files or URLs.",
            code="playlist_input",
            status=415,
        )
    raise MediaError(
        f"'{name}' is not a recognised media container "
        "(MP4/MOV, MKV/WebM, AVI, FLV, MPEG-TS, ...).",
        code="unsupported_container",
        status=415,
    )
