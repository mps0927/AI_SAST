from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..hashing import stable_id
from ..stage3_schemas import (
    AgentContext,
    AgentName,
    ChallengerOutput,
    ContradictionPayload,
    EvidencePayload,
    FindingPayload,
    InvestigatorOutput,
    JudgeOutput,
    MessageType,
    ProofObligation,
    ProofStatus,
    RequestContextPayload,
    RequestKind,
    StructuredMessage,
    TriageOutput,
    Verdict,
)


class GeminiWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TriageObligationWire(GeminiWireModel):
    description_code: str = Field(description="Short uppercase proof requirement")
    required: bool = Field(description="Whether the Judge must resolve this proof")


class GeminiTriageWireOutput(GeminiWireModel):
    decision: Literal["PRIORITIZE", "DEFER"]
    hypothesis_code: str = Field(description="Concise vulnerability hypothesis")
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    obligations: list[TriageObligationWire] = Field(min_length=1, max_length=6)
    terminate: bool


class ObligationAssessmentWire(GeminiWireModel):
    obligation_index: int = Field(ge=0, description="Zero-based input obligation index")
    status: Literal[ProofStatus.SUPPORTED, ProofStatus.UNKNOWN]
    summary_code: str = Field(description="Concise evidence reasoning code")
    evidence_indexes: list[int] = Field(
        description="Zero-based indexes into verified_evidence"
    )


class GeminiInvestigatorWireOutput(GeminiWireModel):
    assessments: list[ObligationAssessmentWire]
    request_context: bool
    request_kind: RequestKind
    requested_chunk_index: int = Field(ge=0)
    requested_obligation_index: int = Field(ge=0)
    request_reason_code: str
    terminate: bool


class GeminiChallengerWireOutput(GeminiWireModel):
    contradiction_found: bool
    refuted_obligation_index: int = Field(ge=0)
    reason_code: str
    evidence_indexes: list[int]
    terminate: bool


class GeminiJudgeWireOutput(GeminiWireModel):
    verdict: Verdict
    rationale_code: str
    terminate: bool


WIRE_OUTPUT_MODELS: dict[AgentName, type[BaseModel]] = {
    AgentName.TRIAGE: GeminiTriageWireOutput,
    AgentName.INVESTIGATOR: GeminiInvestigatorWireOutput,
    AgentName.CHALLENGER: GeminiChallengerWireOutput,
    AgentName.JUDGE: GeminiJudgeWireOutput,
}


ALLOWED_BY_ROLE = {
    AgentName.TRIAGE: {MessageType.FINDING, MessageType.REQUEST_CONTEXT},
    AgentName.INVESTIGATOR: {MessageType.EVIDENCE, MessageType.REQUEST_CONTEXT},
    AgentName.CHALLENGER: {
        MessageType.CONTRADICTION,
        MessageType.REQUEST_CONTEXT,
    },
    AgentName.JUDGE: set(),
}


def wire_output_model(agent: AgentName) -> type[BaseModel]:
    return WIRE_OUTPUT_MODELS[agent]


def _indexed(values: list[str], indexes: list[int], label: str) -> list[str]:
    if len(indexes) != len(set(indexes)):
        raise ValueError(f"duplicate {label} index")
    try:
        return [values[index] for index in indexes]
    except IndexError as error:
        raise ValueError(f"{label} index is outside verified input") from error


def _message(
    *,
    context: AgentContext,
    message_type: MessageType,
    payload: BaseModel,
    evidence_ids: list[str],
) -> StructuredMessage:
    message_id = stable_id(
        "MSG",
        {
            "agent": context.agent.value,
            "batch": context.batch_id,
            "phase": context.phase,
            "type": message_type.value,
            "payload": payload.model_dump(mode="json"),
            "evidence": evidence_ids,
        },
    )
    return StructuredMessage(
        message_id=message_id,
        message_type=message_type,
        agent=context.agent,
        batch_id=context.batch_id,
        evidence_ids=evidence_ids,
        payload=payload,
    )


def _triage_to_domain(
    value: GeminiTriageWireOutput, context: AgentContext
) -> TriageOutput:
    finding_id = stable_id(
        "FND",
        {
            "batch": context.batch_id,
            "hypothesis": value.hypothesis_code,
            "severity": value.severity,
        },
    )
    obligations = [
        ProofObligation(
            obligation_id=stable_id(
                "OBL",
                {
                    "finding": finding_id,
                    "index": index,
                    "description": item.description_code,
                },
            ),
            description_code=item.description_code,
            required=item.required,
        )
        for index, item in enumerate(value.obligations)
    ]
    finding = FindingPayload(
        finding_id=finding_id,
        hypothesis_code=value.hypothesis_code,
        severity=value.severity,
        obligation_ids=[item.obligation_id for item in obligations],
    )
    evidence_ids = context.evidence_ids[:1]
    message = _message(
        context=context,
        message_type=MessageType.FINDING,
        payload=finding,
        evidence_ids=evidence_ids,
    )
    return TriageOutput(
        decision=value.decision,
        messages=[message],
        obligations=obligations,
        terminate=value.terminate,
    )


def _investigator_to_domain(
    value: GeminiInvestigatorWireOutput,
    context: AgentContext,
    obligations: list[ProofObligation],
) -> InvestigatorOutput:
    seen: set[int] = set()
    updates: list[ProofObligation] = []
    messages: list[StructuredMessage] = []
    unresolved: set[str] = set()
    for assessment in value.assessments:
        index = assessment.obligation_index
        if index in seen or index >= len(obligations):
            raise ValueError("obligation assessment index is invalid")
        seen.add(index)
        original = obligations[index]
        evidence_ids = _indexed(
            context.evidence_ids, assessment.evidence_indexes, "evidence"
        )
        status = ProofStatus(assessment.status)
        if status == ProofStatus.SUPPORTED and not evidence_ids:
            raise ValueError("SUPPORTED assessment omitted verified evidence")
        if status == ProofStatus.UNKNOWN:
            evidence_ids = []
            unresolved.add(original.obligation_id)
        update = original.model_copy(
            update={"status": status, "evidence_ids": evidence_ids}
        )
        updates.append(update)
        if status == ProofStatus.SUPPORTED:
            payload = EvidencePayload(
                summary_code=assessment.summary_code,
                supports_obligation_ids=[original.obligation_id],
            )
            messages.append(
                _message(
                    context=context,
                    message_type=MessageType.EVIDENCE,
                    payload=payload,
                    evidence_ids=evidence_ids,
                )
            )
    unresolved.update(
        item.obligation_id
        for index, item in enumerate(obligations)
        if index not in seen and item.required
    )
    if value.request_context:
        if value.requested_obligation_index >= len(obligations):
            raise ValueError("requested obligation index is outside proof packet")
        chunks = context.available_chunk_ids
        if value.requested_chunk_index >= len(chunks):
            raise ValueError("requested Chunk index is outside available context")
        target = obligations[value.requested_obligation_index]
        unresolved.add(target.obligation_id)
        request = RequestContextPayload(
            request_kind=value.request_kind,
            chunk_id=chunks[value.requested_chunk_index],
            obligation_id=target.obligation_id,
            reason_code=value.request_reason_code,
        )
        messages.append(
            _message(
                context=context,
                message_type=MessageType.REQUEST_CONTEXT,
                payload=request,
                evidence_ids=[],
            )
        )
    return InvestigatorOutput(
        messages=messages,
        obligation_updates=updates,
        unresolved_obligation_ids=sorted(unresolved),
        terminate=value.terminate,
    )


def _challenger_to_domain(
    value: GeminiChallengerWireOutput,
    context: AgentContext,
    obligations: list[ProofObligation],
    finding_id: str,
) -> ChallengerOutput:
    if not value.contradiction_found:
        return ChallengerOutput(
            messages=[],
            obligation_updates=[],
            contradiction_found=False,
            terminate=value.terminate,
        )
    if value.refuted_obligation_index >= len(obligations):
        raise ValueError("refuted obligation index is outside proof packet")
    evidence_ids = _indexed(
        context.evidence_ids, value.evidence_indexes, "evidence"
    )
    if not evidence_ids:
        raise ValueError("CONTRADICTION omitted verified evidence")
    original = obligations[value.refuted_obligation_index]
    payload = ContradictionPayload(
        finding_id=finding_id,
        reason_code=value.reason_code,
        refutes_obligation_id=original.obligation_id,
    )
    message = _message(
        context=context,
        message_type=MessageType.CONTRADICTION,
        payload=payload,
        evidence_ids=evidence_ids,
    )
    update = original.model_copy(
        update={"status": ProofStatus.REFUTED, "evidence_ids": evidence_ids}
    )
    return ChallengerOutput(
        messages=[message],
        obligation_updates=[update],
        contradiction_found=True,
        terminate=value.terminate,
    )


def _judge_to_domain(
    value: GeminiJudgeWireOutput, obligations: list[ProofObligation]
) -> JudgeOutput:
    required = [item for item in obligations if item.required]
    return JudgeOutput(
        verdict=value.verdict,
        rationale_code=value.rationale_code,
        required_obligations=len(required),
        supported_obligations=sum(
            item.status == ProofStatus.SUPPORTED for item in required
        ),
        refuted_obligations=sum(
            item.status == ProofStatus.REFUTED for item in required
        ),
        unknown_obligations=sum(
            item.status == ProofStatus.UNKNOWN for item in required
        ),
        terminate=value.terminate,
    )


def wire_to_domain(
    agent: AgentName,
    value: BaseModel,
    *,
    context: AgentContext,
    obligations: list[ProofObligation] | None = None,
    finding_id: str = "",
) -> BaseModel:
    proof = obligations or []
    if agent == AgentName.TRIAGE:
        return _triage_to_domain(value, context)  # type: ignore[arg-type]
    if agent == AgentName.INVESTIGATOR:
        return _investigator_to_domain(value, context, proof)  # type: ignore[arg-type]
    if agent == AgentName.CHALLENGER:
        if not finding_id:
            raise ValueError("Challenger adapter requires verified finding ID")
        return _challenger_to_domain(  # type: ignore[arg-type]
            value, context, proof, finding_id
        )
    return _judge_to_domain(value, proof)  # type: ignore[arg-type]


def _evidence_indexes(evidence_ids: list[str], context_ids: list[str]) -> list[int]:
    return [context_ids.index(item) for item in evidence_ids if item in context_ids]


def domain_to_wire(
    value: BaseModel, *, context: AgentContext | None = None
) -> dict[str, object]:
    """Convert deterministic domain fixtures to the shallow Gemini contract."""

    context_ids = context.evidence_ids if context else ["EVD-1"]
    if isinstance(value, TriageOutput):
        finding = next(
            item.payload
            for item in value.messages
            if item.message_type == MessageType.FINDING
        )
        return {
            "decision": value.decision,
            "hypothesis_code": finding.hypothesis_code,
            "severity": finding.severity,
            "obligations": [
                {
                    "description_code": item.description_code,
                    "required": item.required,
                }
                for item in value.obligations
            ],
            "terminate": value.terminate,
        }
    if isinstance(value, InvestigatorOutput):
        assessments = [
            {
                "obligation_index": index,
                "status": item.status.value,
                "summary_code": "FIXTURE_EVIDENCE_ASSESSMENT",
                "evidence_indexes": _evidence_indexes(
                    item.evidence_ids, context_ids
                ),
            }
            for index, item in enumerate(value.obligation_updates)
        ]
        return {
            "assessments": assessments,
            "request_context": False,
            "request_kind": RequestKind.GET_DATAFLOW_SLICE.value,
            "requested_chunk_index": 0,
            "requested_obligation_index": 0,
            "request_reason_code": "NOT_REQUESTED",
            "terminate": value.terminate,
        }
    if isinstance(value, ChallengerOutput):
        message = value.messages[0] if value.messages else None
        return {
            "contradiction_found": value.contradiction_found,
            "refuted_obligation_index": 0,
            "reason_code": (
                message.payload.reason_code if message is not None else "NONE"
            ),
            "evidence_indexes": _evidence_indexes(
                message.evidence_ids if message is not None else [], context_ids
            ),
            "terminate": value.terminate,
        }
    if isinstance(value, JudgeOutput):
        return {
            "verdict": value.verdict.value,
            "rationale_code": value.rationale_code,
            "terminate": value.terminate,
        }
    raise TypeError(f"unsupported domain fixture: {type(value).__name__}")
