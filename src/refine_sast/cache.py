from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable


class ContentHashCache:
    """Small persistent cache keyed only by versioned content-derived keys."""

    def __init__(self, path: Path):
        self.path = path
        self._items: dict[str, Any] = {}
        self.hits = 0
        self.misses = 0
        self.pruned = 0
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("format") == 1:
                    self._items = payload.get("items", {})
            except (OSError, ValueError, TypeError):
                self._items = {}

    def get(self, key: str) -> Any | None:
        if key in self._items:
            self.hits += 1
            return self._items[key]
        self.misses += 1
        return None

    def put(self, key: str, value: Any) -> None:
        self._items[key] = value

    def retain_prefix(self, prefix: str) -> None:
        stale = [key for key in self._items if not key.startswith(prefix)]
        for key in stale:
            del self._items[key]
        self.pruned += len(stale)

    def get_or_compute(self, key: str, factory: Callable[[], Any]) -> Any:
        cached = self.get(key)
        if cached is not None:
            return cached
        value = factory()
        self.put(key, value)
        return value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"format": 1, "items": self._items}
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    @property
    def size(self) -> int:
        return len(self._items)
