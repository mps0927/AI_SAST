from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .batching import BATCH_BUILDER_VERSION, SELECTOR_VERSION, BatchBuilder, ResultBlindSelector
from .cache import ContentHashCache
from .chunker import CHUNKER_VERSION, SemanticChunker
from .hashing import stable_digest
from .models import Chunk, ParseResult
from .parser import PARSER_VERSION, TreeSitterBackend, parser_cache_key
from .risk import RiskRanker
from .scanner import RepositoryScanner
from .tokens import TOKEN_ESTIMATOR_VERSION, TokenEstimator


SCHEMA_VERSION = "stage2-artifacts-v1"
PARSED_LANGUAGES = {"c", "cpp", "c-header", "cpp-header"}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for value in values
    )
    path.write_text(text, encoding="utf-8")


def run_pipeline(
    target: Path,
    artifacts: Path,
    cache_path: Path,
    chunk_tokens: int = 1800,
    batch_tokens: int = 6000,
) -> dict[str, Any]:
    resolved_target = target.resolve()
    try:
        public_target_path = resolved_target.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        # Generated inventory must remain portable and must not disclose the
        # caller's home/workspace path when the TargetCode lives elsewhere.
        public_target_path = resolved_target.name

    target = target.resolve()
    artifacts = artifacts.resolve()
    scanner = RepositoryScanner(target)
    files, commit = scanner.scan()
    estimator = TokenEstimator()
    parser = TreeSitterBackend()
    chunker = SemanticChunker(estimator, max_tokens=chunk_tokens)
    cache = ContentHashCache(cache_path)
    cache.retain_prefix(f"parse|{PARSER_VERSION}|")
    chunks: list[Chunk] = []

    for record in files:
        if record.language not in PARSED_LANGUAGES:
            continue
        data = (target / Path(record.path)).read_bytes()
        key = parser_cache_key(record.path, record.content_hash, record.language)
        cached = cache.get(key)
        if cached is None:
            result = parser.parse(data, record.language)
            cache.put(key, result.to_dict())
        else:
            result = ParseResult.from_dict(cached)
        record.parse_quality = result.quality
        record.parse_errors = result.errors
        for function in result.functions:
            chunks.extend(
                chunker.chunk_function(
                    record.path,
                    record.scope,
                    result.quality,
                    data,
                    function,
                )
            )
    cache.save()
    chunks.sort(key=lambda item: (item.path, item.start_byte, item.end_byte, item.chunk_id))

    ranker = RiskRanker()
    builder = BatchBuilder(ranker, max_tokens=batch_tokens)
    batches = builder.build(chunks)
    selector = ResultBlindSelector(count=3)
    selected, selection = selector.select(batches)
    batches.sort(key=lambda item: item.batch_id)

    non_exception_over_budget = [
        chunk.chunk_id
        for chunk in chunks
        if chunk.estimated_tokens > chunk_tokens and chunk.budget_exception is None
    ]
    batch_over_budget = [batch.batch_id for batch in batches if batch.source_token_estimate > batch_tokens]
    if non_exception_over_budget:
        raise RuntimeError(f"chunks exceed budget without exception: {non_exception_over_budget[:5]}")
    if batch_over_budget:
        raise RuntimeError(f"batches exceed budget: {batch_over_budget[:5]}")

    inventory = {
        "schema_version": SCHEMA_VERSION,
        "target": {
            "path": public_target_path,
            "origin": "https://github.com/raspberrypi/userland.git",
            "commit": commit,
            "read_only_contract": True,
        },
        "tools": {
            "scanner": scanner.version,
            "parser": PARSER_VERSION,
            "chunker": CHUNKER_VERSION,
            "token_estimator": TOKEN_ESTIMATOR_VERSION,
        },
        "statistics": {
            "tracked_files": len(files),
            "bytes": sum(item.bytes for item in files),
            "physical_lines": sum(item.physical_lines for item in files),
            "by_language": dict(sorted(Counter(item.language for item in files).items())),
            "by_scope": dict(sorted(Counter(item.scope for item in files).items())),
            "by_parse_quality": dict(sorted(Counter(item.parse_quality for item in files).items())),
            "build_membership_files": sum(bool(item.build_memberships) for item in files),
        },
        "files": [item.to_dict() for item in files],
    }

    chunk_dicts = [item.to_dict() for item in chunks]
    batch_dicts = [item.to_dict() for item in batches]
    selection.update(
        {
            "schema_version": SCHEMA_VERSION,
            "target_commit": commit,
            "candidate_batch_count": len(batches),
            "batch_builder_version": BATCH_BUILDER_VERSION,
        }
    )
    selection["selection_hash"] = "sha256:" + stable_digest(selection, length=64).lower()

    _write_json(artifacts / "inventory" / "repository.json", inventory)
    _write_jsonl(artifacts / "chunks" / "chunks.jsonl", chunk_dicts)
    _write_jsonl(artifacts / "batches" / "batches.jsonl", batch_dicts)
    _write_json(artifacts / "batches" / "selection.json", selection)

    stats = {
        "target_commit": commit,
        "tracked_files": len(files),
        "parsed_files": sum(item.language in PARSED_LANGUAGES for item in files),
        "parse_quality": dict(sorted(Counter(item.parse_quality for item in files if item.language in PARSED_LANGUAGES).items())),
        "chunks": len(chunks),
        "chunk_kinds": dict(sorted(Counter(item.kind for item in chunks).items())),
        "budget_exceptions": sum(item.budget_exception is not None for item in chunks),
        "max_chunk_tokens": max((item.estimated_tokens for item in chunks if item.budget_exception is None), default=0),
        "candidate_batches": len(batches),
        "max_batch_tokens": max((item.source_token_estimate for item in batches), default=0),
        "selected_batches": [
            {
                "batch_id": item.batch_id,
                "focus_path": item.focus_path,
                "focus_symbol": item.focus_symbol,
                "risk_tags": item.risk_tags,
                "risk_score": item.risk_score,
                "source_token_estimate": item.source_token_estimate,
                "selection_reasons": item.selection_reasons,
            }
            for item in selected
        ],
        "cache": {"hits": cache.hits, "misses": cache.misses, "pruned": cache.pruned, "entries": cache.size},
        "artifact_fingerprint": stable_digest(
            {
                "chunks": [item.chunk_id for item in chunks],
                "batches": [item.batch_id for item in batches],
                "selection": selection["selected_batch_ids"],
            },
            length=64,
        ).lower(),
    }
    _write_json(artifacts / "stage2-summary.json", stats)
    return stats
