"""Job persistence: plain JSON files, atomic writes, no database.

<home>/jobs/<job_id>/
    job.json  upload/<name>  audio/  frames/  stages/<stage>/...  out/
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from frame_ingest.errors import JobNotFound
from frame_ingest.models import Job

JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._ \-()+]")


def safe_filename(name: str) -> str:
    """Strip any path and unsafe characters from a client-supplied filename."""
    base = Path(name.replace("\\", "/")).name
    base = _SAFE_CHARS.sub("_", base).strip(" .") or "video"
    if len(base) > 120:
        stem, dot, ext = base.rpartition(".")
        base = (stem[: 115 - len(ext)] + dot + ext) if dot else base[:120]
    return base


def file_stem(name: str) -> str:
    stem = Path(name).stem or "video"
    return _SAFE_CHARS.sub("_", stem).strip(" .") or "video"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json(path: Path, obj: BaseModel | Any) -> None:
    if isinstance(obj, BaseModel):
        text = obj.model_dump_json(indent=2)
    else:
        text = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    atomic_write_text(path, text)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class JobStore:
    def __init__(self, jobs_dir: Path) -> None:
        self.root = jobs_dir
        self.root.mkdir(parents=True, exist_ok=True)

    # -- ids & paths ----------------------------------------------------------------------
    @staticmethod
    def new_id() -> str:
        return secrets.token_hex(6)

    def dir(self, job_id: str) -> Path:
        if not JOB_ID_RE.match(job_id):
            raise JobNotFound(job_id)
        return self.root / job_id

    def exists(self, job_id: str) -> bool:
        return JOB_ID_RE.match(job_id) is not None and (self.root / job_id / "job.json").is_file()

    def upload_dir(self, job_id: str) -> Path:
        return self.dir(job_id) / "upload"

    def out_dir(self, job_id: str) -> Path:
        return self.dir(job_id) / "out"

    def frames_dir(self, job_id: str) -> Path:
        return self.dir(job_id) / "frames"

    def video_path(self, job_id: str, filename: str) -> Path:
        return self.upload_dir(job_id) / safe_filename(filename)

    # -- job.json -------------------------------------------------------------------------
    def save_job(self, job: Job) -> None:
        write_json(self.dir(job.id) / "job.json", job)

    def load_job(self, job_id: str) -> Job:
        path = self.dir(job_id) / "job.json"
        if not path.is_file():
            raise JobNotFound(job_id)
        return Job.model_validate(read_json(path))

    def list_ids(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if JOB_ID_RE.match(p.name) and p.is_dir())

    def delete(self, job_id: str) -> None:
        d = self.dir(job_id)
        if not d.exists():
            raise JobNotFound(job_id)
        shutil.rmtree(d, ignore_errors=True)

    def clean_tmp(self) -> int:
        """Remove orphaned *.tmp files left by a crash mid-write. Returns how many."""
        removed = 0
        for tmp in self.root.rglob("*.tmp"):
            tmp.unlink(missing_ok=True)
            removed += 1
        return removed
