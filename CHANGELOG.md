# Changelog

All notable changes are recorded here. The format follows Keep a Changelog; versions follow
semantic versioning (pre-1.0: minor versions may change the CLI).

## [Unreleased]

The hardened agent mode (`docs/HARDENED-ARCHITECTURE.md`).

### Added
- `ingest`, `next`, `check`, `finish`: one call for everything mechanical, a task card with the
  exact next command and `fi_path` (the launcher path to reuse), a millisecond check per agent
  file, and assemble + validate + scan in one call. `ingest --profile local|cloud` runs the
  pipeline instead (cloud only with a key and `--allow-egress`).
- Audio guarantee: a loudness check and a retry without voice-activity filtering when speech-to-
  text finds nothing in a track that is not silent; otherwise `needs_decision` (exit 6) with the
  options `--captions`, `--allow-frames-only`, `--cloud-speech --allow-egress`.
- A `coverage` block (audio yes/no, transcript source, frames N/M, chapters, quotes verified) in
  the manifest, the document and the sidecar, and a "Frames only" banner when there is no audio.
- Prefilled templates for every agent file, with `TODO:` placeholders the checks never accept.
- Long-video guidance: vision batches grouped for parallel subagents in the task card.
- `evals/`: three generated cases with expected behaviour, an offline CLI-side test and a scorer
  for runs by hand in Claude Code and Codex.

### Changed
- Job ids come from the video's content, so every command sees the same job for the same input.
- SKILL.md rewritten around the four commands (120 lines); details moved to `references/`.
- Tests no longer download a real speech model.

### Fixed
- The skill told agents to type `${CLAUDE_SKILL_DIR}/scripts/fi`, which ran `/scripts/fi` (exit
  127) where the variable was empty.
- An empty transcript no longer lets a run continue quietly from frames alone.
- `estimate` and `prepare` printed different job ids for the same video.
- `run --metrics` followed by `run` on the same job reused the metrics section from the cache.
- The ffmpeg argument builder accepted an option name as a `-vf` value (found by fuzzing).

## [0.1.0] - unreleased

First tagged version. Everything below is new.

### Added
- MIT license.
- One-step install (`scripts/install.sh`) and automatic local transcription in agent mode.
- Pipeline ported from faircopy (probe, audio, transcribe, frames, vision, correct, synthesize,
  assemble) with a content-chained stage cache and resumable jobs.
- CLI: `doctor`, `probe`, `estimate`, `run`, `fetch`, `prepare`, `assemble`, `validate`, `scan`,
  all with `--json` and documented exit codes.
- Security core: one process wrapper, ffmpeg argument allowlist, container sniffing, path jail,
  size/duration/pixel caps, untrusted-text sanitiser, injection scan, security test suite.
- URL ingest: URL policy, pinned-IP fetcher, hardened yt-dlp (version floor 2026.7.4).
- Agent mode and the Agent Skill: evidence pack with contact sheets, validating `assemble`.
- Pipeline mode: `cloud` and `local` profiles, egress plan and consent, `--offline`, `--max-cost`.
- Pacing and hook metrics, safe export (Markdown or Obsidian), capped playlists, speaker labels.
- Hardening: OS sandbox for ffmpeg (sandbox-exec, bubblewrap), egress-guard proxy for yt-dlp,
  hypothesis fuzzing, a disclosure process and a threat-model brief for reviewers.
- Packaging: Claude Code plugin and marketplace manifests, portable plugin manifest, Codex skill
  metadata, deterministic `.skill` zip, pinned launcher, release workflow (OIDC, attestations).

### Known gaps
- No real provider or harness has been exercised end to end; see docs/PLAN.md.
