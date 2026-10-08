# evals

- `cases.json`: the three cases and what each should end with.
- `make_fixtures.py`: builds the videos into `evals/out/` (git-ignored) with Pillow and the bundled
  ffmpeg; real speech needs `say` or `espeak-ng`, otherwise a tone stands in.
- `score.py`: grades a run from what is on disk (jobs are found by the video's content hash).

How to run them, offline and by hand in Claude Code and Codex: the "Evals" section of the
repository README.
