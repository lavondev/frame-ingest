"""Remembers what an endpoint/model turned out to support (strict json_schema, reasoning_effort,
segment timestamps) so a 400 is only paid for once. Persisted to <home>/capabilities.json."""

from __future__ import annotations

import json
from pathlib import Path


class CapabilityMemo:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._data: dict[str, bool] = {}
        if path and path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                self._data = {k: bool(v) for k, v in loaded.items()}
            except (ValueError, OSError):
                self._data = {}

    @staticmethod
    def _key(base_url: str | None, model: str, name: str) -> str:
        return f"{base_url or 'openai'}|{model}|{name}"

    def get(self, base_url: str | None, model: str, name: str, default: bool = True) -> bool:
        return self._data.get(self._key(base_url, model, name), default)

    def set(self, base_url: str | None, model: str, name: str, value: bool) -> None:
        self._data[self._key(base_url, model, name)] = value
        if self._path:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            except OSError:
                pass

    def snapshot(self) -> dict[str, bool]:
        return dict(self._data)
