"""The only way a yt-dlp command line is built (PLAN T4).

Fixed flags only: no config files, no plugins, no cookies, no `--exec`, no link files, no
post-processing (so yt-dlp never starts ffmpeg on its own), one item, a size cap, timeouts and a
restricted output template. The caller supplies a URL (already validated by `fetch.policy`),
an empty directory, numbers and a choice among three subtitle modes; nothing else is
parameterisable, and `--` precedes the URL so it can never be read as an option.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from frame_ingest.errors import FrameIngestError

Subs = Literal["none", "manual", "auto"]
FORMAT = "b[height<=1080]/b"  # a single file: no merge step, so no ffmpeg inside yt-dlp
FORBIDDEN_FRAGMENTS = (
    "--exec",
    "--write-link",
    "--write-url-link",
    "--write-webloc-link",
    "--write-desktop-link",
    "--cookies",
    "--cookies-from-browser",
    "--config-locations",
    "--plugin-dirs",
    "--netrc",
    "--username",
    "--password",
    "--batch-file",
    "--load-info-json",
    "--use-postprocessor",
    "--downloader",
    "--external-downloader",
    "--ffmpeg-location",
    "--paths",
    "--output-na-placeholder",
)


class YtdlpArgsRejected(FrameIngestError):
    code = "argv_rejected"
    status = 400


def build_ytdlp_argv(
    prefix: Sequence[str],
    url: str,
    *,
    out_dir: Path,
    max_filesize: int,
    socket_timeout: int = 30,
    subs: Subs = "none",
    skip_download: bool = False,
    proxy: str | None = None,
) -> list[str]:
    if not url.startswith(("http://", "https://")) or "\x00" in url:
        raise YtdlpArgsRejected("yt-dlp is only given validated http(s) URLs.")
    if not (0 < max_filesize < 1 << 44) or not (1 <= socket_timeout <= 120):
        raise YtdlpArgsRejected("yt-dlp limits are out of range.")
    argv = [
        *prefix,
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-cache-dir",
        "--no-playlist",
        "--max-downloads",
        "1",
        "--restrict-filenames",
        "--no-progress",
        "--quiet",
        "--no-warnings",
        "--max-filesize",
        str(max_filesize),
        "--socket-timeout",
        str(socket_timeout),
        "-f",
        FORMAT,
        "-o",
        str(out_dir / "%(id)s.%(ext)s"),
    ]
    if proxy:
        argv += ["--proxy", proxy]
    if subs != "none":
        argv += ["--write-subs" if subs == "manual" else "--write-auto-subs"]
        argv += ["--sub-format", "vtt", "--sub-langs", "en.*,en"]
    if skip_download:
        argv.append("--skip-download")
    argv += ["--", url]
    return argv


def assert_safe(argv: Sequence[str]) -> None:
    """Defence in depth for tests and callers: nothing forbidden may appear before `--`."""
    head = list(argv)[: list(argv).index("--")] if "--" in argv else list(argv)
    for arg in head:
        if arg.split("=", 1)[0] in FORBIDDEN_FRAGMENTS:
            raise YtdlpArgsRejected(f"Forbidden yt-dlp option {arg[:30]!r}.")
