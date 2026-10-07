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

> **Status: pre-alpha.** Works on local files only. Agent mode (M4) is usable from a coding agent
> today: no API key, you supply captions for the transcript. Pipeline mode (M5: `--profile cloud`
> and `--profile local`) is built and tested offline but has not yet been run against real
> providers. URLs (M3) and packaging (M6) are not built yet. Follow progress in `docs/PLAN.md`.

## Install

You need [uv](https://docs.astral.sh/uv/). Until the first release is on PyPI, install from the
repository:

```bash
uv tool install git+https://github.com/lavondev/frame-ingest        # the CLI
uv tool install "frame-ingest[url,local] @ git+https://github.com/lavondev/frame-ingest"   # + URLs, local speech
frame-ingest doctor
```

**The skill.** Pick the route that matches your agent; all of them use the same `skills/frame-ingest/`:

| Agent | How |
|---|---|
| Claude Code | `/plugin marketplace add lavondev/frame-ingest`, then `/plugin install frame-ingest@frame-ingest` |
| Codex and other `.agents/skills` readers | clone the repo (it ships `.agents/skills/frame-ingest`), or copy `skills/frame-ingest` into `~/.agents/skills/` |
| Any skills installer | `npx skills add lavondev/frame-ingest` |
| By hand | download `frame-ingest-<version>.skill` from a release (a zip; verify it against `SHA256SUMS`) and unzip it into your agent's skills directory |

The skill's launcher uses an installed `frame-ingest`, then a repository checkout, then a pinned
release through `uvx`; it never downloads "latest" and never pipes a script into a shell.

## Try it

**With a coding agent (agent mode).** Copy or symlink `skills/frame-ingest/` into your agent's
skills directory (for Claude Code: `~/.claude/skills/frame-ingest`), then ask it to
`/frame-ingest path/to/video.mp4`. Add a caption file (`.srt` or `.vtt`) to get a transcript.
The agent looks at contact sheets of frames, writes the analysis as JSON, and the CLI validates
it and builds the document. Frames and transcript go to whichever model runs your agent.

**By hand.**

```bash
frame-ingest prepare video.mp4 --captions video.srt   # evidence pack + manifest
# ...write vision/*.json, corrections.json, synthesis.json under the reported output directory
frame-ingest assemble <job_id>                        # validate, then write the document
frame-ingest validate <document.md>
frame-ingest run video.mp4 --profile fake             # offline demo with canned output
```

**Pipeline mode (the CLI calls the models).**

```bash
frame-ingest estimate video.mp4 --profile cloud       # frames, tokens, cost, and the egress plan
frame-ingest run video.mp4 --profile cloud            # asks before sending anything (a terminal)
frame-ingest run video.mp4 --profile cloud --allow-egress --max-cost 2   # non-interactive
# fully local: uv tool install ".[local]", ollama pull the two models, then
frame-ingest doctor --profile local
frame-ingest run video.mp4 --profile local --offline
```

`cloud` needs `OPENAI_API_KEY` (or per-role `FRAME_INGEST_<ROLE>_API_KEY`) in the environment and
optionally `OPENAI_BASE_URL` for Groq, vLLM and other OpenAI-compatible servers. Before anything
is sent, `run` prints what goes where; with no terminal it refuses unless `--allow-egress` is
given or `egress: allow` is set. Prices for `--max-cost` go in `~/.frame-ingest/pricing.yaml`. Follow progress in `docs/PLAN.md`.

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
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): why the pipeline works the way it does.
- [`SECURITY.md`](SECURITY.md): how to report a vulnerability.
- [`AGENTS.md`](AGENTS.md): instructions for coding agents working in this repository.

## Development

```bash
uv sync
uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy
```

## License

To be decided before the first public release.
