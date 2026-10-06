"""Stage + unit caches. Everything paid is cached so reruns never repeat API calls.

Stage key = sha256(video sha, stage, stage version, relevant settings, upstream stage keys).
Unit caches (one file per audio chunk / vision batch / correction window / chapter) live under
stages/<stage>/units and are keyed by the stage key plus a unit identity, so a crash mid-stage
only loses the unit that was in flight.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from frame_ingest.models import StageName
from frame_ingest.storage import read_json, write_json

M = TypeVar("M", bound=BaseModel)


def digest(*parts: Any) -> str:
    blob = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class UnitCache:
    def __init__(self, directory: Path, stage_key: str) -> None:
        self.dir = directory
        self.stage_key = stage_key

    def _path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def get(self, name: str, model: type[M], ident: str = "") -> M | None:
        path = self._path(name)
        if not path.is_file():
            return None
        try:
            payload = read_json(path)
            if payload.get("key") != digest(self.stage_key, ident):
                return None
            return model.model_validate(payload["data"])
        except (ValueError, KeyError, ValidationError, OSError):
            return None

    def put(self, name: str, value: BaseModel, ident: str = "") -> None:
        write_json(
            self._path(name),
            {"key": digest(self.stage_key, ident), "data": value.model_dump(mode="json")},
        )


class StageCache:
    def __init__(self, job_dir: Path) -> None:
        self.root = job_dir / "stages"

    def stage_dir(self, stage: StageName) -> Path:
        return self.root / stage.value

    def load(self, stage: StageName, key: str, model: type[M]) -> M | None:
        d = self.stage_dir(stage)
        meta, result = d / "meta.json", d / "result.json"
        if not (meta.is_file() and result.is_file()):
            return None
        try:
            if read_json(meta).get("key") != key:
                return None
            return model.model_validate(read_json(result))
        except (ValueError, ValidationError, OSError):
            return None

    def save(self, stage: StageName, key: str, result: BaseModel) -> str:
        """Persist a stage result. Returns the content hash that downstream keys chain on, so a
        re-executed stage with different output invalidates everything after it."""
        d = self.stage_dir(stage)
        d.mkdir(parents=True, exist_ok=True)
        write_json(d / "result.json", result)
        out = digest(result.model_dump_json())
        write_json(d / "meta.json", {"key": key, "out": out})
        return out

    def out_hash(self, stage: StageName) -> str:
        try:
            return str(read_json(self.stage_dir(stage) / "meta.json").get("out", ""))
        except (ValueError, OSError):
            return ""

    def is_current(self, stage: StageName, key: str) -> bool:
        meta = self.stage_dir(stage) / "meta.json"
        try:
            return meta.is_file() and read_json(meta).get("key") == key
        except (ValueError, OSError):
            return False

    def invalidate(self, stage: StageName) -> None:
        shutil.rmtree(self.stage_dir(stage), ignore_errors=True)

    def units(self, stage: StageName, stage_key: str) -> UnitCache:
        d = self.stage_dir(stage) / "units"
        d.mkdir(parents=True, exist_ok=True)
        return UnitCache(d, stage_key)
