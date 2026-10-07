"""Shared test helpers (plain functions and constants; fixtures live in conftest.py)."""

from __future__ import annotations

PUBLIC_IP = "93.184.216.34"


async def public(host: str, port: int) -> list[str]:
    """A DNS resolver that answers every name with one public address."""
    return [PUBLIC_IP]


FAKE = """\
import json, os, shutil, sys
from pathlib import Path
here = Path(__file__).parent
spec = json.loads((here / "fake.json").read_text())
args = sys.argv[1:]
if "--version" in args:
    print(spec["version"]); sys.exit(0)
if "--dump-single-json" in args:
    Path(here / "argv.json").write_text(json.dumps(args))
    if spec.get("list_exit", 0):
        sys.stderr.write("ERROR: cannot list\\n"); sys.exit(spec["list_exit"])
    sys.stdout.write(spec["listing"] if isinstance(spec["listing"], str) else json.dumps(spec["listing"]))
    sys.exit(0)
if spec.get("exit", 0) not in (0, 101):
    sys.stderr.write("ERROR: fake failure KEYSET=%s\\n" % ("1" if "OPENAI_API_KEY" in os.environ else "0"))
    sys.exit(spec["exit"])
out = Path(args[args.index("-o") + 1]).parent
auto = "--write-auto-subs" in args
Path(here / "argv.json").write_text(json.dumps(args))
for name, how in spec["files"]:
    if auto != name.endswith(".auto.vtt"):
        if not (auto and name.endswith(".auto.vtt")) and not (not auto and not name.endswith(".auto.vtt")):
            continue
    target = out / name.replace(".auto.vtt", ".vtt")
    if how.startswith("copy:"):
        shutil.copy(how[5:], target)
    elif how.startswith("link:"):
        target.symlink_to(how[5:])
    elif how == "dir":
        target.mkdir()
    else:
        target.write_text(how[5:])
sys.exit(spec.get("exit", 0))
"""
VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nHello from the captions.\n"
