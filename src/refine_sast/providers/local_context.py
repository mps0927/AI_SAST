from __future__ import annotations

from typing import Any, Protocol

from ..runtime.evidence import EvidenceBlackboard
from ..runtime.evidence import StructuredMessageBus
from ..runtime.retriever import ContextRetriever
from ..stage3_schemas import AgentContext
from ..stage3_schemas import AgentName


class LocalContextSource(Protocol):
    def build(self, context: AgentContext) -> dict[str, Any]: ...


class VerifiedLocalContextSource:
    """Materialize only Blackboard-registered evidence for one transient LLM call."""

    def __init__(
        self,
        blackboard: EvidenceBlackboard,
        retriever: ContextRetriever,
        message_bus: StructuredMessageBus | None = None,
    ):
        self.blackboard = blackboard
        self.retriever = retriever
        self.message_bus = message_bus
        self._role_packets: dict[AgentName, dict[str, Any]] = {}

    def set_role_packet(self, agent: AgentName, packet: dict[str, Any]) -> None:
        """Attach a source-free structured packet for exactly one Agent role."""

        forbidden = {"source", "source_code", "raw", "prompt", "api_key", "secret"}

        def verify(value: Any) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    if str(key).lower() in forbidden:
                        raise ValueError("structured Agent packet contains forbidden field")
                    verify(nested)
            elif isinstance(value, list):
                for nested in value:
                    verify(nested)

        verify(packet)
        self._role_packets[agent] = packet

    def build(self, context: AgentContext) -> dict[str, Any]:
        evidence: list[dict[str, Any]] = []
        for evidence_id in context.evidence_ids:
            record = self.blackboard.get(evidence_id)
            item = {
                "evidence_id": record.evidence_id,
                "chunk_id": record.chunk_id,
                "path": record.path,
                "start_line": record.start_line,
                "end_line": record.end_line,
                "content_hash": record.content_hash,
            }
            # Judge receives a proof packet and verified provenance metadata only.
            # Other roles receive transient source for independent analysis.
            if context.agent != AgentName.JUDGE:
                source = self.retriever.materialize_evidence(record)
                item["source"] = source.decode("utf-8", errors="replace")
            evidence.append(item)
        messages = []
        if self.message_bus is not None:
            messages = [
                self.message_bus.get(message_id).model_dump(mode="json")
                for message_id in context.input_message_ids
            ]
        return {
            "verified_evidence": evidence,
            "structured_messages": messages,
            "role_packet": self._role_packets.get(context.agent, {}),
        }


class StaticLocalContextSource:
    """Network-free contract-test source. It deliberately contains no code."""

    def __init__(self, value: dict[str, Any] | None = None):
        self.value = value or {"verified_evidence": []}

    def build(self, context: AgentContext) -> dict[str, Any]:
        del context
        return self.value
