# frame-ingest

Turn a video (a local file or a URL) into a structured, citable Markdown document: a corrected
transcript, chapters, per-chapter summaries, a glossary and an entity index, with stable anchors
and timestamps you can cite.

It is built as an **Agent Skill plus a CLI**, so any coding agent that supports skills can use it:

- Claude Code: `/frame-ingest <video file or URL>`
- Codex: `$frame-ingest <video file or URL>`
- Other harnesses that read `.agents/skills/` (Gemini CLI, Cursor, GitHub Copilot, OpenCode, Cline
  and others): ask for it by name, for example "frame-ingest this video".
- Local models: harnesses launched through Ollama work the same way, and a local profile can use
  Ollama for the vision and text stages.

> **Status: pre-alpha.** Nothing is usable yet. The repository currently holds the plan, the
> project skeleton and a placeholder skill. Follow progress in `docs/PLAN.md`.

## What makes it different

- **Citable output.** Deterministic Markdown with stable anchors (`ch-01`, `t-000125`), uniform
  timestamps and a JSON sidecar. Structure comes from code, not from a model.
- **Corrected transcripts.** Speech-to-text errors are fixed using what was actually on screen.
- **Safe on untrusted input.** Video content is treated as hostile data, URLs and media go through
  a hardened ingestion path, and nothing is sent to a cloud provider without consent.
- **Works with any model.** In agent mode the host agent does the vision and writing, and the CLI
  validates and assembles the result. In pipeline mode the CLI calls providers directly, which
  suits long videos.
- **Never pay twice.** Every stage is cached and resumable.

## Documentation

- [`docs/PLAN.md`](docs/PLAN.md): architecture, security model, roadmap and open decisions.
- [`SECURITY.md`](SECURITY.md): how to report a vulnerability.
- [`AGENTS.md`](AGENTS.md): instructions for coding agents working in this repository.

## Development

```bash
uv sync
uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy
```

## License

To be decided before the first public release.
