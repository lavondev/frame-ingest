"""Fetch a direct media URL to a file (PLAN T2).

Our own client, not a library's: every hop (the first request and each redirect) is validated by
`policy`, and the TCP connection goes to the address that was validated, with the original host
in the Host header and TLS SNI (so certificates are still checked against the name, and a DNS
answer that changes between check and connect cannot redirect us). No environment proxies, no
auto-redirects, no compression, a size cap and an overall deadline; the body streams to a new
file that is never overwritten.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx

from frame_ingest.errors import FrameIngestError
from frame_ingest.fetch.policy import (
    DEFAULT_PORTS,
    Resolver,
    UrlRejected,
    ValidatedUrl,
    validate_url,
)
from frame_ingest.storage import safe_filename

MAX_REDIRECTS = 5
CHUNK = 256 * 1024
MEDIA_SUFFIXES = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".ts", ".flv", ".ogv", ".mpg"}


class FetchError(FrameIngestError):
    code = "fetch_failed"
    status = 502


@dataclass(frozen=True)
class Downloaded:
    path: Path
    size: int
    source: str  # display form of the final URL (no query string)


def looks_like_direct_media(url: str) -> bool:
    path = url.split("#", 1)[0].split("?", 1)[0].lower()
    return any(path.endswith(s) for s in MEDIA_SUFFIXES)


def _target(v: ValidatedUrl) -> tuple[str, dict[str, str], dict[str, object]]:
    ip = v.ips[0]
    host_ip = f"[{ip}]" if ":" in ip else ip
    default = 443 if v.scheme == "https" else 80
    host_header = v.host if v.port == default else f"{v.host}:{v.port}"
    headers = {"Host": host_header, "Accept-Encoding": "identity", "User-Agent": "frame-ingest"}
    extensions: dict[str, object] = {"sni_hostname": v.host} if v.scheme == "https" else {}
    return f"{v.scheme}://{host_ip}:{v.port}{v.path_query}", headers, extensions


async def download_media(
    url: str,
    dest_dir: Path,
    *,
    max_bytes: int,
    timeout_s: float = 900.0,
    resolver: Resolver | None = None,
    ports: frozenset[int] = DEFAULT_PORTS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Downloaded:
    """Download `url` into `dest_dir` (created, private) and return the file."""
    try:
        async with asyncio.timeout(timeout_s):
            return await _download(url, dest_dir, max_bytes, resolver, ports, transport)
    except TimeoutError:
        raise FetchError(f"The download did not finish within {timeout_s:.0f} s.") from None
    except httpx.HTTPError as exc:
        raise FetchError(f"The download failed ({type(exc).__name__}).") from None


async def _download(
    url: str,
    dest_dir: Path,
    max_bytes: int,
    resolver: Resolver | None,
    ports: frozenset[int],
    transport: httpx.AsyncBaseTransport | None,
) -> Downloaded:
    async with httpx.AsyncClient(
        transport=transport or httpx.AsyncHTTPTransport(),
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(30.0, read=60.0),
    ) as client:
        current = url
        first_scheme: str | None = None
        for _hop in range(MAX_REDIRECTS + 1):
            v = await validate_url(current, resolver=resolver, ports=ports)
            if first_scheme == "https" and v.scheme != "https":
                raise UrlRejected("A redirect from https to http was refused.")
            first_scheme = first_scheme or v.scheme
            target, headers, ext = _target(v)
            async with client.stream("GET", target, headers=headers, extensions=ext) as resp:
                if resp.status_code in {301, 302, 303, 307, 308}:
                    loc = resp.headers.get("location")
                    if not loc:
                        raise FetchError("A redirect had no Location header.")
                    current = urljoin(v.url, loc)
                    continue
                if resp.status_code != 200:
                    raise FetchError(f"The server answered {resp.status_code}.")
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype.startswith("text/") or ctype in {"application/json", "application/xml"}:
                    raise FetchError(
                        "The URL is a web page, not a media file. Page URLs need the yt-dlp "
                        'extra: uv tool install ".[url]".'
                    )
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise FetchError(f"The file is larger than the {max_bytes} byte limit.")
                return await _stream_to_file(resp, v, dest_dir, max_bytes)
        raise FetchError(f"Too many redirects (more than {MAX_REDIRECTS}).")


async def _stream_to_file(
    resp: httpx.Response, v: ValidatedUrl, dest_dir: Path, max_bytes: int
) -> Downloaded:
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = safe_filename(v.path_query.split("?", 1)[0].rsplit("/", 1)[-1] or "download")
    path = dest_dir / name
    size = 0
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # never overwrite or follow
    except FileExistsError:
        raise FetchError("The download target already exists; refusing to overwrite it.") from None
    try:
        with os.fdopen(fd, "wb") as fh:
            async for chunk in resp.aiter_raw(CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise FetchError(f"The file is larger than the {max_bytes} byte limit.")
                fh.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if size == 0:
        path.unlink(missing_ok=True)
        raise FetchError("The server sent an empty body.")
    return Downloaded(path=path, size=size, source=v.display)
