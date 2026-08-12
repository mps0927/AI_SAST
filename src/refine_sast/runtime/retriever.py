from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..hashing import content_hash
from ..stage3_schemas import AgentName
from .evidence import EvidenceBlackboard, EvidenceRecord, EvidenceValidationError
from .token_governor import TokenGovernor


@dataclass(slots=True)
class ContextPacket:
    """Ephemeral source packet. It must never be placed on the message bus or event log."""

    evidence: EvidenceRecord
    source: bytes
    estimated_tokens: int


class ContextRetriever:
    def __init__(self, target: Path, chunks_path: Path):
        self.target = target.resolve()
        self.chunks: dict[str, dict[str, object]] = {}
        for line in chunks_path.read_text(encoding="utf-8").splitlines():
            if line:
                value = json.loads(line)
                self.chunks[value["chunk_id"]] = value

    def materialize_evidence(self, evidence: EvidenceRecord) -> bytes:
        """Read a registered range for one provider call and verify it again.

        The returned bytes are ephemeral. Callers must not persist them in logs,
        ledgers, messages, or cache values.
        """

        chunk = self.chunks.get(evidence.chunk_id)
        if chunk is None:
            raise EvidenceValidationError("Evidence references an unknown Chunk ID")
        expected = {
            "path": evidence.path,
            "start_line": evidence.start_line,
            "end_line": evidence.end_line,
            "start_byte": evidence.start_byte,
            "end_byte": evidence.end_byte,
            "content_hash": evidence.content_hash,
        }
        for key, value in expected.items():
            if chunk.get(key) != value:
                raise EvidenceValidationError(f"Evidence does not match Chunk metadata: {key}")
        data = (self.target / Path(evidence.path)).read_bytes()
        source = data[evidence.start_byte:evidence.end_byte]
        if content_hash(source) != evidence.content_hash:
            raise EvidenceValidationError("Evidence changed before provider materialization")
        return source

    def retrieve(
        self,
        chunk_id: str,
        *,
        agent: AgentName,
        blackboard: EvidenceBlackboard,
        governor: TokenGovernor,
        charge_context: bool = True,
    ) -> ContextPacket:
        try:
            chunk = self.chunks[chunk_id]
        except KeyError as error:
            raise KeyError(f"unknown Chunk ID: {chunk_id}") from error
        estimated_tokens = int(chunk["estimated_tokens"])
        if charge_context:
            governor.authorize_context(agent, estimated_tokens)
        path = str(chunk["path"])
        data = (self.target / Path(path)).read_bytes()
        start_byte, end_byte = int(chunk["start_byte"]), int(chunk["end_byte"])
        source = data[start_byte:end_byte]
        if content_hash(source) != chunk["content_hash"]:
            raise ValueError(f"retrieved Chunk hash mismatch: {chunk_id}")
        evidence = blackboard.register(
            chunk_id=chunk_id,
            path=path,
            start_line=int(chunk["start_line"]),
            end_line=int(chunk["end_line"]),
            start_byte=start_byte,
            end_byte=end_byte,
            expected_hash=str(chunk["content_hash"]),
        )
        return ContextPacket(evidence=evidence, source=source, estimated_tokens=estimated_tokens)
