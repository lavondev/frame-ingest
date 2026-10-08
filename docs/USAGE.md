# Usage

Three ways to run frame-ingest. Most people want the first.

## With a coding agent (agent mode)

Install the skill (see the [README](../README.md#install)), then ask for
`/frame-ingest path/to/video.mp4`. The agent makes four calls: `ingest` (checks the install,
transcribes speech on your machine, picks frames, builds contact sheets and writes fill-in
templates), then it fills the templates while looking at the sheets, runs `check` on each file,
and `finish` builds, validates and scans the document. Its reply ends with a coverage line such
as `audio yes · transcript asr · frames 8/8 analysed`. If the video has audio but no transcript
can be made, it stops and asks you: captions, frames only, or cloud speech (with your consent).
Frames and transcript go to whichever model runs your agent.

### How the skill launches the CLI

The skill's launcher runs the checkout it lives in, then an installed `frame-ingest`, then a pinned
release through `uvx`; it never downloads "latest" and never pipes a script into a shell.

## By hand

```bash
printf '%s' video.mp4 | frame-ingest ingest - --json   # task card: job id, files to fill, next command
# ...replace every TODO: in the files listed under "fill", checking each one:
frame-ingest check <file> --json
frame-ingest next <job_id> --json                       # where the job stands, the next command
frame-ingest finish <job_id> --json                     # build, validate and scan
frame-ingest run video.mp4 --profile fake               # offline demo with canned output
```

## Pipeline mode (the CLI calls the models)

Suits long videos.

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
given or `egress: allow` is set. Prices for `--max-cost` go in `~/.frame-ingest/pricing.yaml`.
Follow progress in [`PLAN.md`](PLAN.md).
