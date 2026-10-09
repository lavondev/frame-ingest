"""URL ingest: the fixtures that concern URLs, redirects,
DNS, yt-dlp and argument injection. Nothing here touches the network: DNS is injected, HTTP is a
mock transport, and yt-dlp is a small fake executable."""

from __future__ import annotations

import json
import os
import stat
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest

from frame_ingest import cli as cli_mod
from frame_ingest.cli import main
from frame_ingest.config import AppConfig
from frame_ingest.fetch.acquire import fetch_url
from frame_ingest.fetch.http import FetchError, download_media, looks_like_direct_media
from frame_ingest.fetch.policy import UrlRejected, blocked_reason, literal_ip, validate_url
from frame_ingest.fetch.ytdlp import (
    YTDLP_FLOOR,
    YtdlpError,
    YtdlpTooOld,
    check_version,
    download_with_ytdlp,
    parse_version,
)
from frame_ingest.guard.ytdlp_args import (
    FORBIDDEN_FRAGMENTS,
    YtdlpArgsRejected,
    assert_safe,
    build_ytdlp_argv,
)
from tests.helpers import PUBLIC_IP, VTT, public

PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


async def validate(url: str, **kw: Any) -> Any:
    return await validate_url(url, resolver=kw.pop("resolver", public), **kw)


# ── URL policy ──────────────────────────────────────────────────────────────────
HOSTILE_URLS = [
    "http://169.254.169.254/latest/meta-data/",
    "http://169.254.169.254.nip.io/",  # a name: needs resolution, covered below
    "http://localhost/",
    "http://LOCALHOST:8080/",
    "http://foo.localhost/",
    "http://[::1]/",
    "http://0x7f000001/",
    "http://0X7F.0.0.1/",
    "http://2130706433/",
    "http://0177.0.0.1/",
    "http://127.1/",
    "http://127.0.0.1/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:7f00:1]/",
    "http://[::ffff:169.254.169.254]/",
    "http://10.0.0.1/",
    "http://192.168.1.1/",
    "http://172.16.0.1/",
    "http://172.31.255.255/",
    "http://100.64.0.1/",
    "http://100.100.100.200/",
    "http://[fd00:ec2::254]/",
    "http://[fe80::1]/",
    "http://0.0.0.0/",
    "http://[::]/",
    "http://224.0.0.1/",
    "http://[ff02::1]/",
    "http://255.255.255.255/",
    "http://[64:ff9b::7f00:1]/",
    "http://[2002:7f00:0001::]/",
    "http://metadata.google.internal/",
    "http://printer.local/",
    "http://host.localdomain/",
]


@pytest.mark.parametrize("url", [u for u in HOSTILE_URLS if "nip.io" not in u])
async def test_private_and_internal_destinations_are_refused(url: str) -> None:
    async def never(host: str, port: int) -> list[str]:  # names must not even be resolved
        raise AssertionError(f"resolved {host}")

    with pytest.raises(UrlRejected):
        await validate_url(url, resolver=never)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/x.mp4",
        "file:///etc/passwd",
        "gopher://example.com/",
        "javascript:alert(1)",
        "data:text/plain,hi",
        "rtmp://example.com/live",
        "//example.com/x",
        "example.com/x",
        "http:///x",
        "http://",
        f"http://user:pass@{PUBLIC_IP}/x",
        f"http://user@{PUBLIC_IP}/x",
        f"http://{PUBLIC_IP}:22/x",
        f"http://{PUBLIC_IP}:6379/x",
        f"http://{PUBLIC_IP}:65536/x",
        f"http://{PUBLIC_IP}/a b",
        f"http://{PUBLIC_IP}/a\nb",
        f"http://{PUBLIC_IP}/a\x00b",
        f"http://{PUBLIC_IP}/a\tb",
        f"http://{PUBLIC_IP}\\@evil.example/",
        "http://exаmple.com/",  # Cyrillic а: homograph
        "http://" + "a" * 3000 + ".com/",
        "",
    ],
)
async def test_malformed_and_unsafe_urls_are_refused(url: str) -> None:
    with pytest.raises(UrlRejected):
        await validate(url)


@pytest.mark.parametrize(
    "url",
    [
        f"http://{PUBLIC_IP}/a.mp4",
        f"https://{PUBLIC_IP}:8443/a.mp4?token=1#frag",
        f"https://[{PUBLIC_V6}]/a.mp4",
        "https://videos.example.org/watch?v=abc",
        "http://videos.example.org:8080/a.mp4",
    ],
)
async def test_ordinary_public_urls_are_accepted(url: str) -> None:
    v = await validate(url)
    assert v.ips and "#" not in v.url


async def test_dns_answers_are_checked_not_just_literals() -> None:
    async def private(host: str, port: int) -> list[str]:
        return ["10.1.2.3"]

    async def mixed(host: str, port: int) -> list[str]:  # rebinding trick: one good, one bad
        return [PUBLIC_IP, "169.254.169.254"]

    async def mapped(host: str, port: int) -> list[str]:
        return ["::ffff:127.0.0.1"]

    async def empty(host: str, port: int) -> list[str]:
        return []

    async def fails(host: str, port: int) -> list[str]:
        raise OSError("nope")

    for resolver in (private, mixed, mapped, empty, fails):
        with pytest.raises(UrlRejected):
            await validate_url("https://rebind.example.org/x", resolver=resolver)


def test_numeric_host_spellings_normalise_to_the_real_address() -> None:
    for spelling in ("0x7f000001", "2130706433", "0177.0.0.1", "127.1", "0x7f.1"):
        assert str(literal_ip(spelling)) == "127.0.0.1", spelling
    assert literal_ip("example.org") is None
    assert blocked_reason(literal_ip(PUBLIC_IP)) is None  # type: ignore[arg-type]
    with pytest.raises(UrlRejected):
        literal_ip("999.999.999.999")


async def test_shell_metacharacters_in_a_url_are_just_data() -> None:
    for tail in ("$(touch%20pwned)", "`touch%20pwned`", "a;touch%20pwned", "a|id", "a&b=c"):
        v = await validate(f"https://videos.example.org/{tail}")
        assert v.path_query.startswith("/")
    assert not Path("pwned").exists()


async def test_display_form_drops_the_query_string() -> None:
    v = await validate("https://videos.example.org:8443/a/b.mp4?token=SECRET#x")
    assert v.display == "https://videos.example.org:8443/a/b.mp4" and "SECRET" not in v.display


# ── the pinned-IP fetcher ───────────────────────────────────────────────────────
def serve(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def body(data: bytes, headers: dict[str, str] | None = None, status: int = 200) -> httpx.Response:
    """A streaming response, like a real network one (`content=` would arrive pre-read)."""
    return httpx.Response(status, headers=headers or {}, stream=httpx.ByteStream(data))


async def get(url: str, tmp_path: Path, handler: Any, *, resolver: Any = public, **kw: Any) -> Any:
    return await download_media(
        url,
        tmp_path / "dl",
        max_bytes=kw.pop("max_bytes", 1 << 20),
        resolver=resolver,
        transport=serve(handler),
        **kw,
    )


async def test_connects_to_the_validated_ip_with_the_original_host(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return body(b"\x00\x00\x00\x18ftypmp42" + b"x" * 100)

    got = await get("https://videos.example.org/v/clip.mp4?sig=abc", tmp_path, handler)
    req = seen[0]
    assert req.url.host == PUBLIC_IP  # the connection goes to the validated address ...
    assert req.headers["host"] == "videos.example.org"  # ... under the original name
    assert req.extensions["sni_hostname"] == "videos.example.org"  # TLS is checked against it
    assert req.headers["accept-encoding"] == "identity"
    assert got.path.name == "clip.mp4" and got.size > 100 and "sig=" not in got.source
    assert stat.S_IMODE(got.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(got.path.parent.stat().st_mode) == 0o700


async def test_dns_rebinding_between_check_and_connect_cannot_redirect_the_connection(
    tmp_path: Path,
) -> None:
    calls = {"n": 0}

    async def rebinding(host: str, port: int) -> list[str]:
        calls["n"] += 1
        return [PUBLIC_IP] if calls["n"] == 1 else ["127.0.0.1"]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == PUBLIC_IP  # never the second answer
        return body(b"data" * 10)

    await get("https://rebind.example.org/a.mp4", tmp_path, handler, resolver=rebinding)
    assert calls["n"] == 1


async def test_a_redirect_to_an_internal_address_is_refused(tmp_path: Path) -> None:
    hits: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(req.headers["host"])
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    with pytest.raises(UrlRejected):
        await get("https://videos.example.org/a.mp4", tmp_path, handler)
    assert hits == ["videos.example.org"]  # the metadata address was never requested


@pytest.mark.parametrize(
    "location",
    [
        "http://localhost:8080/a.mp4",
        "http://0x7f000001/a.mp4",
        "//127.0.0.1/a.mp4",
        "/\\@10.0.0.1/",
        "file:///etc/passwd",
    ],
)
async def test_every_redirect_hop_is_revalidated(location: str, tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": location})

    with pytest.raises(UrlRejected):
        await get("https://videos.example.org/a.mp4", tmp_path, handler)


async def test_a_redirect_that_resolves_to_a_private_name_is_refused(tmp_path: Path) -> None:
    async def resolver(host: str, port: int) -> list[str]:
        return ["10.0.0.9"] if host == "intranet.example.org" else [PUBLIC_IP]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://intranet.example.org/a.mp4"})

    with pytest.raises(UrlRejected, match="non-public"):
        await get("https://videos.example.org/a.mp4", tmp_path, handler, resolver=resolver)


async def test_redirects_are_followed_within_limits_and_never_downgrade(tmp_path: Path) -> None:
    def ok(req: httpx.Request) -> httpx.Response:
        if req.headers["host"] == "a.example.org":
            return httpx.Response(301, headers={"location": "https://b.example.org/final.mp4"})
        return body(b"video" * 20)

    got = await get("https://a.example.org/x.mp4", tmp_path, ok)
    assert got.path.name == "final.mp4"

    def loop(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://a.example.org/again.mp4"})

    with pytest.raises(FetchError, match="Too many redirects"):
        await get("https://a.example.org/x.mp4", tmp_path / "x", loop)

    def downgrade(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://a.example.org/plain.mp4"})

    with pytest.raises(UrlRejected, match="https to http"):
        await get("https://a.example.org/x.mp4", tmp_path / "y", downgrade)


async def test_size_caps_are_enforced_by_header_and_by_counting(tmp_path: Path) -> None:
    def declared(req: httpx.Request) -> httpx.Response:
        return body(b"x", {"content-length": "999999999"})

    with pytest.raises(FetchError, match="larger"):
        await get("https://v.example.org/a.mp4", tmp_path, declared, max_bytes=1000)

    def liar(req: httpx.Request) -> httpx.Response:  # no content-length, endless body
        return body(b"x" * 5000)

    with pytest.raises(FetchError, match="larger"):
        await get("https://v.example.org/b.mp4", tmp_path / "y", liar, max_bytes=1000)
    assert not any((tmp_path / "y" / "dl").glob("*"))  # the partial file was removed


async def test_html_errors_and_empty_bodies_are_clear(tmp_path: Path) -> None:
    def html(req: httpx.Request) -> httpx.Response:
        return body(b"<html>", {"content-type": "text/html"})

    with pytest.raises(FetchError, match="web page"):
        await get("https://v.example.org/a.mp4", tmp_path, html)
    with pytest.raises(FetchError, match="404"):
        await get("https://v.example.org/a.mp4", tmp_path / "b", lambda r: httpx.Response(404))
    with pytest.raises(FetchError, match="empty"):
        await get("https://v.example.org/a.mp4", tmp_path / "c", lambda r: body(b""))


async def test_an_existing_target_is_never_overwritten(tmp_path: Path) -> None:
    (tmp_path / "dl").mkdir()
    (tmp_path / "dl" / "a.mp4").write_text("keep")
    with pytest.raises(FetchError, match="overwrite"):
        await get("https://v.example.org/a.mp4", tmp_path, lambda r: body(b"x"))
    assert (tmp_path / "dl" / "a.mp4").read_text() == "keep"


async def test_total_deadline(tmp_path: Path) -> None:
    import asyncio

    async def slow(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return body(b"x")

    with pytest.raises(FetchError, match="did not finish"):
        await download_media(
            "https://v.example.org/a.mp4",
            tmp_path / "dl",
            max_bytes=100,
            resolver=public,
            transport=serve(slow),
            timeout_s=0.3,
        )


def test_direct_media_detection() -> None:
    assert looks_like_direct_media("https://x.org/a/b.MP4?x=1")
    assert not looks_like_direct_media("https://x.org/watch?v=a.mp4")
    assert not looks_like_direct_media("https://x.org/page")


# ── yt-dlp: argument allowlist ──────────────────────────────────────────────────
def test_ytdlp_argv_is_fixed_and_the_url_comes_after_double_dash(tmp_path: Path) -> None:
    for subs in ("none", "manual", "auto"):
        argv = build_ytdlp_argv(
            ["yt-dlp"],
            "https://v.example.org/watch?v=1",
            out_dir=tmp_path,
            max_filesize=10**9,
            subs=subs,
            skip_download=subs == "auto",
            proxy="http://127.0.0.1:9",
        )
        assert argv[-2:] == ["--", "https://v.example.org/watch?v=1"]
        assert {
            "--ignore-config",
            "--no-plugin-dirs",
            "--no-playlist",
            "--restrict-filenames",
        } <= set(argv)
        assert "--max-downloads" in argv and "--no-cache-dir" in argv
        assert not set(FORBIDDEN_FRAGMENTS) & set(argv)
        assert_safe(argv)
        assert argv[argv.index("-f") + 1] == "b[height<=1080]/b"  # one file: no merge step


@pytest.mark.parametrize(
    "url",
    ["--exec=id", "-o/etc/passwd", "file:///etc/passwd", "ftp://x/y", "https://x/\x00", ""],
)
def test_ytdlp_never_gets_an_option_or_non_http_url(url: str, tmp_path: Path) -> None:
    with pytest.raises(YtdlpArgsRejected):
        build_ytdlp_argv(["yt-dlp"], url, out_dir=tmp_path, max_filesize=1000)


def test_ytdlp_limits_are_bounded_and_forbidden_options_are_detected(tmp_path: Path) -> None:
    for kw in ({"max_filesize": 0}, {"max_filesize": 1 << 50}, {"socket_timeout": 0}):
        args: dict[str, Any] = {"max_filesize": 1000, **kw}
        with pytest.raises(YtdlpArgsRejected):
            build_ytdlp_argv(["yt-dlp"], "https://x.org/a", out_dir=tmp_path, **args)
    for bad in FORBIDDEN_FRAGMENTS:
        with pytest.raises(YtdlpArgsRejected):
            assert_safe(["yt-dlp", bad, "--", "https://x.org"])
        with pytest.raises(YtdlpArgsRejected):
            assert_safe(["yt-dlp", f"{bad}=x", "--", "https://x.org"])
    assert_safe(["yt-dlp", "--", "--exec"])  # after `--` it is only a (rejected) URL


# ── yt-dlp: version floor and run verification (a fake executable) ──────────────


async def run_yt(
    fake: Any, tmp_path: Path, url: str = "https://v.example.org/watch?v=1", **kw: Any
) -> Any:
    return await download_with_ytdlp(
        url, tmp_path / "in", max_bytes=1 << 30, prefix=fake.prefix, resolver=public, **kw
    )


def test_version_parsing_and_floor() -> None:
    assert (
        parse_version("2026.07.04") == (2026, 7, 4) and parse_version("2026.7.4\n") == YTDLP_FLOOR
    )
    with pytest.raises(YtdlpError):
        parse_version("garbage")


async def test_versions_below_the_floor_are_refused(fake_ytdlp: Any, tmp_path: Path) -> None:
    for old in ("2026.07.03", "2026.06.09", "2025.12.31"):
        fake_ytdlp.set(version=old)
        with pytest.raises(YtdlpTooOld, match="security"):
            await run_yt(fake_ytdlp, tmp_path)
    fake_ytdlp.set(version="2026.07.04")
    assert await check_version(fake_ytdlp.prefix) == "2026.07.04"
    fake_ytdlp.set(version="2027.01.01")
    assert await check_version(fake_ytdlp.prefix) == "2027.01.01"


async def test_a_clean_run_returns_the_video_and_manual_captions(
    fake_ytdlp: Any, tmp_path: Path
) -> None:
    fake_ytdlp.set(files=[["abc.mp4", f"copy:{fake_ytdlp.video}"], ["abc.en.vtt", f"text:{VTT}"]])
    res = await run_yt(fake_ytdlp, tmp_path)
    assert res.media.name == "abc.mp4" and res.captions and not res.captions_auto
    argv = fake_ytdlp.argv()
    assert "--ignore-config" in argv and argv[-2] == "--" and "--exec" not in argv


async def test_auto_captions_are_a_fallback_and_flagged(fake_ytdlp: Any, tmp_path: Path) -> None:
    fake_ytdlp.set(
        files=[["abc.mp4", f"copy:{fake_ytdlp.video}"], ["abc.en.auto.vtt", f"text:{VTT}"]]
    )
    res = await run_yt(fake_ytdlp, tmp_path)
    assert res.captions is not None and res.captions_auto


async def test_no_captions_is_fine(fake_ytdlp: Any, tmp_path: Path) -> None:
    res = await run_yt(fake_ytdlp, tmp_path)
    assert res.captions is None


@pytest.mark.parametrize(
    ("files", "why"),
    [
        ([["abc.mp4", "copy:VIDEO"], ["x.sh", "text:#!/bin/sh\nid"]], "not allowed"),
        ([["abc.mp4", "copy:VIDEO"], ["abc.desktop", "text:[Desktop Entry]"]], "not allowed"),
        ([["abc.mp4", "copy:VIDEO"], ["abc.url", "text:[InternetShortcut]"]], "not allowed"),
        ([["abc.mp4", "copy:VIDEO"], ["abc.webloc", "text:<plist/>"]], "not allowed"),
        ([["abc.mp4", "copy:VIDEO"], ["sub", "dir"]], "plain files"),
        ([["abc.mp4", "link:/etc/passwd"]], "plain files"),
        ([["abc.mp4", "copy:VIDEO"], ["abc.vtt", "link:/etc/passwd"]], "plain files"),
        ([["my video.mp4", "copy:VIDEO"]], "unexpected name"),
        ([["abc.mp4", "copy:VIDEO"], ["def.mp4", "copy:VIDEO"]], "more than one"),
        ([], "did not produce"),
    ],
)
async def test_the_output_directory_is_verified_before_anything_is_adopted(
    files: list[list[str]], why: str, fake_ytdlp: Any, tmp_path: Path
) -> None:
    files = [[n, h.replace("VIDEO", str(fake_ytdlp.video))] for n, h in files]
    fake_ytdlp.set(files=files)
    with pytest.raises(YtdlpError, match=why):
        await run_yt(fake_ytdlp, tmp_path)
    assert list((tmp_path / "in").iterdir()) == []  # the work directory was removed


async def test_failure_exit_codes_surface_a_short_message_and_no_env_leaks(
    fake_ytdlp: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-NOTFORYTDLP-1234567890")
    fake_ytdlp.set(exit=1)
    with pytest.raises(YtdlpError, match="fake failure KEYSET=0"):  # env was scrubbed
        await run_yt(fake_ytdlp, tmp_path)
    fake_ytdlp.set(exit=101)  # --max-downloads reached: success
    assert (await run_yt(fake_ytdlp, tmp_path / "ok")).media.name == "abc.mp4"


async def test_a_private_url_never_reaches_ytdlp(fake_ytdlp: Any, tmp_path: Path) -> None:
    with pytest.raises(UrlRejected):
        await run_yt(fake_ytdlp, tmp_path, url="http://169.254.169.254/latest")
    assert not (tmp_path / "in").exists()


async def test_oversized_downloads_are_refused(fake_ytdlp: Any, tmp_path: Path) -> None:
    with pytest.raises(YtdlpError, match="size limit"):
        await download_with_ytdlp(
            "https://v.example.org/w",
            tmp_path / "in",
            max_bytes=100,
            prefix=fake_ytdlp.prefix,
            resolver=public,
        )


# ── acquire + CLI ───────────────────────────────────────────────────────────────
@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fi-home"
    for name in list(os.environ):
        if name.startswith(("FRAME_INGEST_", "OPENAI_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FRAME_INGEST_HOME", str(home))
    home.mkdir(parents=True)
    return home


def patch_fetch(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> None:
    monkeypatch.setattr(cli_mod, "fetch_url", partial(fetch_url, resolver=public, **kw))


def cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any], str]:
    code = main([*argv, "--json"])
    out = capsys.readouterr()
    return code, json.loads(out.out) if out.out.strip() else {}, out.err


def video_server(video: Path) -> httpx.MockTransport:
    data = video.read_bytes()
    return serve(lambda req: body(data, {"content-type": "video/mp4"}))


def test_fetch_a_direct_media_url_into_a_job(
    home: Path,
    sample_video: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_fetch(monkeypatch, transport=video_server(sample_video))
    code, out, err = cli(capsys, "fetch", "https://videos.example.org/clip.mp4?sig=SECRET")
    assert code == 0 and out["source_url"] == "https://videos.example.org/clip.mp4"
    assert "SECRET" not in json.dumps(out) + err
    assert 23 < out["duration_s"] < 25 and out["captions"] is None
    assert Path(out["file"]).is_file() and out["file"].startswith(str(home / "jobs"))
    assert list((home / "incoming").iterdir()) == []  # the private download area is cleaned up


def test_a_url_goes_through_the_whole_pipeline_and_is_recorded_in_the_document(
    home: Path,
    sample_video: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_fetch(monkeypatch, transport=video_server(sample_video))
    code, out, _ = cli(capsys, "run", "https://videos.example.org/clip.mp4", "--profile", "fake")
    assert code == 0
    md = Path(out["outputs"]["md"]).read_text(encoding="utf-8")
    front = md.split("---\n")[1]
    assert "source_url: https://videos.example.org/clip.mp4" in front
    assert (
        "retrieved_at:" in front and "input_sha256:" in front and "frame_ingest_format: 1" in front
    )
    assert cli(capsys, "validate", out["outputs"]["md"])[0] == 0


def test_page_urls_use_ytdlp_and_its_captions_become_the_transcript(
    home: Path, fake_ytdlp: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_ytdlp.set(files=[["abc.mp4", f"copy:{fake_ytdlp.video}"], ["abc.en.vtt", f"text:{VTT}"]])
    patch_fetch(monkeypatch, ytdlp_prefix=fake_ytdlp.prefix)
    code, out, _ = cli(capsys, "prepare", "https://videos.example.org/watch?v=1")
    assert code == 0 and out["transcript_source"] == "captions" and out["transcript_segments"] == 1
    manifest = json.loads(Path(out["manifest"]).read_text(encoding="utf-8"))
    window = json.loads(Path(manifest["transcript"]["windows"][0]["file"]).read_text("utf-8"))
    assert window["target"][0]["text"] == "Hello from the captions."


def test_auto_generated_captions_are_labelled(
    home: Path, fake_ytdlp: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_ytdlp.set(
        files=[["abc.mp4", f"copy:{fake_ytdlp.video}"], ["abc.en.auto.vtt", f"text:{VTT}"]]
    )
    patch_fetch(monkeypatch, ytdlp_prefix=fake_ytdlp.prefix)
    code, out, _ = cli(capsys, "prepare", "https://videos.example.org/watch?v=2")
    assert code == 0 and out["transcript_source"] == "auto-captions"


def test_url_failures_have_distinct_exit_codes_and_leave_no_job(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_fetch(monkeypatch, transport=serve(lambda r: httpx.Response(404)))
    code, out, _ = cli(capsys, "fetch", "https://videos.example.org/gone.mp4")
    assert code == 1 and out["error"]["code"] == "fetch_failed"
    code, out, _ = cli(capsys, "fetch", "http://169.254.169.254/x.mp4")
    assert code == 3 and out["error"]["code"] == "url_rejected"
    assert not list((home / "jobs").glob("*")) and list((home / "incoming").iterdir()) == []


def test_ytdlp_missing_or_old_is_actionable(
    home: Path, fake_ytdlp: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_fetch(monkeypatch)  # no prefix: yt-dlp is not installed in the test environment
    monkeypatch.setattr("importlib.util.find_spec", lambda name, *a, **k: None)
    code, out, _ = cli(capsys, "fetch", "https://videos.example.org/watch?v=1")
    assert (
        code == 4
        and out["error"]["code"] == "missing_dependency"
        and ".[url]" in out["error"]["message"]
    )

    fake_ytdlp.set(version="2025.01.01")
    patch_fetch(monkeypatch, ytdlp_prefix=fake_ytdlp.prefix)
    code, out, _ = cli(capsys, "fetch", "https://videos.example.org/watch?v=1")
    assert code == 4 and out["error"]["code"] == "ytdlp_too_old"


def test_offline_refuses_to_download(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_fetch(
        monkeypatch, transport=serve(lambda r: (_ for _ in ()).throw(AssertionError("sent")))
    )
    code, out, _ = cli(
        capsys, "run", "https://videos.example.org/a.mp4", "--profile", "fake", "--offline"
    )
    assert code == 4 and "--offline" in out["error"]["message"]


def test_file_only_commands_still_refuse_urls(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = cli(capsys, "validate", "https://videos.example.org/doc.md")
    assert code == 3 and "needs a local document" in out["error"]["message"]


async def test_acquire_cleans_its_workspace_on_failure(config: AppConfig, tmp_path: Path) -> None:
    cfg = config.model_copy(update={"home": tmp_path / "h"})
    with pytest.raises(FetchError):
        await fetch_url(
            "https://videos.example.org/a.mp4",
            cfg,
            resolver=public,
            transport=serve(lambda r: httpx.Response(500)),
        )
    assert list((tmp_path / "h" / "incoming").iterdir()) == []


def test_doctor_reports_ytdlp(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("importlib.util.find_spec", lambda name, *a, **k: None)
    code, out, _ = cli(capsys, "doctor")
    names = {c["name"]: c for c in out["report"]["checks"]}
    assert code == 0 and names["yt-dlp"]["ok"] and "not installed" in names["yt-dlp"]["message"]
