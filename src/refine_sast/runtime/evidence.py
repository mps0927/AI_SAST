from __future__ import annotations

from pathlib import Path, PurePosixPath

from pydantic import Field

from ..hashing import content_hash, stable_id
from ..stage3_schemas import MessageType, StrictModel, StructuredMessage


class EvidenceValidationError(ValueError):
    pass


class EvidenceRecord(StrictModel):
    evidence_id: str
    chunk_id: str
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    start_byte: int = Field(ge=0)
    end_byte: int = Field(gt=0)
    content_hash: str
    evidence_kind: str


class EvidenceBlackboard:
    def __init__(self, target: Path):
        self.target = target.resolve()
        self._records: dict[str, EvidenceRecord] = {}

    def register(
        self,
        *,
        chunk_id: str,
        path: str,
        start_line: int,
        end_line: int,
        start_byte: int,
        end_byte: int,
        expected_hash: str,
        evidence_kind: str = "source-chunk",
    ) -> EvidenceRecord:
        posix = PurePosixPath(path)
        if posix.is_absolute() or ".." in posix.parts:
            raise EvidenceValidationError("evidence path escapes TargetCode")
        absolute = (self.target / Path(path)).resolve()
        try:
            absolute.relative_to(self.target)
        except ValueError as error:
            raise EvidenceValidationError("evidence path escapes TargetCode") from error
        if not absolute.is_file():
            raise EvidenceValidationError("evidence file does not exist")
        data = absolute.read_bytes()
        if not (0 <= start_byte < end_byte <= len(data)):
            raise EvidenceValidationError("invalid evidence byte range")
        actual_start = data.count(b"\n", 0, start_byte) + 1
        actual_end = data.count(b"\n", 0, max(start_byte, end_byte - 1)) + 1
        if (start_line, end_line) != (actual_start, actual_end):
            raise EvidenceValidationError("evidence line range does not match byte range")
        actual_hash = content_hash(data[start_byte:end_byte])
        if expected_hash != actual_hash:
            raise EvidenceValidationError("evidence content hash mismatch")
        evidence_id = stable_id(
            "EVD",
            {
                "chunk_id": chunk_id,
                "path": path,
                "start_byte": start_byte,
                "end_byte": end_byte,
                "content_hash": actual_hash,
            },
        )
        record = EvidenceRecord(
            evidence_id=evidence_id,
            chunk_id=chunk_id,
            path=path,
            start_line=start_line,
            end_line=end_line,
            start_byte=start_byte,
            end_byte=end_byte,
            content_hash=actual_hash,
            evidence_kind=evidence_kind,
        )
        self._records[evidence_id] = record
        return record

    def get(self, evidence_id: str) -> EvidenceRecord:
        try:
            return self._records[evidence_id]
        except KeyError as error:
            raise EvidenceValidationError(f"unknown Evidence ID: {evidence_id}") from error

    def verify_all(self) -> None:
        current = list(self._records.values())
        for item in current:
            verified = self.register(
                chunk_id=item.chunk_id,
                path=item.path,
                start_line=item.start_line,
                end_line=item.end_line,
                start_byte=item.start_byte,
                end_byte=item.end_byte,
                expected_hash=item.content_hash,
                evidence_kind=item.evidence_kind,
            )
            if verified.evidence_id != item.evidence_id:
                raise EvidenceValidationError("Evidence ID changed during verification")

    def ids(self) -> list[str]:
        return sorted(self._records)


class StructuredMessageBus:
    def __init__(self, blackboard: EvidenceBlackboard, known_chunk_ids: set[str]):
        self.blackboard = blackboard
        self.known_chunk_ids = known_chunk_ids
        self._messages: dict[str, StructuredMessage] = {}

    def publish(self, message: StructuredMessage) -> None:
        if message.message_id in self._messages:
            raise ValueError(f"duplicate message ID: {message.message_id}")
        for evidence_id in message.evidence_ids:
            self.blackboard.get(evidence_id)
        if message.message_type == MessageType.REQUEST_CONTEXT:
            chunk_id = message.payload.chunk_id  # type: ignore[union-attr]
            if chunk_id not in self.known_chunk_ids:
                raise ValueError(f"unknown requested Chunk ID: {chunk_id}")
        self._messages[message.message_id] = message

    def get(self, message_id: str) -> StructuredMessage:
        return self._messages[message_id]

    def ids(self) -> list[str]:
        return list(self._messages)
