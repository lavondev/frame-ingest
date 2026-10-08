# Output format

The document is deterministic Markdown: code decides every heading, anchor, timestamp and the
entity index. Model output only fills text fields, after schema and guard checks.

```
---
title, source_file, duration, duration_seconds, resolution, analyzed_at, models, has_audio,
chapter_count, coverage: {audio: 'yes'|'no', audio_track, transcript_source, frames_analyzed: N/M,
chapters, quotes_verified: K/N, note}, tags, trust: untrusted-content, mode (agent|pipeline),
transcript_source (captions|auto-captions|asr|none), timestamp_precision,
injection_flags: {kind: count}
---
# Title
> **Untrusted content.** ... (fixed banner)
> **Frames only.** No transcript: <why>. ...   (only when coverage.audio is 'no')
> **Coverage:** audio yes · transcript asr · frames 8/8 analysed · 4 chapter(s) · quotes verified 4/4
## TL;DR {#tldr}
## Abstract {#abstract}
## Table of Contents {#toc}
## Chapter N: Title [HH:MM:SS - HH:MM:SS] {#ch-NN}
  ### Summary / Key Points / Visual Description / On-Screen Text / Notable Quotes /
  ### Corrected Transcript   (each heading restates the chapter title and range)
## Glossary {#glossary}
## Entity Index {#entity-index}
## Open Questions {#open-questions}
## Appendix: Processing Notes {#appendix}
```

Anchors: chapters `ch-NN`, chapter sections `ch-NN-<section>`, transcript lines `t-HHMMSS`
(`-2`, `-3` on collisions). Cite as `[00:01:25](#t-000125)` or `[Chapter 2](#ch-02)`.
Timestamps are always `HH:MM:SS` and never exceed the video duration.

`finish` also copies the document to `frame-ingest-out/<video name>.md` in the current folder (a
copy the harness can preview; the original stays in the job folder) and returns
`reply_markdown`, the compact summary to send back unchanged.

`coverage` says what the document actually covers, computed in code: `audio: 'yes'` means it
includes what was said (from captions or speech-to-text). A document with `audio: 'no'` always
carries the "Frames only" banner, and `validate` rejects one that does not.

All text taken from the video or written by a model has control, zero-width and bidirectional
characters removed and Markdown/HTML syntax escaped, so it cannot forge headings, links or
anchors. A JSON sidecar with the same content and `trust: untrusted-content` is written next to
the document.
