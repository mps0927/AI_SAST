from __future__ import annotations

from collections import defaultdict
from pathlib import PurePosixPath

from .hashing import stable_id
from .models import Batch, Chunk
from .risk import RiskRanker


BATCH_BUILDER_VERSION = "batch-builder-v1"
SELECTOR_VERSION = "result-blind-diversity-v1"


class BatchBuilder:
    def __init__(self, ranker: RiskRanker, max_tokens: int = 6000):
        self.ranker = ranker
        self.max_tokens = max_tokens

    def build(self, chunks: list[Chunk]) -> list[Batch]:
        by_symbol: dict[str, list[Chunk]] = defaultdict(list)
        by_id = {chunk.chunk_id: chunk for chunk in chunks}
        for chunk in chunks:
            if chunk.parent_symbol:
                by_symbol[chunk.parent_symbol].append(chunk)

        batches: list[Batch] = []
        focal_chunks = [
            chunk
            for chunk in chunks
            if chunk.scope == "primary-source"
            and chunk.risk_tags
            and chunk.parse_quality in {"full", "partial"}
            and chunk.budget_exception is None
        ]
        for focal in sorted(focal_chunks, key=lambda item: item.chunk_id):
            candidates: list[Chunk] = []
            seen = {focal.chunk_id}
            for call in focal.calls:
                simple = call.split("::")[-1].split("->")[-1].split(".")[-1]
                for dependency in by_symbol.get(simple, []):
                    if dependency.chunk_id not in seen:
                        seen.add(dependency.chunk_id)
                        candidates.append(dependency)
            candidates.sort(
                key=lambda item: (
                    item.path != focal.path,
                    not bool(set(item.risk_tags) & set(focal.risk_tags)),
                    item.estimated_tokens,
                    item.chunk_id,
                )
            )
            members = [focal.chunk_id]
            dependencies: list[str] = []
            total = focal.estimated_tokens
            for candidate in candidates:
                if total + candidate.estimated_tokens <= self.max_tokens:
                    members.append(candidate.chunk_id)
                    total += candidate.estimated_tokens
                else:
                    dependencies.append(candidate.chunk_id)
            score, components = self.ranker.score(focal)
            batch_id = stable_id(
                "BAT",
                {
                    "version": BATCH_BUILDER_VERSION,
                    "focus": focal.chunk_id,
                    "members": members,
                },
            )
            batches.append(
                Batch(
                    batch_id=batch_id,
                    focus_chunk_id=focal.chunk_id,
                    focus_path=focal.path,
                    focus_symbol=focal.symbol,
                    member_chunk_ids=members,
                    dependency_refs=dependencies,
                    risk_tags=focal.risk_tags,
                    risk_score=score,
                    risk_components=components,
                    source_token_estimate=total,
                )
            )
        return batches


class ResultBlindSelector:
    def __init__(self, count: int = 3):
        self.count = count

    @staticmethod
    def _similarity(left: Batch, right: Batch) -> float:
        score = 0.0
        if left.focus_path == right.focus_path:
            score += 0.4
        if PurePosixPath(left.focus_path).parts[0] == PurePosixPath(right.focus_path).parts[0]:
            score += 0.2
        left_tags, right_tags = set(left.risk_tags), set(right.risk_tags)
        union = left_tags | right_tags
        if union:
            score += 0.25 * len(left_tags & right_tags) / len(union)
        left_members, right_members = set(left.member_chunk_ids), set(right.member_chunk_ids)
        member_union = left_members | right_members
        if member_union:
            score += 0.15 * len(left_members & right_members) / len(member_union)
        return min(1.0, score)

    def select(self, batches: list[Batch]) -> tuple[list[Batch], dict[str, object]]:
        if len(batches) < self.count:
            raise ValueError(f"need at least {self.count} candidate batches, got {len(batches)}")
        highest = max(batch.risk_score for batch in batches) or 1.0
        selected: list[Batch] = []
        remaining = list(batches)
        trace: list[dict[str, object]] = []
        while len(selected) < self.count:
            ranked: list[tuple[float, float, str, Batch]] = []
            selected_dirs = {PurePosixPath(item.focus_path).parts[0] for item in selected}
            selected_tags = {tag for item in selected for tag in item.risk_tags}
            selected_paths = {item.focus_path for item in selected}
            for candidate in remaining:
                normalized = candidate.risk_score / highest
                similarity = max((self._similarity(candidate, item) for item in selected), default=0.0)
                diversity_bonus = 0.0
                top_dir = PurePosixPath(candidate.focus_path).parts[0]
                if selected and candidate.focus_path not in selected_paths:
                    diversity_bonus += 0.06
                if selected and top_dir not in selected_dirs:
                    diversity_bonus += 0.06
                if selected and set(candidate.risk_tags) - selected_tags:
                    diversity_bonus += 0.04
                selection_score = normalized - 0.25 * similarity + diversity_bonus
                ranked.append((selection_score, normalized, candidate.batch_id, candidate))
            ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
            selection_score, normalized, _, winner = ranked[0]
            similarity = max((self._similarity(winner, item) for item in selected), default=0.0)
            winner.selection_status = "selected"
            winner.selection_reasons = [
                f"normalized_risk={normalized:.6f}",
                f"max_similarity={similarity:.6f}",
                f"selection_score={selection_score:.6f}",
                "selected before any LLM analysis",
            ]
            selected.append(winner)
            remaining.remove(winner)
            trace.append(
                {
                    "order": len(selected),
                    "batch_id": winner.batch_id,
                    "focus_path": winner.focus_path,
                    "focus_symbol": winner.focus_symbol,
                    "risk_tags": winner.risk_tags,
                    "risk_score": winner.risk_score,
                    "selection_score": round(selection_score, 6),
                }
            )
        for batch in remaining:
            batch.selection_status = "not-selected"
        manifest: dict[str, object] = {
            "selector_version": SELECTOR_VERSION,
            "selection_count": self.count,
            "result_blind": True,
            "llm_calls_before_selection": 0,
            "constraints": {
                "distinct_focus_files": len({item.focus_path for item in selected}),
                "top_level_directories": sorted({PurePosixPath(item.focus_path).parts[0] for item in selected}),
                "risk_tags": sorted({tag for item in selected for tag in item.risk_tags}),
            },
            "selected_batch_ids": [item.batch_id for item in selected],
            "selection_trace": trace,
        }
        return selected, manifest
