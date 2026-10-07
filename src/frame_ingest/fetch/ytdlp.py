"""Run yt-dlp safely (PLAN T4): version floor in code, an empty private directory per run, a
fixed argument list, a scrubbed environment, and a verification pass over whatever it wrote
before anything is adopted. Optional extra: `uv tool install ".[url]"`."""

from __future__ import annotations

import importlib.util
import json
import re
import secrets
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from frame_ingest.errors import FrameIngestError
from frame_ingest.fetch.policy import Resolver, validate_url
from frame_ingest.guard.paths import is_within
from frame_ingest.guard.subproc import Limits, run_process
from frame_ingest.guard.ytdlp_args import (
    Subs,
    assert_safe,
    build_ytdlp_argv,
    build_ytdlp_list_argv,
)

# Raise as advisories land (CVE-2026-50023, CVE-2026-55404).
YTDLP_FLOOR = (2026, 7, 4)
INSTALL_HINT = 'Install the URL extra: uv tool install ".[url]" (from the repository).'
MEDIA_EXT = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".flv", ".avi", ".ts", ".ogv"}
SUB_EXT = {".vtt", ".srt"}
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_VERSION = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})")


class YtdlpError(FrameIngestError):
    code = "ytdlp_failed"
    status = 502


class YtdlpUnavailable(FrameIngestError):
    code = "missing_dependency"
    status = 400


class YtdlpTooOld(FrameIngestError):
    code = "ytdlp_too_old"
    status = 400


@dataclass(frozen=True)
class YtdlpResult:
    media: Path
    captions: Path | None
    captions_auto: bool


def default_prefix() -> list[str]:
    if importlib.util.find_spec("yt_dlp") is None:
        raise YtdlpUnavailable(f"yt-dlp is not installed. {INSTALL_HINT}")
    return [sys.executable, "-m", "yt_dlp"]


def parse_version(text: str) -> tuple[int, int, int]:
    m = _VERSION.match(text.strip())
    if not m:
        raise YtdlpError("Could not read the yt-dlp version.")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


async def check_version(prefix: list[str]) -> str:
    res = await run_process([*prefix, "--version"], timeout=30, limits=Limits(max_stdout=4096))
    version = res.stdout.decode("utf-8", "replace").strip()
    if res.returncode != 0 or parse_version(version) < YTDLP_FLOOR:
        floor = ".".join(str(n) for n in YTDLP_FLOOR)
        raise YtdlpTooOld(
            f"yt-dlp {version[:20]} is below the required {floor} (security fixes). "
            "Update it: uv tool upgrade frame-ingest, or pip install -U yt-dlp."
        )
    return version


def verify_directory(directory: Path, max_bytes: int) -> tuple[list[Path], list[Path]]:
    """What yt-dlp left behind must be only flat, regular, allowlisted, in-jail files."""
    media: list[Path] = []
    subs: list[Path] = []
    for entry in sorted(directory.iterdir()):
        if entry.is_symlink() or not entry.is_file() or not is_within(entry, directory):
            raise YtdlpError("yt-dlp produced something other than plain files; refusing it.")
        if not _NAME.match(entry.name):
            raise YtdlpError("yt-dlp produced a file with an unexpected name; refusing it.")
        suffix = entry.suffix.lower()
        if suffix in MEDIA_EXT:
            if entry.stat().st_size > max_bytes:
                raise YtdlpError("The downloaded file exceeds the size limit.")
            media.append(entry)
        elif suffix in SUB_EXT:
            subs.append(entry)
        else:
            raise YtdlpError(f"yt-dlp produced a file type that is not allowed ({suffix[:10]}).")
    return media, subs


async def _run(
    prefix: list[str],
    url: str,
    work: Path,
    *,
    max_bytes: int,
    timeout_s: float,
    subs: Subs,
    skip_download: bool,
    proxy: str | None,
) -> None:
    argv = build_ytdlp_argv(
        prefix,
        url,
        out_dir=work,
        max_filesize=max_bytes,
        subs=subs,
        skip_download=skip_download,
        proxy=proxy,
    )
    assert_safe(argv)
    res = await run_process(
        argv,
        timeout=timeout_s,
        cwd=work,
        env={"HOME": str(work)},
        limits=Limits(max_file_bytes=max_bytes + (64 << 20)),
    )
    # --max-downloads 1 exits 101 when it stops after the first item: that is success.
    if res.returncode not in {0, 101}:
        tail = res.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        raise YtdlpError(f"yt-dlp could not download this URL: {tail[0][:200]}")


async def download_with_ytdlp(
    url: str,
    incoming: Path,
    *,
    max_bytes: int,
    timeout_s: float = 1800.0,
    prefix: list[str] | None = None,
    resolver: Resolver | None = None,
    proxy: str | None = None,
    want_captions: bool = True,
) -> YtdlpResult:
    """Download one video (and captions if there are any) from a page URL into a fresh,
    empty directory under `incoming`, verify it, and return the files."""
    validated = await validate_url(url, resolver=resolver)  # racy on its own; see PLAN T2
    cmd = prefix or default_prefix()
    await check_version(cmd)
    work = incoming / f"ytdlp-{secrets.token_hex(6)}"
    work.mkdir(parents=True, mode=0o700)
    try:
        await _run(
            cmd,
            validated.url,
            work,
            max_bytes=max_bytes,
            timeout_s=timeout_s,
            subs="manual" if want_captions else "none",
            skip_download=False,
            proxy=proxy,
        )
        media, subs = verify_directory(work, max_bytes)
        auto = False
        if want_captions and not subs:
            await _run(
                cmd,
                validated.url,
                work,
                max_bytes=max_bytes,
                timeout_s=timeout_s,
                subs="auto",
                skip_download=True,
                proxy=proxy,
            )
            media, subs = verify_directory(work, max_bytes)
            auto = bool(subs)
        if not media:
            raise YtdlpError("yt-dlp did not produce a video file.")
        if len(media) > 1:
            raise YtdlpError("yt-dlp produced more than one video; playlists are not allowed.")
        return YtdlpResult(media=media[0], captions=subs[0] if subs else None, captions_auto=auto)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


async def list_playlist(
    url: str,
    *,
    max_items: int,
    prefix: list[str] | None = None,
    resolver: Resolver | None = None,
    proxy: str | None = None,
) -> tuple[list[str], int]:
    """URLs of up to `max_items` playlist entries, and how many entries were refused.

    Every entry URL is re-validated by the URL policy before it is returned; the list itself comes
    from untrusted metadata, so it is size-capped and only plain http(s) strings are kept."""
    from frame_ingest.fetch.policy import UrlRejected

    validated = await validate_url(url, resolver=resolver)
    cmd = prefix or default_prefix()
    await check_version(cmd)
    argv = build_ytdlp_list_argv(cmd, validated.url, max_items=max_items, proxy=proxy)
    assert_safe(argv)
    res = await run_process(argv, timeout=300, limits=Limits(max_stdout=2 << 20))
    if res.returncode != 0:
        tail = res.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        raise YtdlpError(f"yt-dlp could not list this URL: {tail[0][:200]}")
    try:
        data = json.loads(res.stdout.decode("utf-8", "replace"))
    except ValueError:
        raise YtdlpError("yt-dlp returned a listing that could not be read.") from None
    entries = data.get("entries") if isinstance(data, dict) else None
    candidates = (
        [e.get("url") or e.get("webpage_url") for e in entries if isinstance(e, dict)]
        if isinstance(entries, list)
        else [validated.url]  # not a playlist: the single video itself
    )
    urls: list[str] = []
    refused = 0
    for cand in candidates[:max_items]:
        if not isinstance(cand, str) or len(cand) > 2048:
            refused += 1
            continue
        try:
            urls.append((await validate_url(cand, resolver=resolver)).url)
        except UrlRejected:
            refused += 1
    return urls, refused
