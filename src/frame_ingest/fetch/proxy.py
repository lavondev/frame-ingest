"""Egress-guard proxy: a tiny local HTTP/CONNECT proxy that yt-dlp is pointed at.

A host check before launching yt-dlp is racy (the name can resolve differently later, and
extractors follow redirects of their own). This proxy applies the URL policy at *connect time*,
for every connection yt-dlp makes: it resolves the name itself, refuses any non-public address,
and connects to the address it validated. Loopback-only listener, a connection cap, idle
timeouts and a byte budget per connection.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from types import TracebackType
from urllib.parse import urlsplit

from frame_ingest.fetch.policy import (
    DEFAULT_PORTS,
    Resolver,
    UrlRejected,
    ValidatedUrl,
    validate_url,
)

MAX_HEAD = 16 * 1024
IDLE_S = 120.0
MAX_CONNECTIONS = 32
Validator = Callable[[str, int, str], Awaitable[ValidatedUrl]]  # (host, port, scheme)


class EgressProxy:
    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        ports: frozenset[int] = DEFAULT_PORTS,
        max_bytes: int = 8 * 1024**3,
        validator: Validator | None = None,
    ) -> None:
        self._resolver = resolver
        self._ports = ports
        self._max_bytes = max_bytes
        self._validator = validator or self._default_validator
        self._server: asyncio.Server | None = None
        self._active = 0
        self.refused: list[str] = []
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _default_validator(self, host: str, port: int, scheme: str) -> ValidatedUrl:
        host_part = f"[{host}]" if ":" in host else host
        return await validate_url(
            f"{scheme}://{host_part}:{port}/", resolver=self._resolver, ports=self._ports
        )

    async def __aenter__(self) -> EgressProxy:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # ── one client connection ───────────────────────────────────────────────────────
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._active >= MAX_CONNECTIONS:
            await self._reply(writer, 503, "Busy")
            return
        self._active += 1
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), IDLE_S)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            await self._reply(writer, 400, "Bad request", reader)
            self._active -= 1
            return
        try:
            if len(head) > MAX_HEAD:
                await self._reply(writer, 431, "Header too large")
                return
            await self._route(head, reader, writer)
        except Exception:
            with contextlib.suppress(Exception):
                await self._reply(writer, 502, "Bad gateway")
        finally:
            self._active -= 1

    async def _route(
        self, head: bytes, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/"):
            await self._reply(writer, 400, "Bad request", reader)
            return
        method, target = parts[0], parts[1]
        if method.upper() == "CONNECT":
            host, _, port_s = target.rpartition(":")
            if not port_s.isdigit() or not host:
                await self._reply(writer, 400, "Bad request")
                return
            await self._connect(host.strip("[]"), int(port_s), "https", None, reader, writer)
            return
        split = urlsplit(target)
        if split.scheme != "http" or not split.hostname:
            await self._reply(
                writer, 400, "Only absolute http URLs and CONNECT are supported", reader
            )
            return
        path = (split.path or "/") + (f"?{split.query}" if split.query else "")
        keep = [
            ln for ln in lines[1:] if ln and not ln.lower().startswith(("proxy-", "connection:"))
        ]
        rewritten = (
            f"{method} {path} {parts[2]}\r\n" + "\r\n".join(keep) + "\r\nConnection: close\r\n\r\n"
        ).encode("latin-1")
        try:
            port = split.port or 80
        except ValueError:
            await self._reply(writer, 400, "Bad request", reader)
            return
        await self._connect(split.hostname, port, "http", rewritten, reader, writer)

    async def _connect(
        self,
        host: str,
        port: int,
        scheme: str,
        first: bytes | None,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            v = await self._validator(host, port, scheme)
        except UrlRejected as exc:
            self.refused.append(f"{host}:{port}")
            await self._reply(writer, 403, exc.message[:120])
            return
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(v.ips[0], port), 30)
        except (OSError, TimeoutError):
            await self._reply(writer, 502, "Could not connect")
            return
        if first is None:
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
        else:
            up_w.write(first)
            await up_w.drain()
        budget = [self._max_bytes]
        await asyncio.gather(
            self._pipe(reader, up_w, budget),
            self._pipe(up_r, writer, budget),
            return_exceptions=True,
        )

    async def _pipe(
        self, src: asyncio.StreamReader, dst: asyncio.StreamWriter, budget: list[int]
    ) -> None:
        try:
            while chunk := await asyncio.wait_for(src.read(65536), IDLE_S):
                over = len(chunk) > budget[0]
                chunk = chunk[: max(0, budget[0])]  # deliver up to the budget, then cut
                budget[0] -= len(chunk)
                if chunk:
                    dst.write(chunk)
                    await dst.drain()
                if over:
                    break
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            with contextlib.suppress(Exception):
                dst.close()

    @staticmethod
    async def _reply(
        writer: asyncio.StreamWriter,
        status: int,
        text: str,
        reader: asyncio.StreamReader | None = None,
    ) -> None:
        with contextlib.suppress(Exception):
            body = text.encode("ascii", "replace")
            writer.write(
                f"HTTP/1.1 {status} {text[:40]}\r\nContent-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n".encode("ascii", "replace")
                + body
            )
            await writer.drain()
            if reader is not None:  # closing with unread input makes TCP reset and eat the reply
                got = 0
                while got < (1 << 20):
                    chunk = await asyncio.wait_for(reader.read(65536), 0.3)
                    if not chunk:
                        break
                    got += len(chunk)
        with contextlib.suppress(Exception):
            writer.close()
