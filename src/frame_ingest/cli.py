"""Command-line entry point.

M1 commands: `doctor`, `probe`, `estimate`, `run`, all on local files and all offline unless
`doctor --online` is given. The rest of the planned surface (docs/PLAN.md, section 3.2) arrives
with its milestone. No command runs an external process other than through the engine's ffmpeg
wrapper until the guard layer (M2) exists, and none accepts a URL before M3.

Conventions (the agent-facing contract):

* `--json` prints exactly one JSON object on stdout; progress and logs go to stderr.
* In `--json` mode a failure is `{"ok": false, "error": {"code": ..., "message": ...}}`.
* No flag reads a secret, writes outside the job directory or executes anything. The data
  directory comes from FRAME_INGEST_HOME only, never from an argument.
* The input `-` reads one path from stdin, so a caller never has to quote it into a shell string.

Exit codes: 0 ok, 1 the work failed (job failed, doctor found a problem), 2 usage error,
3 input rejected, 4 unavailable (profile, config or provider), 5 job not found, 130 interrupted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from pydantic import ValidationError

from frame_ingest import __version__
from frame_ingest.agent.assemble_agent import ValidationFailed, assemble_job
from frame_ingest.agent.prepare import prepare as prepare_pack
from frame_ingest.agent.validate_doc import (
    read_document,
    scan_document,
    scan_job_files,
    validate_document,
)
from frame_ingest.budget import Budget, check_estimate
from frame_ingest.capabilities import CapabilityMemo
from frame_ingest.config import AppConfig, ConfigError, load_config
from frame_ingest.doctor import HealthCheck, check_local, report_dict, run_doctor
from frame_ingest.egress import EgressDenied, build_plan, enforce
from frame_ingest.engine import Engine, ProviderFactory
from frame_ingest.errors import (
    FatalProviderError,
    FrameIngestError,
    JobNotFound,
    MediaError,
    install_log_redaction,
    redact,
)
from frame_ingest.guard.netblock import OfflineViolation, block_network
from frame_ingest.guard.paths import PathRejected
from frame_ingest.models import EventType, Job, JobSettings, JobStatus
from frame_ingest.profiles import PROFILES, ProfileUnavailable, profile_config, resolve_profile

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INPUT = 3
EXIT_UNAVAILABLE = 4
EXIT_NOT_FOUND = 5
EXIT_INTERRUPTED = 130

PLANNED_COMMANDS = ("fetch", "clean")

_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_ECHO_LIMIT = 200


class InputRejected(FrameIngestError):
    code = "invalid_input"
    status = 400


@dataclass
class Result:
    payload: dict[str, Any]
    lines: list[str] = field(default_factory=list)
    exit_code: int = EXIT_OK
    error: str | None = None  # human-readable failure, printed to stderr


def _exit_code_for(exc: FrameIngestError) -> int:
    if isinstance(exc, InputRejected | MediaError | PathRejected):
        return EXIT_INPUT
    if isinstance(exc, JobNotFound):
        return EXIT_NOT_FOUND
    if isinstance(
        exc, ProfileUnavailable | ConfigError | FatalProviderError | EgressDenied | OfflineViolation
    ):
        return EXIT_UNAVAILABLE
    return EXIT_FAILED


def _echo(text: str) -> str:
    """Quote caller-supplied text for display: hostile input must not inject terminal escapes."""
    shown = text if len(text) <= _ECHO_LIMIT else text[:_ECHO_LIMIT] + "..."
    return repr(shown)


def _resolve_input(raw: str, *, what: str = "file") -> Path:
    """A caller-supplied input must be a regular local file (never a URL, never a symlink)."""
    if raw == "-":
        raw = sys.stdin.readline().strip()
        if not raw:
            raise InputRejected("Expected a path on stdin but got nothing.")
    if _URL_RE.match(raw):
        raise InputRejected(
            f"{_echo(raw)} looks like a URL. URL ingest is not available yet (planned for M3); "
            f"pass a local {what}."
        )
    try:
        path = Path(raw).expanduser()
        if path.is_symlink():
            raise InputRejected(f"Refusing to follow a symlink: {_echo(raw)}.")
        is_file = path.is_file()
    except (OSError, ValueError, RuntimeError):  # name too long, NUL byte, unknown ~user
        raise InputRejected(f"Not a usable file path: {_echo(raw)}.") from None
    if not is_file:
        raise InputRejected(f"Not a regular file: {_echo(raw)}.")
    return path


def _settings(args: argparse.Namespace) -> JobSettings | None:
    frame_cap = getattr(args, "frame_cap", None)
    language = getattr(args, "language", None)
    if frame_cap is None and language is None:
        return None
    return JobSettings(frame_cap=frame_cap, language=language)


def _no_providers() -> NoReturn:
    raise ProfileUnavailable("This command never calls a provider.")


def _engine(config: AppConfig, factory: ProviderFactory | None = None) -> Engine:
    return Engine(config, factory or _no_providers)


def _progress(event: EventType, data: dict[str, Any]) -> None:
    """Stage transitions and pipeline log lines, on stderr so stdout stays machine-readable."""
    if event == "stage" and data.get("status") in {"running", "done", "skipped", "failed"}:
        suffix = " (cached)" if data.get("cache_hit") else ""
        print(f"[{data.get('name')}] {data['status']}{suffix}", file=sys.stderr)
    elif event == "log" and data.get("level") in {"warning", "error"}:
        print(f"[{data.get('stage')}] {data.get('message')}", file=sys.stderr)


# ── commands ────────────────────────────────────────────────────────────────────
async def _cmd_doctor(args: argparse.Namespace, config: AppConfig) -> Result:
    memo = CapabilityMemo(config.data_root / "capabilities.json")
    online = args.online or args.deep  # --deep spends a fraction of a cent: it is its own consent
    providers = resolve_profile("cloud", config).factory() if args.deep else None
    report = await run_doctor(config, memo, online=online, deep=args.deep, providers=providers)
    if args.profile == "local":
        extra: list[HealthCheck] = await check_local(config)
        report.checks += extra
        report.ok = report.ok and all(c.ok for c in extra)
    lines = [f"{'ok  ' if c.ok else 'FAIL'} {c.name}: {c.message}" for c in report.checks]
    lines += [f"warn {w}" for w in report.warnings]
    if report.ffmpeg:
        lines.append(f"     {report.ffmpeg['version']}")
    payload = {"ok": report.ok, "online": online, "report": report_dict(report)}
    return Result(payload, lines, EXIT_OK if report.ok else EXIT_FAILED)


async def _cmd_probe(args: argparse.Namespace, config: AppConfig) -> Result:
    source = _resolve_input(args.input)
    # ffmpeg only ever sees a file inside a job directory (AGENTS.md rule 4), so probing makes a
    # private copy there and removes it afterwards.
    engine = _engine(config)
    job = await engine.create(source)
    try:
        video = job.video
        if video is None:  # pragma: no cover - create() always probes
            raise MediaError("The file could not be probed.")
        info = video.model_dump(mode="json")
    finally:
        engine.delete(job.id)
    lines = [
        f"{info['filename']}: {info['duration_s']:.1f}s, "
        f"{info.get('width') or '?'}x{info.get('height') or '?'}, "
        f"video={info.get('video_codec') or 'none'}, "
        f"audio={info.get('audio_codec') if info['has_audio'] else 'none'}"
    ]
    return Result({"ok": True, "video": info}, lines)


async def _job_for(
    args: argparse.Namespace,
    engine: Engine,
    settings: JobSettings | None,
    profile: str | None = None,
) -> Job:
    """The job to act on: an existing one (`--job`) or a new one made from `input`."""
    if args.job:
        if args.input is not None:
            raise _usage("Give either an input or --job, not both.")
        if settings is not None:
            raise _usage("--frame-cap and --language apply only when creating a job.")
        job = engine.load(args.job)
        if profile and job.profile not in (None, profile):
            raise _usage(f"Job {job.id} was created with profile '{job.profile}', not '{profile}'.")
        return job
    if args.input is None:
        raise _usage("Give an input file or --job <id>.")
    return await engine.create(_resolve_input(args.input), settings, profile=profile)


class _Usage(Exception):
    pass


def _usage(message: str) -> _Usage:
    return _Usage(message)


def _plan_payload(plan: Any) -> dict[str, Any]:
    data: dict[str, Any] = plan.model_dump(mode="json")
    data["network"] = plan.leaves_machine
    data["note"] = plan.render()
    return data


async def _cmd_estimate(args: argparse.Namespace, config: AppConfig) -> Result:
    cfg = profile_config(args.profile, config)  # no keys or providers: nothing is contacted
    engine = _engine(cfg)
    job = await _job_for(args, engine, _settings(args), args.profile)
    est = await engine.estimate(job.id)
    if args.profile == "local":
        est = est.model_copy(update={"cost_usd": 0.0, "cost_note": "Local models cost nothing."})
    plan = build_plan(args.profile, cfg, est, job.settings)
    payload = {
        "ok": True,
        "job_id": job.id,
        "profile": args.profile,
        "estimate": est.model_dump(mode="json"),
        "egress": _plan_payload(plan),
    }
    cost = f"~${est.cost_usd:.2f}" if est.cost_usd is not None else "unknown"
    lines = [
        f"job {job.id}: {est.duration_s:.1f}s of video, {est.frames} frames",
        f"{est.total_api_calls} API calls, {est.total_input_tokens} input / "
        f"{est.total_output_tokens} output tokens, cost {cost}",
        plan.render(),
    ]
    return Result(payload, lines)


def _ask(prompt: str) -> bool:
    print(prompt, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip().lower() in {"y", "yes"}


async def _cmd_run(args: argparse.Namespace, config: AppConfig) -> Result:
    resolved = resolve_profile(args.profile, config, offline=args.offline)  # before any copying
    engine = _engine(resolved.config, resolved.factory)
    job = await _job_for(args, engine, _settings(args), args.profile)

    # What would leave the machine, shown before anything is spent or sent (PLAN T8, T10).
    est = await engine.estimate(job.id)
    if resolved.free:
        est = est.model_copy(update={"cost_usd": 0.0, "cost_note": "No per-call cost."})
    plan = build_plan(args.profile, resolved.config, est, job.settings)
    if plan.items:
        print(plan.render(), file=sys.stderr)
    enforce(
        plan,
        mode=config.egress,
        allow_flag=args.allow_egress,
        offline=args.offline,
        interactive=sys.stdin.isatty() and sys.stderr.isatty(),
        ask=_ask,
    )
    budget: Budget | None = None
    if args.max_cost is not None:
        check_estimate(est, args.max_cost, free=resolved.free)
        budget = Budget(args.max_cost, engine.pricing, free=resolved.free)

    with block_network() if args.offline else nullcontext():
        job = await engine.run(job.id, on_event=_progress, budget=budget)
    job_dir = str(engine.store.dir(job.id))
    payload: dict[str, Any] = {
        "ok": job.status == JobStatus.COMPLETED,
        "job_id": job.id,
        "status": job.status.value,
        "profile": args.profile,
        "job_dir": job_dir,
        "chapter_count": job.chapter_count,
        "egress": _plan_payload(plan),
        "spent_usd": round(budget.spent, 4) if budget else None,
        "warnings": [w.model_dump(mode="json") for w in job.warnings],
    }
    if job.status != JobStatus.COMPLETED:
        err = job.error
        payload["error"] = (
            err.model_dump(mode="json") if err else {"code": "pipeline_failed", "message": ""}
        )
        message = f"job {job.id} {job.status.value}: {err.message if err else 'unknown error'}"
        return Result(payload, [], EXIT_FAILED, error=message)
    payload["outputs"] = {key: str(engine.output_path(job, key)) for key in job.outputs}
    lines = [f"job {job.id}: {job.chapter_count} chapters, {len(job.warnings)} warnings"]
    lines += [f"{key}: {path}" for key, path in payload["outputs"].items()]
    return Result(payload, lines)


async def _cmd_prepare(args: argparse.Namespace, config: AppConfig) -> Result:
    engine = _engine(config)
    job = await _job_for(args, engine, _settings(args))
    captions = _resolve_input(args.captions, what="caption file") if args.captions else None
    manifest = await prepare_pack(
        engine, job.id, captions=captions, start=args.start, end=args.end, dense=args.dense
    )
    transcript = manifest["transcript"]
    out_dir = manifest["directories"]["agent"] + "/manifest.json"
    payload = {
        "ok": True,
        "job_id": job.id,
        "manifest": out_dir,
        "frames": len(manifest["frames"]),
        "sheets": len(manifest["sheets"]),
        "transcript_source": transcript["source"],
        "transcript_segments": transcript["segments"],
        "drill_frames_added": manifest["drill_frames_added"],
        "output_directory": manifest["directories"]["out"],
    }
    lines = [
        f"job {job.id}: {payload['frames']} frames on {payload['sheets']} contact sheet(s), "
        f"{payload['transcript_segments']} transcript segment(s) ({payload['transcript_source']})",
        f"manifest: {out_dir}",
        f"write your outputs under: {payload['output_directory']}",
    ]
    return Result(payload, lines)


async def _cmd_assemble(args: argparse.Namespace, config: AppConfig) -> Result:
    engine = _engine(config)
    try:
        job = await assemble_job(engine, args.job)
    except ValidationFailed as exc:
        payload: dict[str, Any] = {
            "ok": False,
            "job_id": args.job,
            "error": {"code": exc.code, "message": exc.message},
            "problems": exc.problems,
        }
        lines = [f"{p['file']}: {p['message']}" for p in exc.problems]
        return Result(payload, lines, EXIT_FAILED, error=exc.message)
    outputs = {key: str(engine.output_path(job, key)) for key in job.outputs}
    payload = {
        "ok": True,
        "job_id": job.id,
        "status": job.status.value,
        "chapter_count": job.chapter_count,
        "warnings": [w.model_dump(mode="json") for w in job.warnings],
        "outputs": outputs,
    }
    lines = [f"job {job.id}: {job.chapter_count} chapters, {len(job.warnings)} warnings"]
    lines += [f"{key}: {path}" for key, path in outputs.items()]
    return Result(payload, lines)


async def _cmd_validate(args: argparse.Namespace, config: AppConfig) -> Result:
    text = read_document(_resolve_input(args.document, what="document"))
    issues = validate_document(text)
    payload = {"ok": not issues, "issues": issues}
    lines = [
        f"{'line ' + str(i['line']) + ': ' if i['line'] else ''}{i['code']}: {i['message']}"
        for i in issues
    ] or ["document is valid"]
    return Result(payload, lines, EXIT_FAILED if issues else EXIT_OK)


async def _cmd_scan(args: argparse.Namespace, config: AppConfig) -> Result:
    engine = _engine(config)
    if engine.store.exists(args.target):
        report = scan_job_files(engine.store.dir(args.target))
        where = f"job {args.target}"
    else:
        report = scan_document(read_document(_resolve_input(args.target, what="document")))
        where = "document"
    payload = {
        "ok": True,
        "flags": report["flags"],
        **{k: v for k, v in report.items() if k != "flags"},
    }
    flags = report["flags"]
    lines = [f"{where}: " + (", ".join(f"{k} x{v}" for k, v in flags.items()) or "no flags")]
    return Result(payload, lines)


_HANDLERS = {
    "prepare": _cmd_prepare,
    "assemble": _cmd_assemble,
    "validate": _cmd_validate,
    "scan": _cmd_scan,
    "doctor": _cmd_doctor,
    "probe": _cmd_probe,
    "estimate": _cmd_estimate,
    "run": _cmd_run,
}


# ── parser and entry point ──────────────────────────────────────────────────────
def _add_common(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--json", action="store_true", help="print one JSON object on stdout")


def _add_input(sub: argparse.ArgumentParser, *, profile: bool) -> None:
    sub.add_argument(
        "input", nargs="?", help="local video file, or '-' to read the path from stdin"
    )
    sub.add_argument("--job", metavar="ID", help="use an existing job (resumes it for `run`)")
    sub.add_argument("--frame-cap", type=int, help="maximum number of frames to analyse")
    sub.add_argument("--language", help="spoken language hint, e.g. 'en'")
    if profile:
        sub.add_argument(
            "--profile",
            required=True,
            choices=PROFILES,
            help="provider profile (fake, cloud, local)",
        )


def _add_prepare(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--captions", metavar="FILE", help="a .srt or .vtt caption file to use as the transcript"
    )
    sub.add_argument(
        "--dense", action="store_true", help="add full-resolution frames for a time range"
    )
    sub.add_argument("--start", type=float, help="range start in seconds (with --dense)")
    sub.add_argument("--end", type=float, help="range end in seconds (with --dense)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="frame-ingest",
        description="Turn a video (file or URL) into a structured, citable Markdown document.",
        epilog="Planned, not implemented yet: " + ", ".join(PLANNED_COMMANDS),
    )
    parser.add_argument("--version", action="version", version=f"frame-ingest {__version__}")
    subs = parser.add_subparsers(dest="command", metavar="<command>")

    doctor = subs.add_parser("doctor", help="check ffmpeg, config and (optionally) the API")
    _add_common(doctor)
    doctor.add_argument(
        "--online",
        action="store_true",
        help="also contact the configured API to verify the key and models (sends the key there)",
    )
    doctor.add_argument(
        "--deep",
        action="store_true",
        help="implies --online; also runs two tiny paid probes to learn what the models support",
    )
    doctor.add_argument(
        "--profile", choices=("local",), help="also check readiness of the local profile"
    )

    probe = subs.add_parser("probe", help="duration, resolution, fps and audio of a local file")
    _add_common(probe)
    probe.add_argument("input", help="local video file, or '-' to read the path from stdin")

    estimate = subs.add_parser("estimate", help="frames, calls and tokens; no network egress")
    _add_common(estimate)
    _add_input(estimate, profile=True)

    run = subs.add_parser("run", help="run the full pipeline on a local file")
    _add_common(run)
    _add_input(run, profile=True)
    run.add_argument(
        "--allow-egress",
        action="store_true",
        help="consent to send data to the cloud destinations shown (only after the user agrees)",
    )
    run.add_argument(
        "--offline", action="store_true", help="forbid all non-loopback network use for this run"
    )
    run.add_argument(
        "--max-cost", type=float, metavar="USD", help="refuse or stop above this dollar amount"
    )

    prepare = subs.add_parser("prepare", help="agent mode: build the evidence pack to read")
    _add_common(prepare)
    _add_input(prepare, profile=False)
    _add_prepare(prepare)

    assemble = subs.add_parser("assemble", help="agent mode: validate outputs, write the document")
    _add_common(assemble)
    assemble.add_argument("job", help="job id from `prepare`")

    validate = subs.add_parser("validate", help="check a finished document against the format")
    _add_common(validate)
    validate.add_argument("document", help="document path, or '-' to read the path from stdin")

    scan = subs.add_parser("scan", help="flag prompt-injection patterns in a job or document")
    _add_common(scan)
    scan.add_argument("target", help="job id, or a document path ('-' reads it from stdin)")
    return parser


def _emit(args: argparse.Namespace, result: Result) -> None:
    if args.json:
        sys.stdout.write(json.dumps(result.payload, indent=2, ensure_ascii=False) + "\n")
    else:
        for line in result.lines:
            print(line)
        if result.error:
            print(f"error: {result.error}", file=sys.stderr)


def _fail(args: argparse.Namespace, code: str, message: str, exit_code: int) -> int:
    if getattr(args, "json", False):
        payload = {"ok": False, "error": {"code": code, "message": message}}
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    else:
        print(f"error: {message}", file=sys.stderr)
    return exit_code


def _secrets(config: AppConfig | None) -> list[str]:
    if config is None:
        return []
    keys = (config.key_for(role) for role in ("transcribe", "vision", "text"))
    return [k for k in keys if k]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.command is None:
        parser.print_help()
        return EXIT_OK

    config: AppConfig | None = None
    try:
        config = load_config()
        install_log_redaction(_secrets(config))
        result = asyncio.run(_HANDLERS[args.command](args, config))
    except _Usage as exc:
        return _fail(args, "usage", str(exc), EXIT_USAGE)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        return _fail(args, "usage", f"Invalid option {where}: {first['msg']}.", EXIT_USAGE)
    except FrameIngestError as exc:
        message = redact(exc.message, *_secrets(config))
        return _fail(args, exc.code, message, _exit_code_for(exc))
    except KeyboardInterrupt:
        return _fail(args, "interrupted", "Interrupted.", EXIT_INTERRUPTED)

    _emit(args, result)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
