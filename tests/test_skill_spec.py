"""Guards the skill against drifting from the Agent Skills spec (https://agentskills.io/specification).

This is a lightweight stand-in until `skills-ref validate` is wired into CI (docs/PLAN.md, M6).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
SPEC_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
# Claude Code only; ignored (harmlessly) by other harnesses.
EXTENSION_KEYS = {"argument-hint"}
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _skill_dirs() -> list[Path]:
    return sorted(p.parent for p in SKILLS_DIR.glob("*/SKILL.md"))


def _split(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    assert match, f"{path} must start with YAML frontmatter"
    front = yaml.safe_load(match.group(1))
    assert isinstance(front, dict)
    return front, match.group(2)


def test_frame_ingest_skill_exists() -> None:
    assert (SKILLS_DIR / "frame-ingest" / "SKILL.md").is_file()


@pytest.mark.parametrize("skill_dir", _skill_dirs(), ids=lambda p: p.name)
def test_skill_matches_spec(skill_dir: Path) -> None:
    front, body = _split(skill_dir / "SKILL.md")

    unknown = set(front) - SPEC_KEYS - EXTENSION_KEYS
    assert not unknown, f"unknown frontmatter keys: {sorted(unknown)}"

    name = front["name"]
    assert isinstance(name, str)
    assert 1 <= len(name) <= 64
    assert NAME_RE.match(name), "name must be lowercase alphanumerics and single hyphens"
    assert name == skill_dir.name, "name must match the directory name"

    description = front["description"]
    assert isinstance(description, str)
    assert description.strip()
    assert len(description) <= 1024

    if "compatibility" in front:
        assert 1 <= len(front["compatibility"]) <= 500

    if "metadata" in front:
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in front["metadata"].items())

    assert len(body.splitlines()) < 500, "keep SKILL.md under 500 lines; move detail to references/"


# ── the frame-ingest skill specifically (docs/PLAN.md T9) ───────────────────────
import argparse  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

from frame_ingest.cli import build_parser  # noqa: E402

FI_DIR = SKILLS_DIR / "frame-ingest"


def _subcommands() -> dict[str, argparse.ArgumentParser]:
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _skill_text() -> str:
    return (FI_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_allowed_tools_pre_approve_only_the_launcher_and_read() -> None:
    front, _ = _split(FI_DIR / "SKILL.md")
    assert front["allowed-tools"] == "Bash(${CLAUDE_SKILL_DIR}/scripts/fi *) Read"
    assert "Write" not in front["allowed-tools"] and "WebFetch" not in front["allowed-tools"]


def test_skill_has_no_network_fetches_or_pipes_to_interpreters() -> None:
    text = _skill_text() + "\n".join(
        p.read_text(encoding="utf-8") for p in (FI_DIR / "references").glob("*.md")
    )
    assert not re.search(r"\b(curl|wget|Invoke-WebRequest|iwr)\b", text)
    assert not re.search(r"\|\s*(sh|bash|zsh|python3?|node|perl|ruby)\b", text)


def test_every_command_in_skill_md_exists_and_goes_through_the_launcher() -> None:
    commands = set(_subcommands())
    mentioned = re.findall(r"\bfi (?:\S+ )?(\w+)", _skill_text())
    used = {m for m in mentioned if m in commands | {"doctor"}}
    assert set(commands) >= used  # only real commands
    assert {"doctor", "estimate", "prepare", "assemble", "validate", "scan", "run"} <= used
    for line in _skill_text().splitlines():  # no bare invocations of the underlying binary
        assert not re.search(r"(^|[`\s])frame-ingest (doctor|run|prepare|assemble)", line)


def test_every_flag_in_skill_md_is_a_real_flag() -> None:
    real: set[str] = set()
    for sub in _subcommands().values():
        real |= {o for a in sub._actions for o in a.option_strings}
    flags = set(re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", _skill_text()))
    assert flags <= real | {"--json"}, sorted(flags - real)


def test_referenced_files_exist() -> None:
    for ref in set(re.findall(r"references/[\w./*-]+", _skill_text())):
        if "*" in ref:
            assert list(FI_DIR.glob(ref)), ref
        else:
            assert (FI_DIR / ref).exists(), ref


def test_launcher_is_posix_sh_and_executable() -> None:
    launcher = FI_DIR / "scripts" / "fi"
    assert os.access(launcher, os.X_OK)
    assert subprocess.run(["sh", "-n", str(launcher)], check=False).returncode == 0


def test_launcher_explains_how_to_install_when_nothing_is_available() -> None:
    env = {"PATH": "/usr/bin:/bin"}  # no frame-ingest, no uv
    res = subprocess.run(
        ["/bin/sh", str(FI_DIR / "scripts" / "fi"), "--version"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if shutil.which("uv", path="/usr/bin:/bin"):  # pragma: no cover - uv in a system dir
        pytest.skip("uv is on the minimal PATH")
    assert res.returncode == 4 and "uv tool install" in res.stderr


def test_launcher_runs_the_cli_from_a_checkout() -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not installed")
    env = {**os.environ, "PATH": f"{Path(uv).parent}:/usr/bin:/bin", "UV_OFFLINE": "1"}
    res = subprocess.run(
        ["/bin/sh", str(FI_DIR / "scripts" / "fi"), "--version"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert res.returncode == 0 and "frame-ingest" in res.stdout
