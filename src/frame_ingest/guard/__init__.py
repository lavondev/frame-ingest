"""Security controls (docs/PLAN.md section 4). Everything that touches the filesystem boundary, a
child process or untrusted text goes through this package:

* `paths`: the job-directory jail, symlink refusal, no-follow reads.
* `media`: container allowlist by magic bytes; rejects playlist-like and unknown inputs.
* `ffmpeg_args`: the only way an ffmpeg argv is built (option allowlist, `-protocol_whitelist`).
* `subproc`: the only place a process is started (argv list, scrubbed env, limits, output caps).
* `limits`: size, duration, pixel and disk caps.
* `text`: sanitiser for untrusted text that ends up in a document.
* `scan`: prompt-injection heuristics over video-derived text.
"""
