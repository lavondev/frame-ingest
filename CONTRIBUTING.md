# Contributing

Read `AGENTS.md` first: it holds the security rules every change must respect and a map of the
code. `docs/PLAN.md` has the design and the roadmap.

```bash
uv sync
uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy
```

All four must pass. Tests are offline: no network, no API keys. Provider calls sit behind
interfaces with fakes; URL tests inject DNS and HTTP.

- **Adding a CLI flag** fails `tests/test_security.py::test_flag_enumeration_every_option_is_reviewed`
  on purpose. Decide that the flag cannot execute code, write outside the job directory, reveal a
  secret or reach the network unasked, record why in that file, then add it.
- **Changing the document format** regenerates the golden file on purpose
  (`UPDATE_GOLDEN=1 uv run pytest tests/test_assemble.py`); review the diff in the PR.
- **Changing agent output models** needs `UPDATE_SCHEMAS=1 uv run pytest tests/test_agent.py`.
- **Releasing**: bump the version in `pyproject.toml`, `src/frame_ingest/__init__.py`,
  `plugin.json`, `.claude-plugin/*.json`, `skills/frame-ingest/SKILL.md` and the `PIN` in
  `skills/frame-ingest/scripts/fi` (a test checks they agree), update `CHANGELOG.md`, tag `vX.Y.Z`.

Report security problems privately (see `SECURITY.md`).
