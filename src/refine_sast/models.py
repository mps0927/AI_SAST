from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class FileRecord:
    path: str
    language: str
    bytes: int
    physical_lines: int
    content_hash: str
    scope: str
    build_memberships: list[str] = field(default_factory=list)
    parse_quality: str = "skipped"
    parse_errors: int = 0
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FunctionRegion:
    symbol: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    calls: list[dict[str, Any]] = field(default_factory=list)
    referenced_types: list[str] = field(default_factory=list)
    referenced_macros: list[str] = field(default_factory=list)
    safe_segments: list[dict[str, Any]] = field(default_factory=list)
    complexity: int = 0
    guard_count: int = 0
    pointer_operations: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FunctionRegion":
        return cls(**value)


@dataclass(slots=True)
class ParseResult:
    quality: str
    errors: int
    backend: str
    functions: list[FunctionRegion] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "quality": self.quality,
            "errors": self.errors,
            "backend": self.backend,
            "functions": [item.to_dict() for item in self.functions],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ParseResult":
        return cls(
            quality=value["quality"],
            errors=value["errors"],
            backend=value["backend"],
            functions=[FunctionRegion.from_dict(item) for item in value["functions"]],
        )


@dataclass(slots=True)
class Chunk:
    chunk_id: str
    path: str
    symbol: str
    kind: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    content_hash: str
    scope: str
    estimated_tokens: int
    calls: list[str]
    referenced_types: list[str]
    referenced_macros: list[str]
    risk_tags: list[str]
    risk_evidence: list[dict[str, Any]]
    parse_quality: str
    complexity: int
    guard_count: int
    pointer_operations: int
    parent_symbol: str | None = None
    part_index: int = 1
    part_count: int = 1
    context_header: str = ""
    budget_exception: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Batch:
    batch_id: str
    focus_chunk_id: str
    focus_path: str
    focus_symbol: str
    member_chunk_ids: list[str]
    dependency_refs: list[str]
    risk_tags: list[str]
    risk_score: float
    risk_components: dict[str, float]
    source_token_estimate: int
    selection_reasons: list[str] = field(default_factory=list)
    selection_status: str = "candidate"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
