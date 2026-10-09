# Security policy

frame-ingest processes untrusted media, URLs and video-derived text, so security reports are
welcome and taken seriously.

## Reporting a vulnerability

Please report privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. Do not open a public issue or pull request for a security problem.

### What to expect

| Step | Target |
|---|---|
| Acknowledgement of your report | within 3 working days |
| First assessment (accepted, needs more information, or declined, with reasons) | within 7 days |
| Fix or mitigation for a confirmed high-severity issue | within 30 days |
| Public advisory and release notes | after a fix ships, or after 90 days at the latest, whichever comes first (earlier if the issue is already being exploited) |

We will credit you in the advisory unless you ask us not to. Please give us a reasonable chance
to fix a problem before you disclose it. Good-faith research that follows this policy will not
be met with legal action.

### Scope

In scope: anything that lets hostile input (a video, a URL, captions, filenames, arguments) read
or write outside the job directory, reach a non-public network address, run code, leak a secret,
send data off the machine without consent, or forge the structure of a document. Also the
release pipeline (workflows, published artifacts) and the skill's launcher.

Out of scope: that a model can be persuaded by text in a video (documented residual risk, see
`docs/THREAT-MODEL-REVIEW.md`); vulnerabilities in yt-dlp, ffmpeg or other dependencies
themselves (report them upstream; tell us if our version floor should move); issues that need
an attacker who already controls your account or machine.

### Supported versions

Only the latest released minor version receives security fixes while the project is pre-1.0.

## Status

The project is pre-1.0 and has not had an external security review. The threat model and the
controls (prompt injection through video content, SSRF and hostile URLs, hostile media and
ffmpeg, yt-dlp advisories, argument injection, path handling, secrets, egress, supply chain) are
mapped to the code and tests that enforce them in
[`docs/THREAT-MODEL-REVIEW.md`](docs/THREAT-MODEL-REVIEW.md), which also lists the residual
risks.

Implemented:

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
- **Egress consent.** Before a cloud run the CLI computes, from the estimate, what would be sent
  where (audio minutes, frames, text, destination hosts, models, cost) and prints it. It then
  requires an interactive yes, `--allow-egress` or `egress: allow`; with no terminal and none of
  those it refuses. Only loopback counts as staying on the machine. `--offline` overrides every
  consent and blocks non-loopback sockets and DNS for the whole run. `--max-cost` refuses an
  over-budget or unpriceable run up front and stops a running one at the cap. Because the skill
  pre-approves its launcher with any arguments, SKILL.md tells the agent never to pass
  `--allow-egress` without the user's yes; that instruction is a control, not a guarantee.
- **URL policy and fetching.** Only http/https, no credentials in the URL, an allowlisted port,
  and every address the host resolves to must be public (numeric forms such as `0x7f000001` are
  normalised first). Direct media links use our own client: every redirect hop is validated again,
  the connection goes to the address that was validated, and there is a size cap and a deadline.
  ffmpeg never receives a URL.
- **yt-dlp.** Fixed flags only: no config files, plugins, cookies, `--exec` or link files, and an
  empty private directory per run. A version floor is enforced in code. It runs behind a
  loopback proxy that applies the URL policy again to every connection it makes.
- **OS sandbox for ffmpeg.** `sandbox-exec` on macOS and `bwrap` on Linux: no network, no home
  directory, writes only in the job directory. The default (`sandbox: auto`) runs unsandboxed
  when none works and says so in `doctor`; `require` refuses instead.
- **Writes outside the job directory.** `export` writes only inside roots you list in config,
  never one given as an argument. The only default write outside the home folder is the preview
  copy in `./frame-ingest-out/`, which follows the same rules (no symlinks, nothing overwritten,
  `preview_copy: false` turns it off).
- **Network use** is confined to the provider client, `doctor` and URL fetching; a test fails if
  any other module imports a network client.

Not yet exercised: the real provider clients, the skill under Claude Code or Codex, and a live
yt-dlp download. The residual risks are listed in
[`docs/THREAT-MODEL-REVIEW.md`](docs/THREAT-MODEL-REVIEW.md).
