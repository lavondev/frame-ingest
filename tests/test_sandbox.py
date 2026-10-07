"""OS sandbox for ffmpeg (macOS sandbox-exec, Linux bwrap). The confinement tests run real
commands inside the real sandbox where one is available, and compare against the same command
unsandboxed so a pass cannot be an accident of the environment."""

from __future__ import annotations

import asyncio
import http.server
import json
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from frame_ingest.cli import main
from frame_ingest.ffmpeg import ffmpeg_exe, run_ffmpeg
from frame_ingest.guard import sandbox
from frame_ingest.guard.paths import job_jail
from frame_ingest.guard.subproc import run_process


@pytest.fixture(autouse=True)
def _restore_sandbox_state() -> Iterator[None]:
    saved = dict(sandbox._state)
    yield
    sandbox._state.update(saved)


@pytest.fixture
def jail_dir(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "job"
    root.mkdir()
    with job_jail(root):
        yield root


async def real_backend() -> str:
    sandbox.reset_probe()
    kind = await sandbox.backend()
    if kind is None:
        pytest.skip("no working OS sandbox on this machine")
    return kind


async def sandboxed(argv: list[str], jail: Path | None, exe_dir: Path) -> Any:
    cmd = await sandbox.wrap(argv, jail=jail, exe_dir=exe_dir)
    return await run_process(cmd, timeout=30, cwd=jail)


# ── modes ───────────────────────────────────────────────────────────────────────
async def test_off_and_unavailable_auto_leave_the_command_alone(tmp_path: Path) -> None:
    argv = ["/bin/echo", "hi"]
    sandbox.configure("off")
    assert await sandbox.wrap(argv, jail=tmp_path, exe_dir=tmp_path) == argv
    sandbox.configure("auto")
    sandbox._state["backend"] = None
    assert await sandbox.wrap(argv, jail=tmp_path, exe_dir=tmp_path) == argv


async def test_require_refuses_to_run_without_a_sandbox(tmp_path: Path, jail_dir: Path) -> None:
    sandbox.configure("require")
    sandbox._state["backend"] = None
    with pytest.raises(sandbox.SandboxUnavailable, match="require"):
        await sandbox.wrap(["/bin/echo"], jail=tmp_path, exe_dir=tmp_path)
    with pytest.raises(sandbox.SandboxUnavailable):
        await run_ffmpeg(["-version"], loglevel="info", timeout=10)


async def test_unsafe_paths_cannot_break_out_of_the_macos_profile(tmp_path: Path) -> None:
    if (await real_backend()) != "sandbox-exec":
        pytest.skip("macOS only")
    evil = tmp_path / 'x") (allow file-write* (subpath "/'
    evil.mkdir()
    with pytest.raises(sandbox.SandboxUnavailable, match="characters"):
        await sandbox.wrap(["/usr/bin/true"], jail=evil, exe_dir=tmp_path)


# ── confinement, against the real sandbox ───────────────────────────────────────
async def test_writes_are_possible_only_inside_the_job_directory(
    tmp_path: Path, jail_dir: Path
) -> None:
    await real_backend()
    outside = tmp_path / "outside.txt"
    await sandboxed(["/bin/sh", "-c", f"echo x > '{outside}'"], jail_dir, Path("/bin"))
    assert not outside.exists()  # nothing persists on the host (bwrap may accept it in a tmpfs)
    inside = jail_dir / "inside.txt"
    res = await sandboxed(["/bin/sh", "-c", f"echo x > '{inside}'"], jail_dir, Path("/bin"))
    assert res.returncode == 0 and inside.read_text().strip() == "x"
    plain = tmp_path / "unsandboxed.txt"  # the same command, unsandboxed, succeeds
    res = await run_process(["/bin/sh", "-c", f"echo x > '{plain}'"], timeout=10)
    assert res.returncode == 0 and plain.exists()


async def test_the_home_directory_cannot_be_read(
    tmp_path: Path, jail_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await real_backend()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    secret = fake_home / "secret.txt"
    secret.write_text("sk-live-secret")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    open_ = await run_process(["/bin/cat", str(secret)], timeout=10)
    assert open_.returncode == 0 and b"sk-live" in open_.stdout  # readable without the sandbox
    res = await sandboxed(["/bin/cat", str(secret)], jail_dir, Path("/bin"))
    assert res.returncode != 0 and b"sk-live" not in res.stdout


async def test_there_is_no_network_not_even_loopback(jail_dir: Path) -> None:
    await real_backend()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"reachable")

        def log_message(self, *_a: Any) -> None:
            return None

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        curl = ["/usr/bin/curl", "-s", "-m", "5", url]
        plain = await run_process(curl, timeout=15)
        assert plain.returncode == 0 and plain.stdout == b"reachable"
        res = await sandboxed(curl, jail_dir, Path("/usr/bin"))
        assert res.returncode != 0 and res.stdout == b""
    finally:
        server.shutdown()


# ── ffmpeg itself ───────────────────────────────────────────────────────────────
async def test_ffmpeg_runs_inside_the_sandbox_and_stays_functional(
    jail_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kind = await real_backend()
    sandbox.configure("auto")
    seen: list[list[str]] = []
    real = run_process

    async def spy(cmd: Any, **kw: Any) -> Any:
        seen.append(list(cmd))
        return await real(cmd, **kw)

    monkeypatch.setattr("frame_ingest.ffmpeg.run_process", spy)
    out = jail_dir / "tone.ogg"
    res = await run_ffmpeg(
        ["-y", "-f", "lavfi", "-i", "sine=duration=1", "-c:a", "libopus", str(out)], timeout=60
    )
    assert res.returncode == 0 and out.stat().st_size > 0
    wrapper = "/usr/bin/sandbox-exec" if kind == "sandbox-exec" else "bwrap"
    assert seen[0][0].endswith(wrapper) and ffmpeg_exe() in seen[0]


def test_a_full_pipeline_run_works_sandboxed(
    tmp_path: Path,
    sample_video: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sandbox.reset_probe()
    if asyncio.run(sandbox.backend()) is None:
        pytest.skip("no working OS sandbox on this machine")
    monkeypatch.setenv("FRAME_INGEST_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("FRAME_INGEST_SANDBOX", "require")
    code = main(["run", str(sample_video), "--profile", "fake", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"], out


def test_doctor_reports_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FRAME_INGEST_HOME", str(tmp_path / "h"))
    code = main(["doctor", "--json"])
    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["report"]["checks"]}
    assert code == 0 and "mode: auto" in checks["sandbox"]["message"]


def test_sandbox_mode_is_validated_in_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FRAME_INGEST_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("FRAME_INGEST_SANDBOX", "sometimes")
    assert main(["doctor", "--json"]) == 4
    assert sys.version_info >= (3, 11)
