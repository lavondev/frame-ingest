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
