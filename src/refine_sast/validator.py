from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .hashing import content_hash


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def validate_artifacts(
    target: Path,
    artifacts: Path,
    chunk_budget: int = 1800,
    batch_budget: int = 6000,
) -> dict[str, Any]:
    target = target.resolve()
    inventory = json.loads((artifacts / "inventory" / "repository.json").read_text(encoding="utf-8"))
    chunks = _jsonl(artifacts / "chunks" / "chunks.jsonl")
    batches = _jsonl(artifacts / "batches" / "batches.jsonl")
    selection = json.loads((artifacts / "batches" / "selection.json").read_text(encoding="utf-8"))

    files = inventory["files"]
    paths = [item["path"] for item in files]
    if len(paths) != len(set(paths)):
        raise AssertionError("duplicate inventory path")
    for item in files:
        raw = (target / Path(item["path"])).read_bytes()
        if content_hash(raw) != item["content_hash"]:
            raise AssertionError(f"inventory hash mismatch: {item['path']}")

    required_chunk = {
        "chunk_id", "path", "start_line", "end_line", "content_hash", "scope",
        "risk_tags", "estimated_tokens", "parse_quality", "start_byte", "end_byte",
    }
    chunk_by_id: dict[str, dict[str, Any]] = {}
    over_budget_exceptions = 0
    for item in chunks:
        missing = required_chunk - item.keys()
        if missing:
            raise AssertionError(f"chunk fields missing {missing}: {item.get('chunk_id')}")
        if item["chunk_id"] in chunk_by_id:
            raise AssertionError(f"duplicate chunk ID: {item['chunk_id']}")
        chunk_by_id[item["chunk_id"]] = item
        raw_file = (target / Path(item["path"])).read_bytes()
        raw = raw_file[item["start_byte"] : item["end_byte"]]
        if content_hash(raw) != item["content_hash"]:
            raise AssertionError(f"chunk hash mismatch: {item['chunk_id']}")
        start_line = raw_file.count(b"\n", 0, item["start_byte"]) + 1
        end_line = raw_file.count(b"\n", 0, max(item["start_byte"], item["end_byte"] - 1)) + 1
        if (start_line, end_line) != (item["start_line"], item["end_line"]):
            raise AssertionError(f"chunk line range mismatch: {item['chunk_id']}")
        if item["estimated_tokens"] > chunk_budget:
            if not item.get("budget_exception"):
                raise AssertionError(f"chunk budget exceeded: {item['chunk_id']}")
            over_budget_exceptions += 1

    batch_by_id: dict[str, dict[str, Any]] = {}
    for item in batches:
        if item["batch_id"] in batch_by_id:
            raise AssertionError(f"duplicate batch ID: {item['batch_id']}")
        batch_by_id[item["batch_id"]] = item
        if item["focus_chunk_id"] not in chunk_by_id:
            raise AssertionError(f"missing focus chunk: {item['batch_id']}")
        for reference in item["member_chunk_ids"] + item["dependency_refs"]:
            if reference not in chunk_by_id:
                raise AssertionError(f"missing batch reference {reference}: {item['batch_id']}")
        expected_tokens = sum(chunk_by_id[value]["estimated_tokens"] for value in item["member_chunk_ids"])
        if expected_tokens != item["source_token_estimate"]:
            raise AssertionError(f"batch token sum mismatch: {item['batch_id']}")
        if expected_tokens > batch_budget:
            raise AssertionError(f"batch budget exceeded: {item['batch_id']}")
        if not item["risk_components"] or not item["risk_tags"]:
            raise AssertionError(f"risk manifest missing: {item['batch_id']}")

    selected_ids = selection["selected_batch_ids"]
    if len(selected_ids) != 3 or len(set(selected_ids)) != 3:
        raise AssertionError("selection must contain exactly three unique batch IDs")
    if not selection["result_blind"] or selection["llm_calls_before_selection"] != 0:
        raise AssertionError("selection is not result-blind")
    selected = [batch_by_id[value] for value in selected_ids]
    if any(item["selection_status"] != "selected" for item in selected):
        raise AssertionError("selected status mismatch")
    if len({item["focus_path"] for item in selected}) != 3:
        raise AssertionError("selected focal files are not distinct")

    safe = target.as_posix()
    status = subprocess.check_output(
        ["git", "-c", f"safe.directory={safe}", "-C", str(target), "status", "--porcelain=v1"]
    )
    if status:
        raise AssertionError("TargetCode is dirty")
    return {
        "inventory_files": len(files),
        "chunks": len(chunks),
        "batches": len(batches),
        "selected": selected_ids,
        "chunk_budget_exceptions": over_budget_exceptions,
        "target_clean": True,
    }
