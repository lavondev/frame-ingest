"""Release hygiene: versions agree everywhere, manifests validate, the skill zip is reproducible,
and no workflow uses an unpinned third-party action."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from frame_ingest import __version__

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "frame-ingest"


def _load_build_skill() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_skill", ROOT / "scripts" / "build_skill.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_version_agrees() -> None:
    py = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert py == __version__
    assert json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["version"] == py
    assert json.loads((ROOT / "plugin.json").read_text())["version"] == py
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    assert market["plugins"][0]["version"] == py
    front = yaml.safe_load((SKILL / "SKILL.md").read_text().split("---\n")[1])
    assert front["metadata"]["version"] == py
    launcher = (SKILL / "scripts" / "fi").read_text()
    assert f'PIN="{py}"' in launcher  # the launcher fetches exactly this release, never "latest"


def test_the_license_is_mit_everywhere() -> None:
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License") and "Permission is hereby granted" in text
    assert tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["license"] == "MIT"
    assert json.loads((ROOT / "plugin.json").read_text())["license"] == "MIT"
    assert json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["license"] == "MIT"
    assert yaml.safe_load((SKILL / "SKILL.md").read_text().split("---\n")[1])["license"] == "MIT"


def test_manifests_name_the_same_plugin_and_skill() -> None:
    names = {
        json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["name"],
        json.loads((ROOT / "plugin.json").read_text())["name"],
        json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())["plugins"][0]["name"],
        SKILL.name,
    }
    assert names == {"frame-ingest"}


def test_codex_discovers_the_same_skill_directory() -> None:
    link = ROOT / ".agents" / "skills" / "frame-ingest"
    assert link.is_symlink() and link.resolve() == SKILL.resolve()
    meta = yaml.safe_load((SKILL / "agents" / "openai.yaml").read_text())
    assert set(meta) <= {"interface", "policy", "dependencies"}
    assert meta["interface"]["display_name"] and "allow_implicit_invocation" in meta["policy"]


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_claude_validates_the_plugin_and_marketplace() -> None:
    for target in (".", ".claude-plugin/plugin.json"):
        res = subprocess.run(
            [shutil.which("claude") or "claude", "plugin", "validate", target],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert res.returncode == 0, res.stdout + res.stderr


def test_skill_zip_is_reproducible_and_complete(tmp_path: Path) -> None:
    build = _load_build_skill()
    first = build.build(tmp_path / "a")
    second = build.build(tmp_path / "b")
    assert first.read_bytes() == second.read_bytes()
    assert first.name == f"frame-ingest-{__version__}.skill"
    with zipfile.ZipFile(first) as zf:
        names = zf.namelist()
        assert names == sorted(names) and {n.split("/")[0] for n in names} == {"frame-ingest"}
        assert "frame-ingest/SKILL.md" in names and "frame-ingest/scripts/fi" in names
        launcher = zf.getinfo("frame-ingest/scripts/fi")
        assert (launcher.external_attr >> 16) & 0o111  # still executable
        assert all(i.date_time == (2026, 1, 1, 0, 0, 0) for i in zf.infolist())
        assert not [n for n in names if "__pycache__" in n or n.endswith(".DS_Store")]


def test_workflows_pin_every_third_party_action_to_a_commit() -> None:
    sha = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}( #.*)?$")
    offenders = []
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        for line in wf.read_text().splitlines():
            m = re.match(r"\s*-?\s*uses:\s*(\S.*)$", line)
            if m and not m.group(1).startswith("./") and not sha.match(m.group(1)):
                offenders.append(f"{wf.name}: {m.group(1)}")
    assert offenders == []


def test_release_workflow_publishes_through_oidc_with_attestations() -> None:
    wf = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    assert wf["permissions"] == {}  # nothing by default; each job asks for what it needs
    publish = wf["jobs"]["publish"]
    assert (
        publish["permissions"]["id-token"] == "write"
        and publish["permissions"]["attestations"] == "write"
    )
    text = (ROOT / ".github/workflows/release.yml").read_text()
    assert "PYPI_TOKEN" not in text and "password:" not in text  # trusted publishing only
    assert "sha256sum" in text and "attest-build-provenance" in text and ".skill" in text


def test_dependabot_covers_actions_and_python() -> None:
    cfg = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    assert {u["package-ecosystem"] for u in cfg["updates"]} >= {"github-actions", "uv"}


def test_wheel_metadata_declares_extras_and_script() -> None:
    proj = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert proj["scripts"]["frame-ingest"] == "frame_ingest.cli:main"
    assert {"local", "url"} <= set(proj["optional-dependencies"])
    assert any(d.startswith("httpx") for d in proj["dependencies"])
    assert sys.version_info >= (3, 11)
