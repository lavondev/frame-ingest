"""`--offline`: hard-block network use from this process.

While active, any connection or DNS lookup that is not loopback raises `OfflineViolation`.
Loopback stays open so the `local` profile can reach an Ollama or vLLM server on this machine.
Child processes (ffmpeg) are already restricted to local files and pipes by `guard/ffmpeg_args`.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from frame_ingest.errors import FrameIngestError

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


class OfflineViolation(FrameIngestError):
    code = "offline_violation"
    status = 403


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    host = host.strip("[]").lower()
    if host in _LOOPBACK_NAMES or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def _host_of(address: Any) -> str | None:
    if isinstance(address, tuple) and address:
        return str(address[0])
    if isinstance(address, str | bytes):  # AF_UNIX path: local by definition
        return "127.0.0.1"
    return None


@contextmanager
def block_network() -> Iterator[None]:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def check(host: str | None) -> None:
        if not is_loopback(host):
            raise OfflineViolation(
                f"Offline mode blocked a network connection to {str(host)[:80]!r}."
            )

    def connect(self: socket.socket, address: Any) -> None:
        check(_host_of(address))
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        check(_host_of(address))
        return real_connect_ex(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None:  # None is a wildcard bind address, not a lookup
            check(host.decode("ascii", "replace") if isinstance(host, bytes) else str(host))
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[assignment,method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[assignment,method-assign]
    socket.getaddrinfo = getaddrinfo
    try:
        yield
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = real_getaddrinfo
