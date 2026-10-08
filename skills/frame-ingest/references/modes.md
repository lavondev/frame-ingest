# Modes: who does the looking

| Situation | Mode | Speech | Who reads the frames | Cost |
|---|---|---|---|---|
| Default, no key | agent mode: `fi ingest` | on this machine (faster-whisper) | you, the host agent | the user's plan usage |
| A local model server (Ollama) and the user asks for it | `fi run - --profile local` | on this machine | a local vision model | free, slower |
| An API key is set **and** the user says yes | `fi run - --profile cloud --allow-egress` | cloud speech API | a cloud vision model | API cents per video |

Never switch to `cloud` on your own: a key being present is not consent.

## Pipeline mode (`run`)

The CLI calls the models itself, so frames never enter your context. Always run
`fi estimate - --profile <p> --json` first and show the user its `egress` plan.

- `local` stays on this machine. `fi doctor --profile local --json` prints the exact fix for
  anything missing (the local server, a model to pull).
- `cloud` sends audio, frames and text to the destinations the plan lists. Ask the user and wait
  for a clear yes before passing `--allow-egress`; without it the run is refused (exit 4) after
  printing the plan. `--max-cost USD` stops before or while it overspends; `--offline` forbids
  every non-loopback connection.

The result is the same document; finish with `fi validate <document> --json` and
`fi scan <job_id> --json`.

## Cloud speech only

When `ingest` stops with `needs_decision` and the user picks cloud speech, only the audio is sent
(`--cloud-speech --allow-egress`); frames stay here and you still read them.

## Other commands

- `fi export <job_id> --to <folder> [--style obsidian]` copies a finished document into a folder
  the user listed under `export_roots` in their config; it refuses anything else.
- `fi fetch <url> --allow-playlist --max-items N` makes one job per video (at most 25); only when
  the user asked for a playlist.
- `--metrics` on `run` or `finish` adds pacing and hook metrics.
