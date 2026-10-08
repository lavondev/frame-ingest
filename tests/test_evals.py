"""The CLI side of the evals (evals/cases.json), offline.

Each case's video is generated with tone stand-ins for the voice, and speech-to-text is a
scripted stand-in that behaves like faster-whisper would on the real thing: it hears the speech
case, hears the music case only with voice-activity filtering off, and hears nothing in the
silent case. A scripted agent fills the templates (copying the slide text it "sees"), and
evals/score.py, the scorer used for real agent runs, grades the result.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from evals.make_fixtures import SLIDE_S, build_case, load_cases
from evals.score import score_case

from frame_ingest.cli import main

CASES = {c["id"]: c for c in load_cases()}


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("evals")
    for case in CASES.values():
        build_case(case, out, tts=None)
    return out


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fi-home"
    for name in list(os.environ):
        if name.startswith(("FRAME_INGEST_", "OPENAI_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FRAME_INGEST_HOME", str(home))
    return home


def whisper_for(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """A faster_whisper stand-in that hears what the real one would on this case."""
    calls: list[bool] = []
    lines = case["speech"]

    class Model:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def transcribe(self, _samples: Any, **kw: Any) -> tuple[Any, Any]:
            calls.append(kw["vad_filter"])
            hears = case["audio"] == "speech" or (
                case["audio"] == "speech+music" and not kw["vad_filter"]
            )
            segs = [
                types.SimpleNamespace(start=i * SLIDE_S + 0.5, end=i * SLIDE_S + 4.0, text=t)
                for i, t in enumerate(lines)
            ]
            return iter(segs if hears else []), types.SimpleNamespace(language="en")

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = Model  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    return calls


def cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    code = main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def agent_fills(card: dict[str, Any], case: dict[str, Any]) -> None:
    """What a careful agent writes: the slide text it sees, verbatim quotes, no TODO left."""
    m = json.loads(Path(card["read"]["manifest"]).read_text(encoding="utf-8"))
    when = {f["name"]: f["t"] for f in m["frames"]}

    def todo_free(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: todo_free(x) for k, x in v.items() if k != "todo"}
        if isinstance(v, list):
            return [todo_free(x) for x in v]
        return "Described from the frames." if isinstance(v, str) and v.startswith("TODO") else v

    for row in card["fill"]:
        path = Path(row["file"])
        data = todo_free(json.loads(path.read_text(encoding="utf-8")))
        if row["kind"] == "vision-batch":
            for f in data["frames"]:
                slide = case["slides"][
                    min(int(when[f["frame"]] // SLIDE_S), len(case["slides"]) - 1)
                ]
                f["on_screen_text"] = [slide["title"], *slide["lines"]]
                f["scene_type"] = "slide"
        if row["kind"] == "synthesis" and case["speech"]:
            data["chapters"][0]["quotes"] = [{"t": 0.5, "text": case["speech"][0]}]
        path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_eval_case_cli_side(
    case_id: str,
    fixtures: Path,
    home: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = CASES[case_id]
    expect = case["expect"]
    calls = whisper_for(case, monkeypatch)
    code, card = cli(capsys, "ingest", str(fixtures / f"{case_id}.mp4"))
    assert card["state"] == expect["ingest_state"], card

    if expect["ingest_state"] == "needs_decision":
        assert code == 6 and card["decision"]["reason"] == expect["decision_reason"]
        assert len(calls) == expect["speech_attempts"]
        # the user chose frames only; the agent runs the command the card gave for that
        argv = shlex.split(card["after_decision"]["frames_only"])
        code, card = cli(capsys, *argv[1:-1])
        assert code == 0 and card["state"] == "fill" and card["coverage"]["audio"] == "no"
    else:
        assert code == 0 and card["coverage"]["audio"] == expect["coverage_audio"]
        assert calls == ([True] if case["audio"] == "speech" else [True, False])

    agent_fills(card, case)
    code, done = cli(capsys, "next", card["job_id"])
    assert done["state"] == "finish", done
    code, done = cli(capsys, "finish", card["job_id"])
    assert code == 0 and done["ok"], done

    checks = score_case(case, fixtures, home)
    assert [c for c in checks if not c.ok] == []
    assert len(checks) >= 5


def test_the_cases_cover_the_three_audio_situations() -> None:
    assert {c["audio"] for c in CASES.values()} == {"speech", "speech+music", "silence"}
    for case in CASES.values():
        assert case["slides"] and case["expect"]["on_screen_text"]
        assert case["expect"]["ingest_state"] in {"fill", "needs_decision"}
        assert bool(case["speech"]) == (case["audio"] != "silence")


def test_the_scorer_fails_a_case_that_was_never_run(fixtures: Path, home: Path) -> None:
    checks = score_case(CASES["speech-on-screen-text"], fixtures, home)
    assert [(c.name, c.ok) for c in checks] == [("ingested", False)]
