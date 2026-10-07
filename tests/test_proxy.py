"""The egress-guard proxy: the URL policy enforced at connect time, for every connection."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from frame_ingest.config import AppConfig
from frame_ingest.fetch.acquire import fetch_url
from frame_ingest.fetch.policy import ValidatedUrl
from frame_ingest.fetch.proxy import EgressProxy
from tests.helpers import public


async def origin(handler: Any) -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def talk(port: int, data: bytes, *, read: int = 65536, wait: float = 3.0) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    await writer.drain()
    out = b""
    try:
        while len(out) < read:
            chunk = await asyncio.wait_for(reader.read(65536), wait)
            if not chunk:
                break
            out += chunk
    except TimeoutError:
        pass
    writer.close()
    return out


def allow_to(ip: str) -> Any:
    """A validator that accepts any name and says it resolved to `ip` (for tests only)."""

    async def validator(host: str, port: int, scheme: str) -> ValidatedUrl:
        return ValidatedUrl(f"{scheme}://{host}/", scheme, host, port, "/", (ip,))

    return validator


# ── refusals (the real policy) ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    "target",
    [
        "169.254.169.254:443",
        "127.0.0.1:443",
        "localhost:443",
        "[::1]:443",
        "10.0.0.5:443",
        "0x7f000001:443",
        "93.184.216.34:22",  # public address, disallowed port
    ],
)
async def test_connect_to_internal_or_odd_destinations_is_refused(target: str) -> None:
    async with EgressProxy() as proxy:
        reply = await talk(
            proxy.port, f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode()
        )
        assert reply.startswith(b"HTTP/1.1 403"), reply
        assert target.rsplit(":", 1)[0].strip("[]") in proxy.refused[0]


async def test_plain_http_to_a_private_address_or_name_is_refused() -> None:
    async def private(host: str, port: int) -> list[str]:
        return ["10.1.2.3"]

    async with EgressProxy(resolver=private) as proxy:
        for url in (
            "http://10.0.0.5/x",
            "http://169.254.169.254/latest",
            "http://intranet.example.org/x",
        ):
            reply = await talk(proxy.port, f"GET {url} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            assert reply.startswith(b"HTTP/1.1 403"), (url, reply)


async def test_malformed_requests_get_a_400_and_never_crash_the_proxy() -> None:
    async with EgressProxy() as proxy:
        for raw in (
            b"garbage\r\n\r\n",
            b"GET /relative HTTP/1.1\r\nHost: x\r\n\r\n",
            b"GET ftp://example.org/x HTTP/1.1\r\n\r\n",
            b"CONNECT nohostport HTTP/1.1\r\n\r\n",
            b"CONNECT host:abc HTTP/1.1\r\n\r\n",
            b"A" * 200_000,  # no terminator, far beyond the head limit
        ):
            assert (await talk(proxy.port, raw)).startswith(b"HTTP/1.1 4"), raw[:20]
        assert (await talk(proxy.port, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")).startswith(
            b"HTTP/1.1 403"
        )  # still serving


async def test_the_listener_is_loopback_only() -> None:
    async with EgressProxy() as proxy:
        assert proxy._server is not None
        hosts = {s.getsockname()[0] for s in proxy._server.sockets}
        assert hosts == {"127.0.0.1"} and proxy.url == f"http://127.0.0.1:{proxy.port}"


# ── forwarding (a permissive validator stands in for a public address) ─────────
async def test_plain_http_is_forwarded_in_origin_form_without_proxy_headers() -> None:
    seen: list[bytes] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")
        await writer.drain()
        writer.close()

    server, port = await origin(serve)
    try:
        async with EgressProxy(validator=allow_to("127.0.0.1")) as proxy:
            request = (
                f"GET http://videos.example.org:{port}/a/b?x=1 HTTP/1.1\r\n"
                f"Host: videos.example.org:{port}\r\nProxy-Authorization: Basic x\r\n"
                f"Proxy-Connection: keep-alive\r\nConnection: keep-alive\r\nUser-Agent: t\r\n\r\n"
            ).encode()
            reply = await talk(proxy.port, request)
        assert reply.endswith(b"hello") and reply.startswith(b"HTTP/1.1 200")
        head = seen[0].decode()
        assert head.startswith("GET /a/b?x=1 HTTP/1.1\r\n")  # origin form, not the absolute URL
        assert f"Host: videos.example.org:{port}" in head and "User-Agent: t" in head
        assert "Proxy-" not in head and "keep-alive" not in head and "Connection: close" in head
    finally:
        server.close()


async def test_connect_tunnels_bytes_to_the_validated_address_only() -> None:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(1024):
            writer.write(data.upper())
            await writer.drain()
        writer.close()

    server, port = await origin(echo)
    calls: list[tuple[str, int, str]] = []

    async def validator(host: str, p: int, scheme: str) -> ValidatedUrl:
        calls.append((host, p, scheme))
        return ValidatedUrl(f"{scheme}://{host}/", scheme, host, p, "/", ("127.0.0.1",))

    try:
        async with EgressProxy(validator=validator) as proxy:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(f"CONNECT videos.example.org:{port} HTTP/1.1\r\n\r\n".encode())
            await writer.drain()
            assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
            writer.write(b"hello tunnel")
            await writer.drain()
            assert await asyncio.wait_for(reader.read(100), 3) == b"HELLO TUNNEL"
            writer.close()
        assert calls == [("videos.example.org", port, "https")]  # validated once, at connect time
    finally:
        server.close()


async def test_a_connection_is_cut_when_it_exceeds_its_byte_budget() -> None:
    async def flood(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        try:
            for _ in range(200):
                writer.write(b"x" * 1000)
                await writer.drain()
        except OSError:
            pass
        writer.close()

    server, port = await origin(flood)
    try:
        async with EgressProxy(validator=allow_to("127.0.0.1"), max_bytes=5000) as proxy:
            reply = await talk(
                proxy.port,
                f"GET http://v.example.org:{port}/ HTTP/1.1\r\nHost: v\r\n\r\n".encode(),
                read=10**6,
            )
        assert len(reply) == 5000  # cut exactly at the budget, not 200 kB
    finally:
        server.close()


# ── wired into the downloader ───────────────────────────────────────────────────
async def test_ytdlp_is_pointed_at_the_proxy_and_it_is_closed_afterwards(
    fake_ytdlp: Any, config: AppConfig, tmp_path: Path
) -> None:
    cfg = config.model_copy(update={"home": tmp_path / "h"})
    acquired = await fetch_url(
        "https://videos.example.org/watch?v=1", cfg, resolver=public, ytdlp_prefix=fake_ytdlp.prefix
    )
    argv = fake_ytdlp.argv()
    proxy_url = argv[argv.index("--proxy") + 1]
    assert proxy_url.startswith("http://127.0.0.1:")
    port = int(proxy_url.rsplit(":", 1)[1])
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)  # the proxy shut down with the download
    acquired.cleanup()

    acquired = await fetch_url(
        "https://videos.example.org/watch?v=2",
        cfg,
        resolver=public,
        ytdlp_prefix=fake_ytdlp.prefix,
        use_proxy=False,
    )
    assert "--proxy" not in fake_ytdlp.argv()
    acquired.cleanup()
