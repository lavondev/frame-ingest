"""Turn a URL into a local file the engine can adopt.

A direct media link goes through our own pinned-IP fetcher; any other page goes through the
hardened yt-dlp wrapper. Either way the result is a regular file in a private directory under
<home>/incoming/, which the engine then moves into a job; nothing here touches ffmpeg.
"""

from __future__ import annotations

import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

from frame_ingest.config import AppConfig
from frame_ingest.fetch.http import download_media, looks_like_direct_media
from frame_ingest.fetch.policy import Resolver, validate_url
from frame_ingest.fetch.ytdlp import download_with_ytdlp


@dataclass
class Acquired:
    path: Path
    source_url: str  # display form: no query string
    captions: Path | None
    captions_auto: bool
    workdir: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


async def fetch_url(
    url: str,
    config: AppConfig,
    *,
    resolver: Resolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    ytdlp_prefix: list[str] | None = None,
    proxy: str | None = None,
) -> Acquired:
    max_bytes = int(config.max_file_mb * 1024 * 1024)
    incoming = config.home / "incoming"
    incoming.mkdir(parents=True, exist_ok=True, mode=0o700)
    workdir = incoming / secrets.token_hex(6)
    workdir.mkdir(mode=0o700)
    try:
        validated = await validate_url(url, resolver=resolver)
        if looks_like_direct_media(url):
            got = await download_media(
                url, workdir, max_bytes=max_bytes, resolver=resolver, transport=transport
            )
            return Acquired(got.path, got.source, None, False, workdir)
        res = await download_with_ytdlp(
            url,
            workdir,
            max_bytes=max_bytes,
            prefix=ytdlp_prefix,
            resolver=resolver,
            proxy=proxy,
        )
        return Acquired(res.media, validated.display, res.captions, res.captions_auto, workdir)
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


def attach_captions(job_dir: Path, acquired: Acquired) -> None:
    """Keep fetched captions with the job so `prepare` can use them as the transcript."""
    if acquired.captions is None:
        return
    stem = "captions-auto" if acquired.captions_auto else "captions"
    shutil.copyfile(acquired.captions, job_dir / f"{stem}{acquired.captions.suffix.lower()}")
