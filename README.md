<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/banner-dark.svg">
  <img alt="frame-ingest: video in, citable Markdown out" src="docs/assets/banner-light.svg" width="100%">
</picture>

<br>

**Turn any video, a local file or a URL, into a structured document you can cite.**

![status](https://img.shields.io/badge/status-pre--alpha-E8731A?style=flat-square)
![license](https://img.shields.io/badge/license-MIT-0E9F8E?style=flat-square)

</div>

<br>

frame-ingest is a **Claude Code skill** that watches a video for you. Give it a file or a link (YouTube, Vimeo or a direct `.mp4`) and it writes a Markdown document: a timestamped transcript with speech-to-text errors corrected, chapters with summaries, verbatim quotes, a glossary and an index of the entities mentioned. Use it to take notes on a lecture, talk, tutorial, demo or meeting recording, or to quote and cite one.

It also works in Codex, Gemini CLI, Cursor and other agents that read Agent Skills, and it ships a command-line tool. No API key is needed: speech is transcribed on your machine and your agent reads the frames.

## Install

frame-ingest needs [uv](https://docs.astral.sh/uv/), a small tool that sets up Python and the CLI for you. Skip this step if you already have it (`uv --version`). Otherwise it takes about ten seconds:

```bash
brew install uv                                    # macOS
curl -LsSf https://astral.sh/uv/install.sh | sh    # Linux and macOS
```

Then install the skill:

```bash
npx skills add lavondev/frame-ingest
```

Then ask your agent to run it:

| Agent | Command |
|---|---|
| Claude Code | `/frame-ingest <video file or URL>` |
| Codex | `$frame-ingest <video file or URL>` |
| Gemini CLI, Cursor, Copilot, OpenCode, Cline | say *"frame-ingest this video"* |

<details>
<summary><b>Claude Code plugin</b></summary>

<br>

```text
/plugin marketplace add lavondev/frame-ingest
/plugin install frame-ingest@frame-ingest
```

</details>

<details>
<summary><b>Clone the repo</b> (Claude Code and Codex)</summary>

<br>

```bash
git clone https://github.com/lavondev/frame-ingest ~/.frame-ingest-src
~/.frame-ingest-src/scripts/install.sh
```

The repo ships the skill as `skills/frame-ingest/` and `.agents/skills/frame-ingest`. Codex and other `.agents/skills` readers can also copy `skills/frame-ingest` into `~/.agents/skills/`.

</details>

<details>
<summary><b>Command line only</b></summary>

<br>

```bash
uv tool install "frame-ingest[url,local] @ git+https://github.com/lavondev/frame-ingest"
frame-ingest doctor
```

</details>

<details>
<summary><b>Manual download</b></summary>

<br>

Download `frame-ingest-<version>.skill` from a release (a zip; verify it against `SHA256SUMS`) and unzip it into your agent's skills directory.

</details>

> [!NOTE]
> **Pre-alpha.** Feature complete and tested offline, not yet field-tested against real providers and harnesses.

## What you get

- A **corrected transcript**, with speech-to-text errors fixed using what was on screen
- **Chapters** with summaries, plus a **glossary** and an **entity index**
- **Verbatim quotes**, each checked against the transcript
- A description of **what is on screen**: slides, screen recordings and on-screen text
- **Stable anchors** (`ch-01`, `t-000125`) and timestamps, with a JSON sidecar, so every claim is citable

When it finishes, the agent gives you the document path, a TL;DR, the chapters and a coverage line:

```text
audio yes · transcript asr · frames 8/8 analysed
```

## Example output

A shortened excerpt of a generated document. It comes from the project's test fixture, so the content is made up.

```markdown
---
title: Widget Frobnicator Walkthrough
duration: 00:01:30
trust: untrusted-content
coverage:
  audio: 'yes'
  transcript_source: asr
  frames_analyzed: 2/2
  chapters: 2
  quotes_verified: 1/2
---

## Chapter 1: Setup [00:00:00 - 00:00:45] {#ch-01}

### Summary — Chapter 1: Setup [00:00:00 - 00:00:45] {#ch-01-summary}

How to install the product.

### Notable Quotes — Chapter 1: Setup [00:00:00 - 00:00:45] {#ch-01-quotes}

> "Open the dashboard first." — [00:00:06](#t-000006)

### Corrected Transcript — Chapter 1: Setup [00:00:00 - 00:00:45] {#ch-01-transcript}

<a id="t-000001"></a>**[00:00:01]** Welcome to the Widget Frobnicator.
<a id="t-000006"></a>**[00:00:06]** Open the dashboard first.
```

## Questions

### Can Claude watch a video?

Your agent reads text and images, so frame-ingest turns the video into both. It transcribes the speech on your machine, picks frames at scene changes and slide changes, and gives the agent the transcript and the frames to read, each stamped with its time.

### How do I summarize a YouTube video with Claude Code?

Install the skill (above), then run `/frame-ingest <YouTube link>` in Claude Code. It downloads the video, transcribes it and gives you the document path, a TL;DR and the chapters. The full document is Markdown, and a copy is saved to `./frame-ingest-out/` in your current folder so you can preview it.

### Do I need an API key?

No. Speech is transcribed on your machine and your agent reads the frames, so it runs on the model you already use. The command-line tool can also call an OpenAI-compatible API (`--profile cloud`), but it shows what would be sent and asks first.

### Does my video leave my machine?

The speech is transcribed locally. The transcript and frames go to whichever model runs your agent, the same as any other file you give it. Nothing goes to another cloud provider without your consent, and `--offline` blocks network access for a run.

### What if the video contains text that tries to instruct the AI?

The document is marked `trust: untrusted-content` and starts with a warning banner. Hidden and control characters are stripped, Markdown and HTML syntax in the video's text is neutralised, and a scan flags instruction-like phrases. The skill tells your agent to treat the document as data. That lowers the risk but cannot remove it; see [SECURITY.md](SECURITY.md).

### Which videos work?

Local video files, links to YouTube and other sites that yt-dlp supports, and direct links to media files. ffmpeg comes bundled.

## How the data flows

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/flow-dark.svg">
  <img alt="Data flow: video, ingest on your machine, your agent's model writes, finish checks and builds the document" src="docs/assets/flow-light.svg" width="100%">
</picture>

Videos without captions are transcribed on your own machine, and nothing is uploaded for that. Frames and transcript go to whichever model runs your agent, and nothing goes to a cloud provider without your consent. If a video has audio but no transcript can be made, it stops and asks you instead of quietly dropping the sound.

## Learn more

- [Testing](docs/TESTING.md) · [Deep testing](docs/TESTING-DEEP.md) · [Threat model review](docs/THREAT-MODEL-REVIEW.md)
- [Security](SECURITY.md) · [Agent instructions](AGENTS.md)

<details>
<summary><b>Development</b></summary>

<br>

```bash
uv sync
uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Regenerate the artwork (logo, banner, flow diagram, both themes): `python3 scripts/gen_assets.py docs/assets`

</details>

## License

MIT, see [`LICENSE`](LICENSE).
