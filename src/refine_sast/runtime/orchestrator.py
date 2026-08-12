from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..agents import ChallengerAgent, InvestigatorAgent, JudgeAgent, TriageAgent
from ..providers.mock_provider import MockProvider
from ..providers.usage_tracker import UsageTracker
from ..stage3_schemas import (
    AgentContext,
    AgentName,
    ChallengerInput,
    FindingPayload,
    MessageType,
    InvestigatorInput,
    JudgeInput,
    ProofObligation,
    RequestContextPayload,
    TriageInput,
    Verdict,
)
from .event_log import EventLogger
from .evidence import EvidenceBlackboard, StructuredMessageBus
from .proofs import ProofTable
from .retriever import ContextRetriever
from .token_governor import TokenBudgetExceeded, TokenGovernor


STATE_ORDER = ["CREATED", "TRIAGE", "INVESTIGATION", "CHALLENGE", "JUDGMENT", "COMPLETE"]


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class Orchestrator:
    version = "orchestrator-v1"

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.target = self.workspace / "target" / "userland"
        self.artifacts = self.workspace / "artifacts"
        self.selection = json.loads(
            (self.artifacts / "batches" / "selection.json").read_text(encoding="utf-8")
        )
        if not self.selection.get("result_blind") or self.selection.get("selection_count") != 3:
            raise ValueError("Stage 3 requires exactly three result-blind selected batches")
        self.batches = {
            item["batch_id"]: item
            for item in _jsonl(self.artifacts / "batches" / "batches.jsonl")
        }
        self.retriever = ContextRetriever(
            self.target, self.artifacts / "chunks" / "chunks.jsonl"
        )

    @staticmethod
    def _context(
        *,
        agent: AgentName,
        batch: dict[str, Any],
        scenario: Verdict,
        phase: int,
        message_ids: list[str],
        evidence_ids: list[str],
        available_chunk_ids: list[str],
        governor: TokenGovernor,
    ) -> AgentContext:
        return AgentContext(
            agent=agent,
            batch_id=batch["batch_id"],
            scenario=scenario,
            phase=phase,
            input_message_ids=list(message_ids),
            evidence_ids=list(evidence_ids),
            available_chunk_ids=list(available_chunk_ids),
            risk_tags=list(batch["risk_tags"]),
            budget_snapshot=governor.snapshot(),
        )

    def run_all(self) -> list[dict[str, Any]]:
        scenarios = [Verdict.CONFIRMED, Verdict.REJECTED, Verdict.INCONCLUSIVE]
        selected = self.selection["selected_batch_ids"]
        return [self.run_batch(batch_id, scenario, index) for index, (batch_id, scenario) in enumerate(zip(selected, scenarios), 1)]

    def run_batch(self, batch_id: str, scenario: Verdict, order: int) -> dict[str, Any]:
        batch = self.batches[batch_id]
        run_id = f"stage3-{order}-{scenario.value.lower()}-{batch_id[-8:].lower()}"
        run_dir = self.artifacts / "runs" / run_id
        governor = TokenGovernor()
        tracker = UsageTracker(run_id, run_dir / "token-ledger.json")
        provider = MockProvider(tracker=tracker, workspace=self.workspace)
        blackboard = EvidenceBlackboard(self.target)
        bus = StructuredMessageBus(blackboard, set(self.retriever.chunks))
        logger = EventLogger(run_id, batch_id)
        agents = {
            AgentName.TRIAGE: TriageAgent(provider, governor, self.workspace),
            AgentName.INVESTIGATOR: InvestigatorAgent(provider, governor, self.workspace),
            AgentName.CHALLENGER: ChallengerAgent(provider, governor, self.workspace),
            AgentName.JUDGE: JudgeAgent(provider, governor, self.workspace),
        }
        available = list(dict.fromkeys(batch["member_chunk_ids"] + batch["dependency_refs"]))
        focus_packet = self.retriever.retrieve(
            batch["focus_chunk_id"],
            agent=AgentName.TRIAGE,
            blackboard=blackboard,
            governor=governor,
            charge_context=False,
        )
        # The packet's source bytes remain local and are intentionally not logged or put on the bus.
        del focus_packet.source
        state = "CREATED"

        def transition(agent: AgentName, target_state: str) -> None:
            nonlocal state
            logger.record(
                event_type="STATE_TRANSITION",
                agent=agent,
                from_state=state,
                to_state=target_state,
                detail_codes=[f"ENTER_{target_state}"],
            )
            state = target_state

        def publish_messages(messages: list[Any], prompt_version: str) -> None:
            for message in messages:
                bus.publish(message)
                logger.record(
                    event_type="MESSAGE_PUBLISHED",
                    agent=message.agent,
                    from_state=state,
                    to_state=state,
                    prompt_version=prompt_version,
                    message_type=message.message_type,
                    input_evidence_ids=message.evidence_ids,
                    output_message_ids=[message.message_id],
                )

        transition(AgentName.TRIAGE, "TRIAGE")
        triage_context = self._context(
            agent=AgentName.TRIAGE,
            batch=batch,
            scenario=scenario,
            phase=0,
            message_ids=[],
            evidence_ids=blackboard.ids(),
            available_chunk_ids=available,
            governor=governor,
        )
        triage_run = agents[AgentName.TRIAGE].run(
            TriageInput(
                context=triage_context,
                focus_evidence_id=focus_packet.evidence.evidence_id,
                security_sketch_code="STAGE2_RISK_MANIFEST",
            )
        )
        publish_messages(triage_run.output.messages, triage_run.prompt_version)
        logger.record(
            event_type="AGENT_CALL",
            agent=AgentName.TRIAGE,
            from_state=state,
            to_state=state,
            prompt_version=triage_run.prompt_version,
            message_type=triage_run.output.messages[0].message_type,
            input_evidence_ids=triage_context.evidence_ids,
            output_message_ids=[item.message_id for item in triage_run.output.messages],
            usage=triage_run.usage,
            detail_codes=[triage_run.output.decision],
        )
        proof_table = ProofTable(triage_run.output.obligations)
        finding_payload = triage_run.output.messages[0].payload
        if not isinstance(finding_payload, FindingPayload):
            raise ValueError("Triage did not produce Finding payload")

        transition(AgentName.INVESTIGATOR, "INVESTIGATION")
        phase = 0
        unresolved: list[str] = []
        while True:
            investigator_context = self._context(
                agent=AgentName.INVESTIGATOR,
                batch=batch,
                scenario=scenario,
                phase=phase,
                message_ids=bus.ids(),
                evidence_ids=blackboard.ids(),
                available_chunk_ids=available,
                governor=governor,
            )
            investigator_run = agents[AgentName.INVESTIGATOR].run(
                InvestigatorInput(
                    context=investigator_context,
                    obligations=proof_table.values(),
                    finding_message_id=triage_run.output.messages[0].message_id,
                )
            )
            publish_messages(investigator_run.output.messages, investigator_run.prompt_version)
            proof_table.apply(investigator_run.output.obligation_updates)
            unresolved = list(investigator_run.output.unresolved_obligation_ids)
            message_type = investigator_run.output.messages[0].message_type if investigator_run.output.messages else None
            logger.record(
                event_type="AGENT_CALL",
                agent=AgentName.INVESTIGATOR,
                from_state=state,
                to_state=state,
                prompt_version=investigator_run.prompt_version,
                message_type=message_type,
                input_evidence_ids=investigator_context.evidence_ids,
                output_message_ids=[item.message_id for item in investigator_run.output.messages],
                usage=investigator_run.usage,
                detail_codes=[f"PHASE_{phase}"],
            )
            request = next(
                (
                    item
                    for item in investigator_run.output.messages
                    if item.message_type == MessageType.REQUEST_CONTEXT
                ),
                None,
            )
            if request is None:
                break
            payload = request.payload
            if not isinstance(payload, RequestContextPayload):
                raise ValueError("REQUEST_CONTEXT payload mismatch")
            try:
                packet = self.retriever.retrieve(
                    payload.chunk_id,
                    agent=AgentName.INVESTIGATOR,
                    blackboard=blackboard,
                    governor=governor,
                    charge_context=True,
                )
                del packet.source
                logger.record(
                    event_type="CONTEXT_GRANTED",
                    agent=AgentName.INVESTIGATOR,
                    from_state=state,
                    to_state=state,
                    message_type=MessageType.REQUEST_CONTEXT,
                    input_evidence_ids=[packet.evidence.evidence_id],
                    output_message_ids=[request.message_id],
                    detail_codes=[payload.request_kind.value],
                )
                phase += 1
            except TokenBudgetExceeded as error:
                logger.record(
                    event_type="CONTEXT_DENIED",
                    agent=AgentName.INVESTIGATOR,
                    from_state=state,
                    to_state=state,
                    message_type=MessageType.REQUEST_CONTEXT,
                    output_message_ids=[request.message_id],
                    detail_codes=[str(error)],
                )
                break

        transition(AgentName.CHALLENGER, "CHALLENGE")
        challenger_context = self._context(
            agent=AgentName.CHALLENGER,
            batch=batch,
            scenario=scenario,
            phase=0,
            message_ids=bus.ids(),
            evidence_ids=blackboard.ids(),
            available_chunk_ids=available,
            governor=governor,
        )
        challenger_run = agents[AgentName.CHALLENGER].run(
            ChallengerInput(
                context=challenger_context,
                obligations=proof_table.values(),
                finding_id=finding_payload.finding_id,
                investigator_message_ids=[
                    message_id
                    for message_id in bus.ids()
                    if bus.get(message_id).agent == AgentName.INVESTIGATOR
                ],
            )
        )
        publish_messages(challenger_run.output.messages, challenger_run.prompt_version)
        proof_table.apply(challenger_run.output.obligation_updates)
        logger.record(
            event_type="AGENT_CALL",
            agent=AgentName.CHALLENGER,
            from_state=state,
            to_state=state,
            prompt_version=challenger_run.prompt_version,
            message_type=challenger_run.output.messages[0].message_type if challenger_run.output.messages else None,
            input_evidence_ids=challenger_context.evidence_ids,
            output_message_ids=[item.message_id for item in challenger_run.output.messages],
            usage=challenger_run.usage,
            detail_codes=["CONTRADICTION_FOUND" if challenger_run.output.contradiction_found else "NO_CONTRADICTION"],
        )

        transition(AgentName.JUDGE, "JUDGMENT")
        judge_context = self._context(
            agent=AgentName.JUDGE,
            batch=batch,
            scenario=scenario,
            phase=0,
            message_ids=bus.ids(),
            evidence_ids=blackboard.ids(),
            available_chunk_ids=[],
            governor=governor,
        )
        judge_run = agents[AgentName.JUDGE].run(
            JudgeInput(
                context=judge_context,
                obligations=proof_table.values(),
                unresolved_obligation_ids=unresolved,
                finding_message_id=triage_run.output.messages[0].message_id,
                contradiction_message_ids=[
                    message_id
                    for message_id in bus.ids()
                    if bus.get(message_id).message_type == MessageType.CONTRADICTION
                ],
            )
        )
        logger.record(
            event_type="AGENT_CALL",
            agent=AgentName.JUDGE,
            from_state=state,
            to_state=state,
            prompt_version=judge_run.prompt_version,
            input_evidence_ids=judge_context.evidence_ids,
            usage=judge_run.usage,
            detail_codes=[judge_run.output.verdict.value, judge_run.output.rationale_code],
        )
        transition(AgentName.JUDGE, "COMPLETE")
        blackboard.verify_all()
        logger.write(run_dir / "events.jsonl")

        summary = {
            "schema_version": "stage3-mock-run-v1",
            "orchestrator_version": self.version,
            "provider": provider.name,
            "run_id": run_id,
            "batch_id": batch_id,
            "selection_hash": self.selection["selection_hash"],
            "scenario": scenario.value,
            "verdict": judge_run.output.verdict.value,
            "mock_only": True,
            "not_a_security_finding": True,
            "agent_call_counts": {
                role.value: sum(
                    event["event_type"] == "AGENT_CALL" and event["agent"] == role.value
                    for event in logger.events
                )
                for role in AgentName
            },
            "message_ids": bus.ids(),
            "evidence_ids": blackboard.ids(),
            "proof_obligations": [item.model_dump(mode="json") for item in proof_table.values()],
            "token_governor": governor.snapshot(),
            "event_count": len(logger.events),
        }
        (run_dir / "run-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return summary
