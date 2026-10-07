from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any


def stable_hash(*parts: Any) -> str:
    joined = "|".join(str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


class Cache:
    """On-disk JSON cache with atomic writes and optional mtime-based TTL."""

    def __init__(self, root: Path):
        self.root = root

    def path(self, relpath: str) -> Path:
        return self.root / relpath

    def get(self, relpath: str, ttl_seconds: float | None = None) -> Any | None:
        path = self.path(relpath)
        if not path.exists():
            return None
        if ttl_seconds is not None:
            age = time.time() - path.stat().st_mtime
            if age > ttl_seconds:
                return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def put(self, relpath: str, data: Any) -> Path:
        path = self.path(relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def delete(self, relpath: str) -> None:
        path = self.path(relpath)
        if path.exists():
            path.unlink()
