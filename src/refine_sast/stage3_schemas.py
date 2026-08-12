from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentName(StrEnum):
    TRIAGE = "TRIAGE"
    INVESTIGATOR = "INVESTIGATOR"
    CHALLENGER = "CHALLENGER"
    JUDGE = "JUDGE"


class MessageType(StrEnum):
    FINDING = "FINDING"
    EVIDENCE = "EVIDENCE"
    REQUEST_CONTEXT = "REQUEST_CONTEXT"
    CONTRADICTION = "CONTRADICTION"


class Verdict(StrEnum):
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class ProofStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    REFUTED = "REFUTED"
    UNKNOWN = "UNKNOWN"


class RequestKind(StrEnum):
    GET_FUNCTION = "GET_FUNCTION"
    GET_CALLERS = "GET_CALLERS"
    GET_CALLEES = "GET_CALLEES"
    GET_TYPE_DEFINITION = "GET_TYPE_DEFINITION"
    GET_MACRO = "GET_MACRO"
    GET_GUARDS = "GET_GUARDS"
    GET_DATAFLOW_SLICE = "GET_DATAFLOW_SLICE"
    GET_GLOBAL_WRITES = "GET_GLOBAL_WRITES"
    GET_BUILD_CONDITION = "GET_BUILD_CONDITION"


class FindingPayload(StrictModel):
    finding_id: str
    hypothesis_code: str
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    obligation_ids: list[str]


class EvidencePayload(StrictModel):
    summary_code: str
    supports_obligation_ids: list[str] = Field(default_factory=list)


class RequestContextPayload(StrictModel):
    request_kind: RequestKind
    chunk_id: str
    obligation_id: str
    reason_code: str


class ContradictionPayload(StrictModel):
    finding_id: str
    reason_code: str
    refutes_obligation_id: str


MessagePayload = FindingPayload | EvidencePayload | RequestContextPayload | ContradictionPayload


class StructuredMessage(StrictModel):
    message_id: str
    message_type: MessageType
    agent: AgentName
    batch_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    payload: MessagePayload

    @model_validator(mode="after")
    def payload_matches_type(self) -> "StructuredMessage":
        expected = {
            MessageType.FINDING: FindingPayload,
            MessageType.EVIDENCE: EvidencePayload,
            MessageType.REQUEST_CONTEXT: RequestContextPayload,
            MessageType.CONTRADICTION: ContradictionPayload,
        }[self.message_type]
        if not isinstance(self.payload, expected):
            raise ValueError(f"{self.message_type} requires {expected.__name__}")
        if self.message_type in {MessageType.EVIDENCE, MessageType.CONTRADICTION} and not self.evidence_ids:
            raise ValueError(f"{self.message_type} requires at least one Evidence ID")
        return self


class ProofObligation(StrictModel):
    obligation_id: str
    description_code: str
    required: bool = True
    status: ProofStatus = ProofStatus.UNKNOWN
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def supported_requires_evidence(self) -> "ProofObligation":
        if self.status in {ProofStatus.SUPPORTED, ProofStatus.REFUTED} and not self.evidence_ids:
            raise ValueError(f"{self.status} obligation requires Evidence ID")
        return self


class AgentContext(StrictModel):
    agent: AgentName
    batch_id: str
    scenario: Verdict
    phase: int = 0
    input_message_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    available_chunk_ids: list[str] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)
    budget_snapshot: dict[str, int | bool | str | list[str]] = Field(default_factory=dict)


class MockUsage(StrictModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)


class TriageInput(StrictModel):
    context: AgentContext
    focus_evidence_id: str
    security_sketch_code: str


class InvestigatorInput(StrictModel):
    context: AgentContext
    obligations: list[ProofObligation]
    finding_message_id: str


class ChallengerInput(StrictModel):
    context: AgentContext
    obligations: list[ProofObligation]
    finding_id: str
    investigator_message_ids: list[str] = Field(default_factory=list)


class JudgeInput(StrictModel):
    context: AgentContext
    obligations: list[ProofObligation]
    unresolved_obligation_ids: list[str] = Field(default_factory=list)
    finding_message_id: str
    contradiction_message_ids: list[str] = Field(default_factory=list)


class TriageOutput(StrictModel):
    decision: Literal["PRIORITIZE", "DEFER", "NEED_CONTEXT"]
    messages: list[StructuredMessage]
    obligations: list[ProofObligation]
    terminate: bool = True


class InvestigatorOutput(StrictModel):
    messages: list[StructuredMessage]
    obligation_updates: list[ProofObligation] = Field(default_factory=list)
    unresolved_obligation_ids: list[str] = Field(default_factory=list)
    terminate: bool


class ChallengerOutput(StrictModel):
    messages: list[StructuredMessage]
    obligation_updates: list[ProofObligation] = Field(default_factory=list)
    contradiction_found: bool
    terminate: bool = True


class JudgeOutput(StrictModel):
    verdict: Verdict
    rationale_code: str
    required_obligations: int
    supported_obligations: int
    refuted_obligations: int
    unknown_obligations: int
    terminate: bool = True


class ProviderEnvelope(StrictModel):
    output: Any
    usage: MockUsage


ROLE_OUTPUT_MODELS = {
    AgentName.TRIAGE: TriageOutput,
    AgentName.INVESTIGATOR: InvestigatorOutput,
    AgentName.CHALLENGER: ChallengerOutput,
    AgentName.JUDGE: JudgeOutput,
}

ROLE_INPUT_MODELS = {
    AgentName.TRIAGE: TriageInput,
    AgentName.INVESTIGATOR: InvestigatorInput,
    AgentName.CHALLENGER: ChallengerInput,
    AgentName.JUDGE: JudgeInput,
}


def export_role_schemas(destination: Any) -> None:
    from pathlib import Path
    import json

    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    for role, model in ROLE_INPUT_MODELS.items():
        value = model.model_json_schema()
        value["x-agent-role"] = role.value
        (root / f"{role.value.lower()}-input.schema.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    for role, model in ROLE_OUTPUT_MODELS.items():
        value = model.model_json_schema()
        value["x-agent-role"] = role.value
        (root / f"{role.value.lower()}-output.schema.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    (root / "structured-message.schema.json").write_text(
        json.dumps(StructuredMessage.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
