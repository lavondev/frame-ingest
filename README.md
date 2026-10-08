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

> **Status: pre-alpha, feature complete, not yet field-tested.** Everything in the plan is built and
> tested offline: local files and URLs, agent mode and the skill, pipeline mode (cloud and local),
> security hardening and packaging. What has not happened yet is running it against the real
> world: a real provider key, a real harness (Claude Code, Codex), a live yt-dlp download, a
> tagged release and an external security review. See `docs/PLAN.md` for the exact list.

## Install

You need [uv](https://docs.astral.sh/uv/) and git. One step, for Claude Code and Codex:

```bash
git clone https://github.com/lavondev/frame-ingest ~/.frame-ingest-src
~/.frame-ingest-src/scripts/install.sh
```

Then, in a new Claude Code session, type `/frame-ingest <video file or URL>` (in Codex,
`$frame-ingest <video file or URL>`). Videos without captions are transcribed on your own
machine; nothing is uploaded for that.

To use only the command line, `uv tool install "frame-ingest[url,local] @
git+https://github.com/lavondev/frame-ingest"` puts `frame-ingest` on your PATH, then
`frame-ingest doctor`.

**Other ways to get the skill** (all use the same `skills/frame-ingest/`):

| Agent | How |
|---|---|
| Claude Code | `/plugin marketplace add lavondev/frame-ingest`, then `/plugin install frame-ingest@frame-ingest` |
| Codex and other `.agents/skills` readers | clone the repo (it ships `.agents/skills/frame-ingest`), or copy `skills/frame-ingest` into `~/.agents/skills/` |
| Any skills installer | `npx skills add lavondev/frame-ingest` |
| By hand | download `frame-ingest-<version>.skill` from a release (a zip; verify it against `SHA256SUMS`) and unzip it into your agent's skills directory |

The skill's launcher runs the checkout it lives in, then an installed `frame-ingest`, then a pinned
release through `uvx`; it never downloads "latest" and never pipes a script into a shell.

## Try it

**With a coding agent (agent mode).** Install as above, then ask for
`/frame-ingest path/to/video.mp4`. The agent makes four calls: `ingest` (checks the install,
transcribes speech on your machine, picks frames, builds contact sheets and writes fill-in
templates), then it fills the templates while looking at the sheets, runs `check` on each file,
and `finish` builds, validates and scans the document. Its reply ends with a coverage line such
as `audio yes · transcript asr · frames 8/8 analysed`. If the video has audio but no transcript
can be made, it stops and asks you: captions, frames only, or cloud speech (with your consent).
Frames and transcript go to whichever model runs your agent.

**By hand.**

```bash
printf '%s' video.mp4 | frame-ingest ingest - --json   # task card: job id, files to fill, next command
# ...replace every TODO: in the files listed under "fill", checking each one:
frame-ingest check <file> --json
frame-ingest next <job_id> --json                       # where the job stands, the next command
frame-ingest finish <job_id> --json                     # build, validate and scan
frame-ingest run video.mp4 --profile fake               # offline demo with canned output
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

## Evals

Three short videos with known answers, generated rather than stored (`evals/cases.json`):

| Case | What it tests | Expected |
|---|---|---|
| `speech-on-screen-text` | narration naming things written on the slides | transcript (`audio: yes`), the slide spelling used in corrections |
| `speech-under-music` | narration under a loud music bed | a transcript, if needed through the retry without voice filtering; never a quiet frames-only document |
| `silent-slides` | slides with a silent audio track | `needs_decision`; the agent asks, and only after you choose frames only: `audio: no` and the "Frames only" banner |

**CLI side, offline** (scripted speech-to-text and agent, runs in CI):

```bash
uv run pytest tests/test_evals.py
```

**Agent side, by hand.** This is the part that tells you whether a model follows the skill.

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

## Documentation

- [`docs/HARDENED-ARCHITECTURE.md`](docs/HARDENED-ARCHITECTURE.md): the ingest, check, finish
  design and the audio guarantee.

- [`docs/PLAN.md`](docs/PLAN.md): architecture, security model, roadmap and open decisions.
- [`docs/TESTING.md`](docs/TESTING.md): the quick try-it page; [`docs/TESTING-DEEP.md`](docs/TESTING-DEEP.md) has the thorough checks.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): why the pipeline works the way it does.
- [`SECURITY.md`](SECURITY.md): how to report a vulnerability.
- [`AGENTS.md`](AGENTS.md): instructions for coding agents working in this repository.

## Development

```bash
uv sync
uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy
```

## License

MIT, see [`LICENSE`](LICENSE).
