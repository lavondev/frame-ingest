# Evals

Three short videos with known answers, generated rather than stored (`evals/cases.json`):

| Case | What it tests | Expected |
|---|---|---|
| `speech-on-screen-text` | narration naming things written on the slides | transcript (`audio: yes`), the slide spelling used in corrections |
| `speech-under-music` | narration under a loud music bed | a transcript, if needed through the retry without voice filtering; never a quiet frames-only document |
| `silent-slides` | slides with a silent audio track | `needs_decision`; the agent asks, and only after you choose frames only: `audio: no` and the "Frames only" banner |

## CLI side, offline

Scripted speech-to-text and agent; runs in CI.

```bash
uv run pytest tests/test_evals.py
```

## Agent side, by hand

This is the part that tells you whether a model follows the skill.

1. Make the videos with real speech (macOS `say`, or `espeak-ng` on Linux):
   `uv run python evals/make_fixtures.py` (they land in `evals/out/`).
2. Optionally keep eval jobs apart from your own: `export FRAME_INGEST_HOME=$PWD/evals/home` in
   the terminal you start the agent from.
3. In **Claude Code** (`claude`, or `claude --model <haiku|sonnet|opus>` to compare models) run
   each case in a fresh session:
   `/frame-ingest evals/out/speech-on-screen-text.mp4`, then `speech-under-music.mp4`, then
   `silent-slides.mp4`. For the silent case the agent must stop and ask; answer "frames only".
4. In **Codex** (`codex`), the same three, invoked as `$frame-ingest evals/out/<case>.mp4`.
5. Score what is on disk: `uv run python evals/score.py` (add `--home "$FRAME_INGEST_HOME"` if
   you set it). Every check should say PASS. Also read each reply: it should give the document
   path, the TL;DR, the chapters and the coverage line, and never pick an option for you.
