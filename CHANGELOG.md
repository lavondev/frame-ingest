# Changelog

All notable changes are recorded here. The format follows Keep a Changelog; versions follow
semantic versioning (pre-1.0: minor versions may change the CLI).

## [0.1.0] - unreleased

First tagged version. Everything below is new.

### Added
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
- No LICENSE file yet (the owner has not chosen one).
