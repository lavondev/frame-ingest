# Security policy

frame-ingest processes untrusted media, URLs and video-derived text, so security reports are
welcome and taken seriously.

## Reporting a vulnerability

Please report privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. Do not open a public issue or pull request for a security problem.

## Status

The project is pre-alpha and there are no supported releases yet. The threat model and the
planned controls (prompt injection through video content, SSRF and hostile URLs, hostile media and
ffmpeg, yt-dlp advisories, argument injection, path handling, secrets, egress, supply chain) are
documented in [`docs/PLAN.md`](docs/PLAN.md), section 4.

Implemented so far (local files only; there is no URL ingest yet):

- **One process wrapper.** Every child process starts in `guard/subproc.py`: argv list, no shell,
  scrubbed environment (no API keys reach a child), timeout with process-group kill, output caps,
  and resource limits on POSIX.
- **ffmpeg allowlist.** Commands are rebuilt from an option allowlist. Inputs and outputs must be
  regular, non-symlink files inside the job directory; every input gets
  `-protocol_whitelist file,pipe`. Playlist-like inputs (HLS, concat, SDP, DASH) and anything that
  is not a real media container are refused by magic bytes, and the sniffed demuxer is forced.
- **Caps.** File size, duration, pixel count and free disk.
- **Untrusted text.** Video-derived and model-written text is stripped of control, zero-width,
  bidi and tag characters and has Markdown/HTML syntax escaped. Every document carries
  `trust: untrusted-content`, a banner, and `injection_flags` counts from a heuristic scan.
  The scan is defence in depth, not a guarantee: a reader must still treat the document as data.
- **Secrets** come from the environment only, are never written to outputs or logs (tested), and
  `.env` files are never read.
- **Agent mode.** The agent's JSON outputs are schema-checked and guard-checked (exact frame and
  segment id sets, verbatim quotes, contiguous chapters) before a document exists. Caption files
  must be `.srt`/`.vtt`, regular, non-symlink and size-capped, and are parsed strictly. `validate`
  and `scan` never echo file content. The skill pre-approves only its own launcher and `Read`.
- **No network use** except the OpenAI-compatible provider client, and nothing runs against it
  yet (`--profile cloud` is not implemented).

Not yet implemented: URL fetching and yt-dlp hardening (M3), the egress consent gate and
`--offline` (M5), OS-level sandboxing (M8).
