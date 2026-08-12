from __future__ import annotations

import hashlib
import json
from typing import Any


def content_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def stable_digest(value: Any, length: int = 20) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length].upper()


def stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}-{stable_digest(value)}"
