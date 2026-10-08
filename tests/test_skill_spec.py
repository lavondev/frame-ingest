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
# None: the reference validator (`agentskills validate`) rejects harness extensions such as
# Claude Code's `argument-hint`, so the skill sticks to the spec.
EXTENSION_KEYS: set[str] = set()
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
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

from frame_ingest.cli import build_parser  # noqa: E402

ROOT = SKILLS_DIR.parent
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


def _reference_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in (FI_DIR / "references").glob("*.md"))


def test_every_command_in_skill_md_exists_and_goes_through_the_launcher() -> None:
    commands = set(_subcommands())
    text = _skill_text()
    used = set(re.findall(r"<(?:fi_path|launcher)> (\w+)", text))
    assert used <= commands, sorted(used - commands)
    assert {"ingest", "check", "finish", "next"} <= used  # the whole state machine
    in_refs = set(re.findall(r"`fi (\w+)", _reference_text()))
    assert in_refs <= commands, sorted(in_refs - commands)
    for line in (text + _reference_text()).splitlines():  # never the bare underlying binary
        assert not re.search(r"(^|[`\s])frame-ingest (doctor|run|prepare|assemble|ingest)", line)


def test_every_flag_in_skill_md_is_a_real_flag() -> None:
    real: set[str] = set()
    for sub in _subcommands().values():
        real |= {o for a in sub._actions for o in a.option_strings}
    flags = set(re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", _skill_text() + _reference_text()))
    assert flags <= real | {"--json"}, sorted(flags - real)


def test_skill_md_is_short_and_never_asks_for_a_shell_variable() -> None:
    """The exit-127 bug: SKILL.md told the agent to type `${CLAUDE_SKILL_DIR}/scripts/fi`, the
    variable was empty in its shell, and it ran `/scripts/fi`. The body must name the launcher by
    path and then rely on the `fi_path` the CLI returns."""
    _, body = _split(FI_DIR / "SKILL.md")
    assert len(body.splitlines()) <= 150
    for marker in ("${", "$CLAUDE", "$SKILL", "%CLAUDE"):
        assert marker not in body, marker
    assert "fi_path" in body and "scripts/fi" in body
    assert "needs_decision" in body and "Never choose for them" in body
    assert "reply_markdown" in body and "pasted exactly as given" in body


def test_description_front_loads_the_trigger_words() -> None:
    front, _ = _split(FI_DIR / "SKILL.md")
    desc = front["description"]
    assert len(desc) <= 1024
    head = desc[:90].lower()
    for word in ("video", "recording", "lecture", "meeting", "screen recording", "youtube/url"):
        assert word in head, word


def test_the_worked_example_is_a_valid_finished_set() -> None:
    from frame_ingest.agent.checks import (
        check_corrections,
        check_synthesis,
        find_placeholders,
        unverified_quotes,
    )
    from frame_ingest.agent.schemas import AgentSynthesis
    from frame_ingest.llm_schemas import CorrectionOut, VisionBatchOut
    from frame_ingest.models import Segment

    text = (FI_DIR / "references" / "example.md").read_text(encoding="utf-8")
    vision, corrections, synthesis = (
        json.loads(b) for b in re.findall(r"```json\n(.*?)```", text, re.DOTALL)
    )
    assert find_placeholders([vision, corrections, synthesis]) == []
    VisionBatchOut.model_validate(vision)
    corr = CorrectionOut.model_validate(corrections)
    segs = [
        Segment(id=c.id, start=t, end=t + 5, raw_text=c.corrected_text)
        for c, t in zip(corr.segments, (0.0, 12.5, 31.0), strict=True)
    ]
    assert check_corrections(corr, "corrections.json", segs)[1] == []
    syn = AgentSynthesis.model_validate(synthesis)
    assert check_synthesis(syn, "synthesis.json", 48.0, 24) == []
    assert unverified_quotes(syn, segs, 48.0) == 0


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


def test_every_launcher_route_brings_local_speech() -> None:
    """The audio guarantee needs faster-whisper: the checkout route and the pinned uvx route
    install the `local` extra; a `frame-ingest` already on PATH cannot be changed, so `doctor`
    warns and `prepare`/`ingest` stop with the exact fix when it lacks the extra."""
    text = (FI_DIR / "scripts" / "fi").read_text(encoding="utf-8")
    assert 'EXTRAS=${FRAME_INGEST_EXTRAS-"--extra local --extra url"}' in text  # 1. checkout
    assert '--from "frame-ingest[local,url]==$PIN"' in text  # 3. uvx
    assert 'uv sync --quiet --project "$repo" --extra local' in (
        ROOT / "scripts" / "install.sh"
    ).read_text(encoding="utf-8")


def test_doctor_warns_when_local_speech_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from frame_ingest.cli import main

    monkeypatch.setenv("FRAME_INGEST_HOME", str(tmp_path / "home"))
    main(["doctor", "--json"])  # conftest hides faster_whisper, as on a PATH install without it
    report = json.loads(capsys.readouterr().out)["report"]
    assert any("faster-whisper" in w and "--extra local" in w for w in report["warnings"])


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
    assert res.returncode == 4 and "scripts/install.sh" in res.stderr
    # Someone who never heard of uv must be told what it is and how to get it.
    assert "brew install uv" in res.stderr and "docs.astral.sh/uv" in res.stderr
    assert not re.search(r"\bcurl\b|\|\s*sh\b", res.stderr)  # no piped installer in agent output


def test_launcher_runs_the_cli_from_a_checkout() -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not installed")
    env = {
        **os.environ,
        "PATH": f"{Path(uv).parent}:/usr/bin:/bin",
        "UV_OFFLINE": "1",
        "FRAME_INGEST_EXTRAS": "",  # no heavy extras in the test
    }
    res = subprocess.run(
        ["/bin/sh", str(FI_DIR / "scripts" / "fi"), "--version"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert res.returncode == 0 and "frame-ingest" in res.stdout


def test_launcher_resolves_the_checkout_through_a_symlink(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not installed")
    skills = tmp_path / "agent-skills"
    skills.mkdir()
    (skills / "frame-ingest").symlink_to(FI_DIR)  # how install.sh links it
    env = {
        **os.environ,
        "PATH": f"{Path(uv).parent}:/usr/bin:/bin",
        "UV_OFFLINE": "1",
        "FRAME_INGEST_EXTRAS": "",
    }
    res = subprocess.run(
        ["/bin/sh", str(skills / "frame-ingest" / "scripts" / "fi"), "--version"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        cwd=tmp_path,
    )
    assert res.returncode == 0 and "frame-ingest" in res.stdout, res.stderr


def test_install_script_links_the_skill_for_claude_and_codex_and_is_safe(tmp_path: Path) -> None:
    script = ROOT / "scripts" / "install.sh"
    assert os.access(script, os.X_OK)
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "FRAME_INGEST_SKIP_WARM": "1"}

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(script)], env=env, capture_output=True, text=True, check=False
        )

    for _ in range(2):  # re-running is fine
        res = run()
        assert res.returncode == 0, res.stderr
        for where in (".claude/skills", ".agents/skills"):
            link = home / where / "frame-ingest"
            assert link.is_symlink() and link.resolve() == FI_DIR.resolve()
    assert "/frame-ingest" in res.stdout and "$frame-ingest" in res.stdout

    (home / ".claude" / "skills" / "frame-ingest").unlink()  # a real folder is never clobbered
    (home / ".claude" / "skills" / "frame-ingest").mkdir()
    (home / ".claude" / "skills" / "frame-ingest" / "mine.txt").write_text("keep")
    res = run()
    assert res.returncode == 0 and "skipped" in res.stderr
    assert (home / ".claude" / "skills" / "frame-ingest" / "mine.txt").read_text() == "keep"


def test_ingest_through_the_launcher_returns_the_launcher_path_to_reuse(
    tmp_path: Path, silent_video: Path
) -> None:
    """The exit-127 bug: the agent typed `${CLAUDE_SKILL_DIR}/scripts/fi` into a shell where the
    variable was empty. Now `ingest` returns `fi_path`, the launcher's absolute path as invoked
    (symlinks kept, so it matches the skill directory the harness knows), to reuse verbatim."""
    import json

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not installed")
    skills = tmp_path / "agent skills"
    skills.mkdir()
    (skills / "frame-ingest").symlink_to(FI_DIR)
    launcher = skills / "frame-ingest" / "scripts" / "fi"
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("FRAME_INGEST_")},
        "PATH": f"{Path(uv).parent}:/usr/bin:/bin",
        "UV_OFFLINE": "1",
        "FRAME_INGEST_EXTRAS": "",
        "FRAME_INGEST_HOME": str(tmp_path / "home"),
    }
    res = subprocess.run(
        [str(launcher), "ingest", "-", "--json"],
        input=str(silent_video),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    card = json.loads(res.stdout)
    assert card["fi_path"] == str(launcher) and card["state"] == "fill"
    assert card["next"].startswith(f"'{launcher}' check ")
