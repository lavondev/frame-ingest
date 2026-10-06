from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from frame_ingest import __version__
from frame_ingest.cli import main

ROOT = Path(__file__).resolve().parent.parent


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_args_prints_help_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "frame-ingest" in capsys.readouterr().out


def test_package_version_matches_pyproject() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == __version__
