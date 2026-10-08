"""M7: pacing and hook metrics, speaker labels, export, playlists."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from frame_ingest.agent.validate_doc import validate_document
from frame_ingest.cli import main
from frame_ingest.fetch.ytdlp import YtdlpError, list_playlist
from frame_ingest.guard.ytdlp_args import (
    MAX_PLAYLIST_ITEMS,
    YtdlpArgsRejected,
    assert_safe,
    build_ytdlp_list_argv,
)
from frame_ingest.pipeline.assemble import render_markdown
from frame_ingest.pipeline.metrics import compute_metrics
from tests.helpers import VTT, public
from tests.test_assemble import make_analysis
from tests.test_fetch import patch_fetch


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fi-home"
    for name in list(os.environ):
        if name.startswith(("FRAME_INGEST_", "OPENAI_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FRAME_INGEST_HOME", str(home))
    home.mkdir(parents=True)
    return home


def cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any], str]:
    code = main([*argv, "--json"])
    out = capsys.readouterr()
    return code, json.loads(out.out) if out.out.strip() else {}, out.err


# ── metrics ─────────────────────────────────────────────────────────────────────
def test_metrics_are_computed_exactly_from_the_analysis() -> None:
    m = compute_metrics(make_analysis())  # 90 s, 18 words in segments 1-12 s and 50-60 s
    assert m.words_per_minute == 12.0
    assert m.speech_coverage == pytest.approx(21 / 90, abs=1e-4)
    assert m.longest_silence_s == 38.0  # the gap between 12 s and 50 s
    h = m.hook
    assert h.window_s == 15.0 and h.first_speech_at_s == 1.0 and h.words_in_window == 9
    assert h.opening_line == "Welcome to the Widget Frobnicator."
    assert h.on_screen_text_blocks_in_window == 2  # the frame at 1 s lists two text blocks
    assert [c.chapter_id for c in m.chapters] == ["ch-01", "ch-02"]
    assert m.chapters[0].words_per_minute == pytest.approx(9 / 0.75, abs=0.1)


def test_metrics_survive_a_video_without_speech() -> None:
    m = compute_metrics(make_analysis(has_audio=False))
    assert m.words_per_minute is None and m.speech_coverage is None and m.longest_silence_s is None
    assert m.hook.first_speech_at_s is None and m.hook.opening_line is None


def test_the_metrics_section_renders_and_the_document_still_validates() -> None:
    a = make_analysis()
    plain = render_markdown(a)
    with_metrics = render_markdown(a.model_copy(update={"metrics": compute_metrics(a)}))
    assert "Pacing and Hook" not in plain
    assert "## Pacing and Hook {#pacing}" in with_metrics and "12 words/min" in with_metrics
    assert validate_document(with_metrics) == []
    assert validate_document(plain) == []


def test_metrics_flag_on_run_and_assemble(
    home: Path, sample_video: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = cli(capsys, "run", str(sample_video), "--profile", "fake", "--metrics")
    assert code == 0
    side = json.loads(Path(out["outputs"]["json"]).read_text(encoding="utf-8"))
    assert side["metrics"]["hook"]["window_s"] == 15.0 and side["metrics"]["words_per_minute"]
    md = Path(out["outputs"]["md"]).read_text(encoding="utf-8")
    assert "## Pacing and Hook" in md and cli(capsys, "validate", out["outputs"]["md"])[0] == 0

    code, out, _ = cli(capsys, "run", str(sample_video), "--profile", "fake")
    assert "## Pacing and Hook" not in Path(out["outputs"]["md"]).read_text(encoding="utf-8")


# ── diarization labels ──────────────────────────────────────────────────────────
def test_speaker_labels_appear_when_diarized(
    home: Path, sample_video: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = cli(capsys, "run", str(sample_video), "--profile", "fake", "--diarize")
    assert code == 0
    md = Path(out["outputs"]["md"]).read_text(encoding="utf-8")
    assert "** A: " in md and "** B: " in md
    code, out, _ = cli(capsys, "run", str(sample_video), "--profile", "fake")
    assert "** A: " not in Path(out["outputs"]["md"]).read_text(encoding="utf-8")


def test_diarize_is_refused_for_local_speech(
    home: Path, sample_video: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = cli(capsys, "run", str(sample_video), "--profile", "local", "--diarize")
    assert code == 2 and "--diarize" in out["error"]["message"]


# ── export ──────────────────────────────────────────────────────────────────────
@pytest.fixture
def finished(home: Path, sample_video: Path, capsys: pytest.CaptureFixture[str]) -> str:
    code, out, _ = cli(capsys, "run", str(sample_video), "--profile", "fake")
    assert code == 0
    return str(out["job_id"])


@pytest.fixture
def notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "notes"
    root.mkdir()
    monkeypatch.setenv("FRAME_INGEST_EXPORT_ROOTS", str(root))
    return root


def test_export_needs_a_configured_root(
    finished: str, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, out, _ = cli(capsys, "export", finished, "--to", str(tmp_path / "anywhere"))
    assert code == 4 and out["error"]["code"] == "export_refused"
    assert "export_roots" in out["error"]["message"] and not (tmp_path / "anywhere").exists()


def test_export_writes_markdown_and_json_inside_the_root(
    finished: str, notes: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = cli(capsys, "export", finished, "--to", "videos")
    assert code == 0 and len(out["written"]) == 2
    md = (notes / "videos" / "sample.md").read_text(encoding="utf-8")
    assert md.startswith("---\n") and "trust: untrusted-content" in md
    assert json.loads((notes / "videos" / "sample.json").read_text(encoding="utf-8"))["trust"]
    assert cli(capsys, "validate", str(notes / "videos" / "sample.md"))[0] == 0
    code, out, _ = cli(capsys, "export", finished, "--to", "videos")  # never overwrites
    assert code == 4 and "not overwriting" in out["error"]["message"]


def test_obsidian_style_adds_properties_and_keeps_the_body(
    finished: str, notes: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(capsys, "export", finished, "--to", "obs", "--style", "obsidian")[0] == 0
    text = (notes / "obs" / "sample.md").read_text(encoding="utf-8")
    front = yaml.safe_load(text.split("---\n")[1])
    assert "frame-ingest" in front["tags"] and front["cssclasses"] == ["frame-ingest"]
    assert front["aliases"] and front["trust"] == "untrusted-content"
    assert cli(capsys, "validate", str(notes / "obs" / "sample.md"))[0] == 0


@pytest.mark.parametrize("target", ["../escape", "/tmp/frame-ingest-escape", ".hidden/x", "a/.git"])  # noqa: S108
def test_export_refuses_destinations_outside_or_hidden(
    target: str, finished: str, notes: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = cli(capsys, "export", finished, "--to", target)
    assert code == 4 and out["error"]["code"] == "export_refused"
    assert not (notes.parent / "escape").exists()


def test_export_refuses_a_symlink_that_leaves_the_root(
    finished: str, notes: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (notes / "link").symlink_to(outside)
    code, _, _ = cli(capsys, "export", finished, "--to", "link")
    assert code == 4 and list(outside.iterdir()) == []


@pytest.mark.parametrize("root", ["/", "/etc", "/usr/local", "/System"])
def test_export_roots_may_not_be_system_locations(
    root: str, finished: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FRAME_INGEST_EXPORT_ROOTS", root)
    code, out, _ = cli(capsys, "export", finished, "--to", root)
    assert code == 4 and "system location" in out["error"]["message"]


def test_export_roots_may_not_be_hidden_in_home(
    finished: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_home = tmp_path / "home"
    (fake_home / ".ssh").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("FRAME_INGEST_EXPORT_ROOTS", str(fake_home / ".ssh"))
    code, out, _ = cli(capsys, "export", finished, "--to", str(fake_home / ".ssh" / "x"))
    assert code == 4 and "hidden" in out["error"]["message"]
    assert not (fake_home / ".ssh" / "x").exists()


def test_export_of_an_unfinished_job_is_a_clear_error(
    home: Path, sample_video: Path, notes: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = cli(capsys, "estimate", str(sample_video), "--profile", "fake")[1]["job_id"]
    code, out, _ = cli(capsys, "export", job, "--to", "x")
    assert code == 1 and out["error"]["code"] == "not_ready"


# ── playlists ───────────────────────────────────────────────────────────────────
ENTRIES = {
    "entries": [
        {"url": "https://videos.example.org/watch?v=1"},
        {"url": "https://videos.example.org/watch?v=2"},
        {"url": "http://169.254.169.254/latest"},
        {"url": "ftp://videos.example.org/x"},
        {"id": "no-url"},
        {"url": 12},
        {"url": "https://videos.example.org/watch?v=3"},
    ]
}


async def test_playlist_entries_are_revalidated_and_capped(fake_ytdlp: Any) -> None:
    fake_ytdlp.set(listing=ENTRIES)
    urls, refused = await list_playlist(
        "https://videos.example.org/playlist?list=1",
        max_items=5,
        prefix=fake_ytdlp.prefix,
        resolver=public,
    )
    # the cap counts entries considered (5): the 3 bad ones among them are refused, and the
    # 7th entry is never looked at
    assert urls == [
        "https://videos.example.org/watch?v=1",
        "https://videos.example.org/watch?v=2",
    ]
    assert refused == 3
    argv = fake_ytdlp.argv()
    assert "--flat-playlist" in argv and argv[argv.index("--playlist-end") + 1] == "5"
    assert argv[-2] == "--"

    urls, _ = await list_playlist(
        "https://videos.example.org/playlist?list=1",
        max_items=1,
        prefix=fake_ytdlp.prefix,
        resolver=public,
    )
    assert len(urls) == 1


async def test_a_single_video_listing_is_just_that_video(fake_ytdlp: Any) -> None:
    fake_ytdlp.set(listing={"id": "abc", "title": "ignored"})
    urls, refused = await list_playlist(
        "https://videos.example.org/watch?v=9",
        max_items=3,
        prefix=fake_ytdlp.prefix,
        resolver=public,
    )
    assert urls == ["https://videos.example.org/watch?v=9"] and refused == 0


async def test_bad_listings_are_clear_errors(fake_ytdlp: Any) -> None:
    for bad in ("not json at all", "[1, 2, 3]"):
        fake_ytdlp.set(listing=bad)
        if bad.startswith("["):
            urls, _ = await list_playlist(
                "https://videos.example.org/p",
                max_items=2,
                prefix=fake_ytdlp.prefix,
                resolver=public,
            )
            assert urls == ["https://videos.example.org/p"]  # not an object: treated as single
        else:
            with pytest.raises(YtdlpError, match="could not be read"):
                await list_playlist(
                    "https://videos.example.org/p",
                    max_items=2,
                    prefix=fake_ytdlp.prefix,
                    resolver=public,
                )
    fake_ytdlp.set(list_exit=1, listing={})
    with pytest.raises(YtdlpError, match="could not list"):
        await list_playlist(
            "https://videos.example.org/p", max_items=2, prefix=fake_ytdlp.prefix, resolver=public
        )


def test_playlist_argv_is_capped_and_never_downloads() -> None:
    argv = build_ytdlp_list_argv(["yt-dlp"], "https://x.org/p", max_items=MAX_PLAYLIST_ITEMS)
    assert_safe(argv)
    assert "--flat-playlist" in argv and "-f" not in argv and "-o" not in argv
    for n in (0, MAX_PLAYLIST_ITEMS + 1, -1):
        with pytest.raises(YtdlpArgsRejected):
            build_ytdlp_list_argv(["yt-dlp"], "https://x.org/p", max_items=n)
    with pytest.raises(YtdlpArgsRejected):
        build_ytdlp_list_argv(["yt-dlp"], "--exec=id", max_items=2)


def test_fetch_allow_playlist_makes_one_job_per_valid_entry(
    home: Path,
    fake_ytdlp: Any,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from functools import partial

    from frame_ingest import cli as cli_mod

    fake_ytdlp.set(
        listing=ENTRIES,
        files=[["abc.mp4", f"copy:{fake_ytdlp.video}"], ["abc.en.vtt", f"text:{VTT}"]],
    )
    patch_fetch(monkeypatch, ytdlp_prefix=fake_ytdlp.prefix)
    monkeypatch.setattr(
        cli_mod, "list_playlist", partial(list_playlist, prefix=fake_ytdlp.prefix, resolver=public)
    )
    code, out, _ = cli(
        capsys,
        "fetch",
        "https://videos.example.org/playlist?list=1",
        "--allow-playlist",
        "--max-items",
        "2",
    )
    assert code == 0 and [bool(i.get("job_id")) for i in out["items"]] == [True, True]
    # both entries serve the same bytes here, and a job is keyed by content: one job, reused
    assert out["items"][0]["job_id"] == out["items"][1]["job_id"]
    assert len(list((home / "jobs").iterdir())) == 1
    assert all("?" not in i["url"] for i in out["items"])


def test_playlist_flag_rules(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        cli(capsys, "fetch", "https://v.example.org/p", "--allow-playlist", "--max-items", "99")[0]
        == 2
    )
    assert cli(capsys, "fetch", "video.mp4", "--allow-playlist")[0] == 2
