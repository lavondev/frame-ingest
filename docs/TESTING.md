# Try it

You need [uv](https://docs.astral.sh/uv/) and git. Install once:

```bash
git clone https://github.com/lavondev/frame-ingest ~/.frame-ingest-src
~/.frame-ingest-src/scripts/install.sh
```

That builds the tool (a minute or two, a few hundred MB) and links the skill into Claude Code
and Codex. Then start a **new** session and type:

- **Claude Code:** `/frame-ingest <video file or URL>`
- **Codex:** `$frame-ingest <video file or URL>`

A file can be dragged into the prompt (that pastes its path) or typed as a path. A URL can be a
direct video link or a page such as YouTube.

What you should see: it tells you where the video's frames and transcript will go, runs the tool,
looks at a few images, asks permission to write a few small files (say yes), and finishes with the
path of a Markdown document, a short summary and a chapter list.

Notes:
- The first run downloads a speech model (a few hundred MB). Videos with no captions are
  transcribed on your machine; nothing is uploaded for that.
- Long videos take a few minutes to transcribe. Let it finish.
- Codex's default sandbox blocks the network, so URLs only work there if you allow network access.
- If it doesn't work, send me exactly what you typed and what came back.

More thorough checks (the command line, the cloud and local profiles, safety tests):
[TESTING-DEEP.md](TESTING-DEEP.md).
