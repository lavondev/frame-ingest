# Modes: who does the looking

| Situation | Command | Speech | Who reads the frames | Cost |
|---|---|---|---|---|
| Default (with or without a key) | `fi ingest -` | on this machine (faster-whisper) | you, the host agent | the user's plan usage |
| The user wants local models (Ollama running) | `fi ingest - --profile local` | on this machine | a local vision model | free, slower |
| An API key is set **and** the user says yes | `fi ingest - --profile cloud --allow-egress` | cloud speech API | a cloud vision model | API cents per video |
| Video over ~20 minutes | the card's `long_video`: subagents, or ask to switch | as above | parallel subagents | shown before running |

A key being present is never consent: without `--profile` the CLI always uses agent mode, and
`--profile cloud` without `--allow-egress` prints the egress plan and refuses (exit 4). Ask the
user and wait for a clear yes before adding `--allow-egress`.

## Pipeline mode (`--profile local|cloud`)

The CLI calls the models itself, so frames never enter your context, and `ingest` returns the
finished document (same fields as `finish`). To see what would be sent first, run
`fi estimate - --profile <local|cloud> --json` and show the user its `egress` plan.

- `local` stays on this machine. `ingest` checks the local server and models first and stops
  with the exact fix (`ollama serve`, a model to pull); `fi doctor --profile local --json` shows
  the same checks.
- `cloud` sends audio, frames and text to the destinations the plan lists.
- For a spending cap or a hard network block use `fi run - --profile <p> --max-cost USD` or
  `--offline`, then `fi validate <document> --json` and `fi scan <job_id> --json`.

## Long videos

Above `long_video_minutes` (default 20) the agent-mode task card adds `long_video`: the vision
batches in at most `long_video_max_groups` (default 8) groups, each with the files, the sheets to
read and a ready-made `prompt` for one subagent. The CLI never starts agents itself. Fill
`corrections.json` and `synthesis.json` yourself after every group is done.

## Cloud speech only

When `ingest` stops with `needs_decision` and the user picks cloud speech, only the audio is sent
(`--cloud-speech --allow-egress`); frames stay here and you still read them.

## Other commands

- `fi export <job_id> --to <folder> [--style obsidian]` copies a finished document into a folder
  the user listed under `export_roots` in their config; it refuses anything else.
- `fi fetch <url> --allow-playlist --max-items N` makes one job per video (at most 25); only when
  the user asked for a playlist.
- `--metrics` on `run` or `finish` adds pacing and hook metrics.
