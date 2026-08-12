from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class LocalModelCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    structured_output: bool
    thinking: bool


class LocalCapabilityDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    models: dict[str, LocalModelCapability]


class LocalModelCapabilityRegistry:
    """Configuration-backed capabilities; model behavior is never guessed in code."""

    def __init__(self, path: Path):
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.document = LocalCapabilityDocument.model_validate(raw)

    def require(self, model: str) -> LocalModelCapability:
        try:
            return self.document.models[model]
        except KeyError as error:
            raise ValueError(f"local model has no declared capabilities: {model}") from error
