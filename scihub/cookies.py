"""Per-mirror cookie persistence.

Each mirror keeps its own credential file (``<mirror>.json``) holding a simple
name->value map. This is what lets a one-time manual captcha solve carry over
to later runs.
"""
from __future__ import annotations

import json
from pathlib import Path


class CookieStore:
    """Loads and atomically saves cookies per mirror domain."""

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, mirror: str) -> Path:
        return self._dir / f"{mirror}.json"

    def load(self, mirror: str) -> dict[str, str]:
        path = self._path(mirror)
        if not path.exists():
            return {}
        try:
            # utf-8-sig tolerates a BOM if the file was hand-edited on Windows.
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}

    def save(self, mirror: str, cookies: dict[str, str]) -> None:
        if not cookies:
            return
        path = self._path(mirror)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(cookies, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)  # atomic on the same filesystem
