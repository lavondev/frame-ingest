"""URL policy (PLAN T2): which URLs we will ever connect to.

Only http/https, no credentials in the URL, an allowlisted port, and every address the host
resolves to must be a public one. The numeric host forms that browsers and C libraries accept
(`0x7f000001`, `2130706433`, `0177.0.0.1`, `127.1`) are normalised before the check, and
IPv4-mapped / 6to4 / NAT64 IPv6 forms are unwrapped, so none of them can smuggle a private
address past it. The fetcher then connects to the address that was validated, never to a name.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

from frame_ingest.errors import FrameIngestError
from frame_ingest.guard.netblock import is_loopback

MAX_URL_LENGTH = 2048
DEFAULT_PORTS = frozenset({80, 443, 8080, 8443})
_BAD_CHARS = re.compile(r"[\x00-\x20\x7f\\]|[^\x00-\x7f]")
_NUMERIC_HOST = re.compile(r"^(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+)){0,3}$", re.IGNORECASE)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIXTOFOUR = ipaddress.ip_network("2002::/16")

Resolver = Callable[[str, int], Awaitable[list[str]]]


class UrlRejected(FrameIngestError):
    code = "url_rejected"
    status = 400


@dataclass(frozen=True)
class ValidatedUrl:
    url: str  # without fragment
    scheme: str
    host: str
    port: int
    path_query: str
    ips: tuple[str, ...]  # public addresses the host resolved to (validated)

    @property
    def display(self) -> str:
        """Safe to print or store: no query string (it may hold tokens)."""
        default = 443 if self.scheme == "https" else 80
        port = "" if self.port == default else f":{self.port}"
        path = self.path_query.split("?", 1)[0]
        return f"{self.scheme}://{self.host}{port}{path}"


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def parse_url(url: str, *, ports: frozenset[int] = DEFAULT_PORTS) -> tuple[SplitResult, int]:
    if len(url) > MAX_URL_LENGTH:
        raise UrlRejected("The URL is too long.")
    if _BAD_CHARS.search(url):
        raise UrlRejected(
            "The URL contains whitespace, control or non-ASCII characters (use percent-encoding)."
        )
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UrlRejected("The URL is malformed.") from None
    if parts.scheme not in {"http", "https"}:
        raise UrlRejected("Only http and https URLs are supported.")
    if parts.username is not None or parts.password is not None:
        raise UrlRejected("URLs with embedded credentials are refused.")
    if not parts.hostname:
        raise UrlRejected("The URL has no host.")
    effective = port or (443 if parts.scheme == "https" else 80)
    if effective not in ports:
        raise UrlRejected(f"Port {effective} is not allowed (allowed: {sorted(ports)}).")
    return parts, effective


def literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a host string denotes if it is an IP in any accepted spelling, else None."""
    h = host.strip("[]")
    try:
        return ipaddress.ip_address(h.split("%")[0])
    except ValueError:
        pass
    if _NUMERIC_HOST.match(h):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(h))
        except OSError:
            raise UrlRejected("The host looks like an IP address but is not valid.") from None
    return None


def blocked_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why an address may not be contacted, or None when it is a public unicast address."""
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            candidates.append(ip.ipv4_mapped)
        if ip in _NAT64:
            candidates.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip in _SIXTOFOUR:
            candidates.append(ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF))
    for c in candidates:
        if c.is_multicast:
            return "a multicast address"
        if not c.is_global:
            return "a private, loopback, link-local or otherwise non-public address"
    return None


async def validate_url(
    url: str,
    *,
    resolver: Resolver | None = None,
    ports: frozenset[int] = DEFAULT_PORTS,
) -> ValidatedUrl:
    resolver = resolver or system_resolver  # looked up at call time so tests can replace it
    parts, port = parse_url(url, ports=ports)
    host = parts.hostname or ""
    literal = literal_ip(host)
    if literal is not None:
        addrs = [str(literal)]
    else:
        if is_loopback(host) or host.endswith((".local", ".internal", ".localdomain")):
            raise UrlRejected("The host is a local name.")
        try:
            addrs = await resolver(host, port)
        except OSError:
            raise UrlRejected(f"The host {host[:80]!r} could not be resolved.") from None
        if not addrs:
            raise UrlRejected(f"The host {host[:80]!r} did not resolve.")
    for addr in addrs:
        reason = blocked_reason(ipaddress.ip_address(addr.split("%")[0]))
        if reason:
            raise UrlRejected(f"The host resolves to {reason}; refusing to connect.")
    path_query = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return ValidatedUrl(
        url=url.split("#", 1)[0],
        scheme=parts.scheme,
        host=host.lower(),
        port=port,
        path_query=path_query,
        ips=tuple(addrs),
    )
