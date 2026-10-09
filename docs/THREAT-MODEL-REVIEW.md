# Threat model: brief for an external reviewer

An external review of the threat model has to come
from someone outside this project; this page is what to hand them. Nothing here claims a review
has happened.

## What is being protected

A user (or their coding agent) points `frame-ingest` at a video file or URL they do not control.
The tool downloads, decodes and analyses it, and writes a document the agent will read.

| Asset | Adversary and vector |
|---|---|
| The agent's behaviour | Text in the video (speech, captions, on-screen text, filenames) written to manipulate an AI reader |
| Host files and network position | A hostile URL (SSRF, redirects, DNS rebinding), hostile media (parser bugs, playlist tricks), hostile yt-dlp output |
| Credentials | Leakage through argv, logs, outputs, error messages, `.env` auto-loading, child processes |
| Money and quota | Huge inputs, runaway loops, instructions to "keep going" |
| Supply chain | Compromised dependency, yt-dlp or ffmpeg drift, tampered release |

Trust boundaries: (1) the CLI's caller is untrusted (a prompt-injected agent may choose any
argument); (2) everything derived from the video is untrusted data; (3) the network is hostile;
(4) the host agent's own provider receives frames and transcript in agent mode (disclosed, not
prevented).

## Where each control lives

| Control | Code | Tests |
|---|---|---|
| One process wrapper (argv list, scrubbed env, limits, caps) | `guard/subproc.py` | `tests/test_security.py` |
| ffmpeg option allowlist, jailed paths, `-protocol_whitelist`, forced demuxer | `guard/ffmpeg_args.py`, `guard/media.py` | `tests/test_security.py`, `tests/test_fuzz.py` |
| OS sandbox for ffmpeg (no network, no home, writes only in the job dir) | `guard/sandbox.py` | `tests/test_sandbox.py` |
| Path jail, symlink refusal, no-follow reads | `guard/paths.py`, `storage.py` | `tests/test_security.py` |
| Caps: size, duration, pixels, disk | `guard/limits.py` | `tests/test_security.py` |
| URL policy, pinned-IP fetcher | `fetch/policy.py`, `fetch/http.py` | `tests/test_fetch.py`, `tests/test_fuzz.py` |
| yt-dlp: fixed argv, version floor, output verification | `guard/ytdlp_args.py`, `fetch/ytdlp.py` | `tests/test_fetch.py`, `tests/test_extras.py` |
| Egress-guard proxy (policy at connect time) | `fetch/proxy.py` | `tests/test_proxy.py` |
| Untrusted-text sanitiser, banner, `trust`, injection scan | `guard/text.py`, `guard/scan.py`, `pipeline/assemble.py` | `tests/test_security.py`, `tests/test_agent.py` |
| Egress plan and consent, `--offline`, `--max-cost` | `egress.py`, `guard/netblock.py`, `budget.py` | `tests/test_providers.py` |
| Secrets stay out of outputs and logs | `errors.py`, `config.py` | `tests/test_security.py` |
| Export confinement | `export.py` | `tests/test_extras.py` |
| Preview copy (fixed `./frame-ingest-out/`, export root rules, no foreign overwrite) and the code-built reply (neutralised text) | `agent/reply.py` | `tests/test_reply.py` |
| Flag review (no flag executes code, writes outside the jail or reveals a secret) | `cli.py` | `tests/test_security.py::test_flag_enumeration_*` |
| Release integrity (pinned actions, OIDC, attestations, checksums) | `.github/workflows/` | `tests/test_packaging.py` |

Run the whole suite with `uv run pytest`; the security-relevant subset is
`tests/test_security.py tests/test_fetch.py tests/test_proxy.py tests/test_sandbox.py
tests/test_fuzz.py tests/test_providers.py`.

## Known residual risks (stated, not hidden)

- **Prompt injection cannot be solved by a scanner.** `scan` and `injection_flags` are heuristics;
  the document is marked untrusted and the skill instructs the agent accordingly, but a model can
  still be persuaded. Structure is built in code and model output is schema-checked, so injection
  can degrade content but not forge structure.
- **`--allow-egress` can be passed by an injected agent.** The skill pre-approves its launcher with
  any arguments and forbids passing it unasked; that is an instruction, not a
  control. `--offline` and `egress: deny` are the hard stops.
- **Decoder bugs in ffmpeg.** Mitigated by the sandbox where one is available. `sandbox: auto`
  runs unsandboxed when none works; `sandbox: require` refuses. The Linux (`bwrap`) path is
  exercised only where CI provides bubblewrap with user namespaces.
- **Sandbox scope.** It covers ffmpeg. yt-dlp runs unsandboxed behind the egress-guard proxy with
  an empty working directory and verified output.
- **In-process network guard.** `--offline` patches Python sockets in this process; it does not
  stop a native extension that opens sockets itself.
- **Real providers and harnesses are untested here.** The provider clients, faster-whisper, the
  skill under Claude Code or Codex, and a live yt-dlp download have not been run by the author.

## Questions worth a reviewer's time

1. Can any argument, URL, filename or caption make the CLI read or write outside the job directory
   (or the configured export roots)?
2. Is there any sequence of redirects, DNS answers or IPv6 spellings that reaches a non-public
   address through `fetch/`?
3. Does the sandbox profile leave anything useful to an attacker who has code execution in ffmpeg?
4. Can the document structure be forged by transcript, caption, on-screen or filename text?
5. Does any code path put a key in argv, a log line, an error, an output file or a child's
   environment?
6. Is the release process sufficient to make a tampered artifact detectable?

Please report findings privately, following `SECURITY.md`.
