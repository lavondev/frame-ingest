# evals

- `cases.json`: the three cases and what each should end with.
- `make_fixtures.py`: builds the videos into `evals/out/` (git-ignored) with Pillow and the bundled
  ffmpeg; real speech needs `say` or `espeak-ng`, otherwise a tone stands in.
- `score.py`: grades a run from what is on disk (jobs are found by the video's content hash).

To run them:

1. `uv run python evals/make_fixtures.py` builds the videos into `evals/out/`.
2. In Claude Code or Codex, ask the agent to run the skill on each video in `evals/out/`.
3. `uv run python evals/score.py` grades what is on disk and exits 0 when every check passes.
