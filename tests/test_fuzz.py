"""Property-based fuzzing of everything that parses untrusted input.

Each target must either succeed or raise its own typed error, never crash, hang or let something
unsafe through. Hypothesis keeps a database of failures in .hypothesis/ (git-ignored)."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import random
import socket
import time
import unicodedata
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from frame_ingest.agent.captions import parse_captions
from frame_ingest.agent.validate_doc import scan_document, validate_document
from frame_ingest.errors import FrameIngestError, MediaError
from frame_ingest.fetch.policy import (
    DEFAULT_PORTS,
    UrlRejected,
    blocked_reason,
    literal_ip,
    validate_url,
)
from frame_ingest.fetch.ytdlp import YtdlpError, parse_version
from frame_ingest.guard.ffmpeg_args import ArgRejected, build_argv
from frame_ingest.guard.media import check_container, sniff_container
from frame_ingest.guard.scan import scan_text
from frame_ingest.guard.text import clean, neutralize_line
from frame_ingest.pipeline.probe import parse_ffmpeg_info

FAST = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
SLOW = settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
PUBLIC = "93.184.216.34"


async def public(host: str, port: int) -> list[str]:
    return [PUBLIC]


# ── containers ──────────────────────────────────────────────────────────────────
@FAST
@given(st.binary(max_size=2000))
def test_sniffing_arbitrary_bytes_never_crashes(data: bytes) -> None:
    found = sniff_container(data)
    assert found is None or found.demuxer


@FAST
@given(st.binary(max_size=2000))
def test_check_container_only_ever_raises_media_error(
    tmp_path_factory: pytest.TempPathFactory, data: bytes
) -> None:
    path = tmp_path_factory.mktemp("c") / "x.mp4"
    path.write_bytes(data)
    with contextlib.suppress(MediaError):
        check_container(path)


@FAST
@given(
    st.sampled_from([b"#EXTM3U\n", b"ffconcat version 1.0\n", b"v=0\r\n", b"<?xml ", b"{"]),
    st.binary(max_size=300),
)
def test_playlist_like_prefixes_are_always_refused(
    tmp_path_factory: pytest.TempPathFactory, prefix: bytes, tail: bytes
) -> None:
    path = tmp_path_factory.mktemp("p") / "x.mp4"
    path.write_bytes(prefix + tail)
    with pytest.raises(MediaError):
        check_container(path)


# ── ffmpeg output and argument parsing ──────────────────────────────────────────
@FAST
@given(st.text(max_size=3000))
def test_ffmpeg_banner_parsing_never_crashes(text: str) -> None:
    info = parse_ffmpeg_info(text)
    assert isinstance(info, dict)


ARG = st.one_of(
    st.sampled_from(
        ["-i", "-f", "-vf", "-map", "-t", "-ss", "-y", "-an", "-protocol_whitelist", "-"]
    ),
    st.text(max_size=40),
    st.sampled_from(
        ["lavfi", "null", "http://x/y", "file:///etc/passwd", "movie=a", "../x", "/etc/passwd"]
    ),
)


@FAST
@given(st.lists(ARG, max_size=12))
def test_ffmpeg_argv_builder_rejects_or_emits_only_safe_commands(
    tmp_path_factory: pytest.TempPathFactory, args: list[str]
) -> None:
    jail = tmp_path_factory.mktemp("jail")
    try:
        out = build_argv(args, jail=jail)
    except (ArgRejected, FrameIngestError):
        return
    assert out[:4] == ["-hide_banner", "-nostdin", "-loglevel", "error"]
    joined = " ".join(out)
    assert "://" not in joined and "movie" not in joined and "/etc/" not in joined
    # every input is protocol-restricted
    for i, tok in enumerate(out):
        if tok == "-i":
            assert out[i - 4 : i - 2] == ["-protocol_whitelist", "file,pipe"]


# ── text, captions, documents ───────────────────────────────────────────────────
@FAST
@given(st.text(max_size=400))
def test_clean_leaves_no_control_or_format_characters(text: str) -> None:
    out = clean(text)
    assert all(
        ch in "\n\t" or unicodedata.category(ch) not in {"Cc", "Cf", "Cs", "Co", "Cn"} for ch in out
    )


@FAST
@given(st.text(max_size=300))
def test_neutralised_lines_cannot_form_markdown_structure(text: str) -> None:
    line = neutralize_line(" ".join(clean(text).split()))
    assert "<" not in line
    assert not line.lstrip().startswith(("#", ">", "-", "*", "+", "|", "=", "_", "~", "`"))
    assert "{#" not in line.replace("\\{#", "")
    import re

    assert not re.search(r"(?<!\\)[\[\]]", line)


@FAST
@given(st.text(max_size=2500), st.floats(min_value=1, max_value=7200, allow_nan=False))
def test_caption_parser_is_total_and_well_formed(text: str, duration: float) -> None:
    try:
        segs = parse_captions(text, duration)
    except MediaError:
        return
    assert [s.id for s in segs] == list(range(len(segs)))
    assert all(0 <= s.start <= s.end <= duration for s in segs)
    assert [s.start for s in segs] == sorted(s.start for s in segs)
    assert all(s.raw_text.strip() and "<" not in s.raw_text for s in segs)


CUE = st.builds(
    lambda a, b, t: f"00:00:{a:02d}.000 --> 00:00:{b:02d}.500\n{t}\n",
    st.integers(0, 58),
    st.integers(0, 58),
    st.text(
        alphabet=st.characters(exclude_categories=["Cs"], exclude_characters="\r"), max_size=60
    ),
)


@FAST
@given(st.lists(CUE, max_size=30))
def test_caption_parser_survives_structured_noise(cues: list[str]) -> None:
    with contextlib.suppress(MediaError):
        parse_captions("WEBVTT\n\n" + "\n".join(cues), 60)


@FAST
@given(st.text(max_size=3000))
def test_document_validator_and_scanner_are_total(text: str) -> None:
    assert isinstance(validate_document(text), list)
    assert set(scan_document(text)) == {"flags", "lines"}


def test_the_scanner_is_linear_on_adversarial_input() -> None:
    for pattern in ("ignore ", "assistant ", "run the ", "a" * 50 + " ", "https://", "[" * 10):
        text = pattern * (200_000 // len(pattern))
        start = time.perf_counter()
        scan_text(text)
        assert time.perf_counter() - start < 3.0, pattern


# ── URLs ────────────────────────────────────────────────────────────────────────
@FAST
@given(st.text(max_size=300))
def test_url_policy_only_ever_raises_url_rejected(url: str) -> None:
    with contextlib.suppress(UrlRejected):
        asyncio.run(validate_url(url, resolver=public))


def spellings(ip: ipaddress.IPv4Address) -> list[str]:
    n = int(ip)
    a, b, c, d = ip.packed
    return [
        str(ip),
        str(n),
        hex(n),
        f"0{a:o}.0{b:o}.0{c:o}.0{d:o}",
        f"0x{a:x}.0x{b:x}.0x{c:x}.0x{d:x}",
        f"{a}.{n & 0xFFFFFF}",
        f"{a}.{b}.{n & 0xFFFF}",
    ]


@FAST
@given(st.integers(0, 2**32 - 1), st.sampled_from(range(7)))
def test_every_ipv4_spelling_is_judged_by_the_real_address(n: int, which: int) -> None:
    ip = ipaddress.IPv4Address(n)
    text = spellings(ip)[which]
    real = ipaddress.IPv4Address(socket.inet_aton(text))
    assert real == ip
    parsed = literal_ip(text)
    assert parsed == ip
    url = f"http://{text}/x.mp4"
    try:
        asyncio.run(validate_url(url, resolver=public))
        accepted = True
    except UrlRejected:
        accepted = False
    port_ok = 80 in DEFAULT_PORTS
    assert accepted == (blocked_reason(ip) is None and port_ok)
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        assert not accepted


@FAST
@given(st.integers(0, 2**128 - 1))
def test_ipv6_forms_embedding_private_ipv4_are_never_accepted(n: int) -> None:
    ip = ipaddress.IPv6Address(n)
    embedded = []
    if ip.ipv4_mapped is not None:
        embedded.append(ip.ipv4_mapped)
    if blocked_reason(ip) is None:
        assert ip.is_global and not ip.is_multicast
        assert all(e.is_global for e in embedded)


@FAST
@given(st.text(max_size=40))
def test_ytdlp_version_parsing_is_total(text: str) -> None:
    with contextlib.suppress(YtdlpError):
        parse_version(text)


# ── mutated real media through the whole engine ─────────────────────────────────
@SLOW
@given(st.integers(0, 2**31), st.integers(1, 12), st.booleans())
def test_corrupted_video_files_fail_cleanly_or_work(
    sample_video: Path,
    tmp_path_factory: pytest.TempPathFactory,
    seed: int,
    flips: int,
    truncate: bool,
) -> None:
    from frame_ingest.config import load_config
    from frame_ingest.engine import Engine
    from frame_ingest.providers.base import ProviderBundle
    from frame_ingest.providers.fake import FakeText, FakeTranscriber, FakeVision

    rng = random.Random(seed)  # noqa: S311 - reproducible test data, not security
    data = bytearray(sample_video.read_bytes())
    for _ in range(flips):
        data[rng.randrange(len(data))] = rng.randrange(256)
    if truncate:
        del data[rng.randrange(1, len(data)) :]
    work = tmp_path_factory.mktemp("fuzz")
    victim = work / "v.mp4"
    victim.write_bytes(bytes(data))
    config = load_config(work / "home", env={})
    engine = Engine(config, lambda: ProviderBundle(FakeTranscriber(), FakeVision(), FakeText()))
    start = time.perf_counter()
    with contextlib.suppress(FrameIngestError):
        asyncio.run(engine.create(victim))
    assert time.perf_counter() - start < 60
    jobs = list((work / "home" / "jobs").glob("*")) if (work / "home" / "jobs").exists() else []
    assert all((j / "job.json").exists() for j in jobs)  # no half-made job directories left behind
