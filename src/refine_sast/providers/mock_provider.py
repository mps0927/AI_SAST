from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from ..hashing import stable_digest, stable_id
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
    MockUsage,
    ProofObligation,
    ProofStatus,
    RequestContextPayload,
    RequestKind,
    StructuredMessage,
    TriageOutput,
    Verdict,
)
from .usage_tracker import UsageTracker


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class MockResponse(Generic[T]):
    output: T
    usage: MockUsage
    provider: str = "mock-provider-v1"
    model: str = "mock-scripted"
    retries: int = 0
    latency_ms: int = 0


class MockProvider:
    """Deterministic scripted provider. It never accepts or returns source code."""

    name = "mock-provider-v1"

    def __init__(self, tracker: UsageTracker | None = None, workspace: Path | None = None):
        self.tracker = tracker
        self.workspace = workspace.resolve() if workspace else None

    def _response(
        self,
        context: AgentContext,
        output: T,
        usage: MockUsage,
    ) -> MockResponse[T]:
        if self.tracker is not None:
            if self.workspace is not None:
                prompt = (
                    self.workspace / "prompts" / f"{context.agent.value.lower()}.md"
                ).read_text(encoding="utf-8")
                prompt_version = f"prompt-v1:{stable_digest(prompt, 16).lower()}"
            else:
                prompt_version = "prompt-v1:mock-scripted"
            schema = json.dumps(
                output.__class__.model_json_schema(),  # type: ignore[attr-defined]
                sort_keys=True,
                separators=(",", ":"),
            )
            self.tracker.record(
                provider=self.name,
                model="mock-scripted",
                agent=context.agent,
                batch=context.batch_id,
                usage=usage,
                retry=0,
                latency_ms=0,
                prompt_version=prompt_version,
                schema_version=f"schema-v1:{stable_digest(schema, 16).lower()}",
                status="MOCK",
            )
        return MockResponse(output, usage)

    @staticmethod
    def _message(
        context: AgentContext,
        message_type: MessageType,
        payload: object,
        evidence_ids: list[str] | None = None,
    ) -> StructuredMessage:
        dumped = payload.model_dump(mode="json")  # type: ignore[attr-defined]
        message_id = stable_id(
            "MSG",
            {
                "provider": MockProvider.name,
                "agent": context.agent.value,
                "batch": context.batch_id,
                "phase": context.phase,
                "type": message_type.value,
                "payload": dumped,
                "evidence": evidence_ids or [],
            },
        )
        return StructuredMessage(
            message_id=message_id,
            message_type=message_type,
            agent=context.agent,
            batch_id=context.batch_id,
            evidence_ids=evidence_ids or [],
            payload=payload,
        )

    def triage(self, context: AgentContext) -> MockResponse[TriageOutput]:
        finding_id = stable_id("FND", {"batch": context.batch_id, "scenario": context.scenario.value})
        obligation_codes = ["EXTERNAL_CONTROL", "SOURCE_SINK_PATH", "GUARD_ABSENCE", "BUILD_REACHABLE"]
        obligations = [
            ProofObligation(
                obligation_id=f"{finding_id}-OBL-{index}",
                description_code=code,
            )
            for index, code in enumerate(obligation_codes, 1)
        ]
        payload = FindingPayload(
            finding_id=finding_id,
            hypothesis_code="MOCK_RISK_PATH_REQUIRES_PROOF",
            severity="HIGH",
            obligation_ids=[item.obligation_id for item in obligations],
        )
        message = self._message(context, MessageType.FINDING, payload, context.evidence_ids[:1])
        return self._response(
            context,
            TriageOutput(decision="PRIORITIZE", messages=[message], obligations=obligations),
            MockUsage(input_tokens=320, output_tokens=110),
        )

    def investigate(
        self, context: AgentContext, obligations: list[ProofObligation]
    ) -> MockResponse[InvestigatorOutput]:
        if context.scenario == Verdict.INCONCLUSIVE:
            candidate_index = context.phase + 1
            if candidate_index >= len(context.available_chunk_ids):
                return self._response(
                    context,
                    InvestigatorOutput(
                        messages=[],
                        unresolved_obligation_ids=[item.obligation_id for item in obligations if item.required],
                        terminate=True,
                    ),
                    MockUsage(input_tokens=280, output_tokens=90),
                )
            payload = RequestContextPayload(
                request_kind=RequestKind.GET_DATAFLOW_SLICE,
                chunk_id=context.available_chunk_ids[candidate_index],
                obligation_id=obligations[min(context.phase, len(obligations) - 1)].obligation_id,
                reason_code="MOCK_REQUIRED_EDGE_NOT_IN_CURRENT_EVIDENCE",
            )
            message = self._message(context, MessageType.REQUEST_CONTEXT, payload)
            return self._response(
                context,
                InvestigatorOutput(
                    messages=[message],
                    unresolved_obligation_ids=[item.obligation_id for item in obligations if item.required],
                    terminate=False,
                ),
                MockUsage(input_tokens=280, output_tokens=90),
            )

        evidence_ids = context.evidence_ids[:1]
        payload = EvidencePayload(
            summary_code="MOCK_PATH_SUPPORT_PACKET",
            supports_obligation_ids=[item.obligation_id for item in obligations],
        )
        message = self._message(context, MessageType.EVIDENCE, payload, evidence_ids)
        updates = [
            item.model_copy(update={"status": ProofStatus.SUPPORTED, "evidence_ids": evidence_ids})
            for item in obligations
        ]
        return self._response(
            context,
            InvestigatorOutput(messages=[message], obligation_updates=updates, terminate=True),
            MockUsage(input_tokens=520, output_tokens=180),
        )

    def challenge(
        self,
        context: AgentContext,
        obligations: list[ProofObligation],
        finding_id: str,
    ) -> MockResponse[ChallengerOutput]:
        if context.scenario != Verdict.REJECTED:
            return self._response(
                context,
                ChallengerOutput(messages=[], contradiction_found=False),
                MockUsage(input_tokens=410, output_tokens=100),
            )
        target = next(item for item in obligations if item.required)
        payload = ContradictionPayload(
            finding_id=finding_id,
            reason_code="MOCK_REQUIRED_CONDITION_REFUTED",
            refutes_obligation_id=target.obligation_id,
        )
        message = self._message(context, MessageType.CONTRADICTION, payload, context.evidence_ids[:1])
        update = target.model_copy(update={"status": ProofStatus.REFUTED, "evidence_ids": context.evidence_ids[:1]})
        return self._response(
            context,
            ChallengerOutput(messages=[message], obligation_updates=[update], contradiction_found=True),
            MockUsage(input_tokens=430, output_tokens=140),
        )

    def judge(
        self, context: AgentContext, obligations: list[ProofObligation]
    ) -> MockResponse[JudgeOutput]:
        supported = sum(item.required and item.status == ProofStatus.SUPPORTED for item in obligations)
        refuted = sum(item.required and item.status == ProofStatus.REFUTED for item in obligations)
        unknown = sum(item.required and item.status == ProofStatus.UNKNOWN for item in obligations)
        required = sum(item.required for item in obligations)
        return self._response(
            context,
            JudgeOutput(
                verdict=context.scenario,
                rationale_code=f"MOCK_{context.scenario.value}_RULE",
                required_obligations=required,
                supported_obligations=supported,
                refuted_obligations=refuted,
                unknown_obligations=unknown,
            ),
            MockUsage(input_tokens=480, output_tokens=130),
        )
