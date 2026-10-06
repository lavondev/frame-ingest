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
