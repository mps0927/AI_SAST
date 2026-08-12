from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..agents import ChallengerAgent, InvestigatorAgent, JudgeAgent, TriageAgent
from ..cache import ContentHashCache
from ..hashing import stable_id
from ..providers.gemini_provider import GeminiProvider
from ..providers.gemini_transport import GeminiTransport, RateLimitedGeminiClient
from ..providers.local_context import VerifiedLocalContextSource
from ..providers.model_router import ModelRouter
from ..providers.retry import ProviderInconclusive, RetryPolicy
from ..providers.usage_tracker import UsageRecord, UsageTracker
from ..stage3_schemas import (
    AgentContext,
    AgentName,
    ChallengerInput,
    FindingPayload,
    InvestigatorInput,
    JudgeInput,
    MessageType,
    ProofObligation,
    ProofStatus,
    RequestContextPayload,
    StructuredMessage,
    TriageInput,
    Verdict,
)
from .event_log import EventLogger
from .evidence import EvidenceBlackboard, StructuredMessageBus
from .proofs import ProofTable
from .retriever import ContextRetriever
from .token_governor import TokenBudgetExceeded, TokenGovernor


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _target_clean(target: Path) -> bool:
    safe = target.as_posix()
    output = subprocess.check_output(
        ["git", "-c", f"safe.directory={safe}", "-C", safe, "status", "--porcelain=v1"]
    )
    return output == b""


class LiveGeminiAnalysis:
    version = "gemini-live-orchestrator-v1"

    def __init__(
        self,
        workspace: Path,
        *,
        transport: Any | None = None,
        run_id: str | None = None,
        max_api_calls: int = 19,
        min_start_interval_seconds: float = 13.0,
        output_root: Path | None = None,
        cache_path: Path | None = None,
        offline_test: bool = False,
        continuation_of: str | None = None,
    ):
        self.workspace = workspace.resolve()
        self.target = self.workspace / "target" / "userland"
        self.artifacts = self.workspace / "artifacts"
        self.selection_path = self.artifacts / "batches" / "selection.json"
        self.selection = json.loads(self.selection_path.read_text(encoding="utf-8"))
        selected = self.selection.get("selected_batch_ids", [])
        if (
            not self.selection.get("result_blind")
            or self.selection.get("selection_count") != 3
            or len(selected) != 3
            or len(set(selected)) != 3
        ):
            raise ValueError("live analysis requires exactly three fixed result-blind batches")
        self.selection_bytes = self.selection_path.read_bytes()
        self.batches = {
            item["batch_id"]: item
            for item in _jsonl(self.artifacts / "batches" / "batches.jsonl")
        }
        if any(batch_id not in self.batches for batch_id in selected):
            raise ValueError("selected Batch is missing from batches.jsonl")
        self.retriever = ContextRetriever(
            self.target, self.artifacts / "chunks" / "chunks.jsonl"
        )
        self.router = ModelRouter(
            self.workspace / "config" / "model-routing.json", profile="gemini-free"
        )
        if self.router.provider != "gemini-generate-content":
            raise ValueError("gemini-free profile selected a different provider")
        if any(self.router.route(agent).model != "gemini-3.6-flash" for agent in AgentName):
            raise ValueError("live analysis model routing does not match approval")
        self.run_id = run_id or (
            "gemini-live-3batch-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        )
        self.run_dir = (output_root or (self.artifacts / "runs")) / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.tracker = UsageTracker(self.run_id, self.run_dir / "token-ledger.json")
        self.continuation_of = continuation_of
        self.prior_api_attempts = 0
        if continuation_of:
            prior_path = self.artifacts / "runs" / continuation_of / "token-ledger.json"
            prior_document = json.loads(prior_path.read_text(encoding="utf-8"))
            prior_records = [
                UsageRecord.model_validate(item) for item in prior_document.get("calls", [])
            ]
            self.tracker.records.extend(prior_records)
            self.prior_api_attempts = sum(
                item.status != "CACHE_HIT" for item in prior_records
            )
        self.cache = ContentHashCache(
            cache_path or (self.workspace / ".cache" / "gemini-live-responses.json")
        )
        self.offline_test = offline_test
        if transport is None:
            key = os.environ.get("GEMINI_API_KEY", "")
            if not key:
                raise ValueError("GEMINI_API_KEY is not present")
            transport = GeminiTransport(self.router.profile.endpoint or "", key)
        self.transport = RateLimitedGeminiClient(
            transport,
            max_calls=max_api_calls,
            min_start_interval_seconds=min_start_interval_seconds,
        )
        self.batch_results: list[dict[str, Any]] = []
        self.all_events: list[dict[str, Any]] = []
        self.source_token_transmissions = 0
        self.logical_calls = 0

    @staticmethod
    def _context(
        *,
        agent: AgentName,
        batch: dict[str, Any],
        phase: int,
        message_ids: list[str],
        evidence_ids: list[str],
        available_chunk_ids: list[str],
        governor: TokenGovernor,
    ) -> AgentContext:
        return AgentContext(
            agent=agent,
            batch_id=batch["batch_id"],
            # GeminiProvider removes this Mock-only field from the live payload.
            scenario=Verdict.INCONCLUSIVE,
            phase=phase,
            input_message_ids=list(message_ids),
            evidence_ids=list(evidence_ids),
            available_chunk_ids=list(available_chunk_ids),
            risk_tags=list(batch["risk_tags"]),
            budget_snapshot=governor.snapshot(),
        )

    def _estimated_source_tokens(self, evidence_ids: list[str], blackboard: EvidenceBlackboard) -> int:
        return sum(
            int(self.retriever.chunks[blackboard.get(evidence_id).chunk_id]["estimated_tokens"])
            for evidence_id in evidence_ids
        )

    def preflight(self) -> dict[str, Any]:
        if self.offline_test:
            # Offline contract tests must be reproducible from the public
            # repository without relying on ignored, machine-local run history.
            smoke = {
                "success": True,
                "model": "gemini-3.6-flash",
                "structured_output_valid": True,
                "api_calls_attempted": 1,
            }
        else:
            smoke_path = (
                self.artifacts
                / "runs"
                / "gemini-smoke-triage-once"
                / "smoke-result.json"
            )
            smoke = json.loads(smoke_path.read_text(encoding="utf-8"))
        batch_estimates = []
        for batch_id in self.selection["selected_batch_ids"]:
            batch = self.batches[batch_id]
            batch_estimates.append(
                {
                    "batch_id": batch_id,
                    "focus_chunk_id": batch["focus_chunk_id"],
                    "focus_path": batch["focus_path"],
                    "focus_symbol": batch["focus_symbol"],
                    "member_chunk_count": len(batch["member_chunk_ids"]),
                    "source_token_estimate": int(batch["source_token_estimate"]),
                    "focus_token_estimate": int(
                        self.retriever.chunks[batch["focus_chunk_id"]]["estimated_tokens"]
                    ),
                    "risk_tags": batch["risk_tags"],
                }
            )
        checks = {
            "gemini_api_key_present": bool(os.environ.get("GEMINI_API_KEY")) or self.offline_test,
            "gemini_free_profile": self.router.profile_name == "gemini-free",
            "approved_model_only": all(
                self.router.route(agent).model == "gemini-3.6-flash" for agent in AgentName
            ),
            "exactly_three_fixed_batches": len(self.selection["selected_batch_ids"]) == 3,
            "selection_result_blind": self.selection["result_blind"] is True,
            "target_clean": _target_clean(self.target),
            "smoke_success": (
                smoke.get("success") is True
                and smoke.get("structured_output_valid") is True
                and smoke.get("api_calls_attempted") == 1
            ),
            "base_calls_fit_remaining_rpd": (
                max(0, 12 - self.prior_api_attempts) <= self.transport.max_calls
            ),
            "rpm_interval_enforced": (
                self.transport.min_start_interval_seconds >= 12.0 or self.offline_test
            ),
        }
        result = {
            "schema_version": "gemini-live-preflight-v1",
            "passed": all(checks.values()),
            "checks": checks,
            "selection_hash": self.selection["selection_hash"],
            "selected_batch_ids": self.selection["selected_batch_ids"],
            "batch_estimates": batch_estimates,
            "base_logical_calls": 12,
            "base_api_calls_remaining_after_continuation": max(
                0, 12 - self.prior_api_attempts
            ),
            "global_api_attempt_cap": self.transport.max_calls,
            "max_extra_attempts": self.transport.max_calls
            - max(0, 12 - self.prior_api_attempts),
            "min_call_start_interval_seconds": self.transport.min_start_interval_seconds,
            "network_calls_so_far": self.prior_api_attempts,
            "continuation_of": self.continuation_of,
        }
        _write_json(self.run_dir / "preflight.json", result)
        return result

    def _provider(
        self,
        blackboard: EvidenceBlackboard,
        governor: TokenGovernor,
        bus: StructuredMessageBus,
    ) -> GeminiProvider:
        return GeminiProvider(
            workspace=self.workspace,
            router=self.router,
            tracker=self.tracker,
            context_source=VerifiedLocalContextSource(
                blackboard, self.retriever, message_bus=bus
            ),
            transport=self.transport,
            cache=self.cache,
            retry_policy=RetryPolicy(max_attempts=2, jitter_ratio=0),
        )

    @staticmethod
    def _live_governor() -> TokenGovernor:
        governor = TokenGovernor()
        # Gemini usage includes system prompt and JSON Schema tokens in addition
        # to the selected function source. These input ceilings remain bounded,
        # but are calibrated above the observed structured-request overhead.
        governor.roles[AgentName.TRIAGE].input_limit = 3_500
        governor.roles[AgentName.INVESTIGATOR].input_limit = 7_000
        governor.roles[AgentName.CHALLENGER].input_limit = 7_000
        governor.roles[AgentName.JUDGE].input_limit = 5_000
        return governor

    @staticmethod
    def _publish(
        *,
        bus: StructuredMessageBus,
        logger: EventLogger,
        messages: list[Any],
        prompt_version: str,
        state: str,
    ) -> None:
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

    def _run_batch_legacy(self, batch_id: str, order: int) -> dict[str, Any]:
        batch = self.batches[batch_id]
        governor = self._live_governor()
        blackboard = EvidenceBlackboard(self.target)
        bus = StructuredMessageBus(blackboard, set(self.retriever.chunks))
        logger = EventLogger(self.run_id, batch_id)
        provider = self._provider(blackboard, governor, bus)
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

        # TRIAGE
        transition(AgentName.TRIAGE, "TRIAGE")
        triage_context = self._context(
            agent=AgentName.TRIAGE,
            batch=batch,
            phase=0,
            message_ids=[],
            evidence_ids=blackboard.ids(),
            available_chunk_ids=available,
            governor=governor,
        )
        self.source_token_transmissions += self._estimated_source_tokens(
            triage_context.evidence_ids, blackboard
        )
        self.logical_calls += 1
        triage_run = agents[AgentName.TRIAGE].run(
            TriageInput(
                context=triage_context,
                focus_evidence_id=focus_packet.evidence.evidence_id,
                security_sketch_code="STAGE2_RESULT_BLIND_RISK_MANIFEST",
            )
        )
        self._publish(
            bus=bus,
            logger=logger,
            messages=triage_run.output.messages,
            prompt_version=triage_run.prompt_version,
            state=state,
        )
        logger.record(
            event_type="AGENT_CALL",
            agent=AgentName.TRIAGE,
            from_state=state,
            to_state=state,
            prompt_version=triage_run.prompt_version,
            message_type=(triage_run.output.messages[0].message_type if triage_run.output.messages else None),
            input_evidence_ids=triage_context.evidence_ids,
            output_message_ids=[item.message_id for item in triage_run.output.messages],
            usage=triage_run.usage,
            detail_codes=[triage_run.output.decision],
        )
        finding_message = next(
            (item for item in triage_run.output.messages if item.message_type == MessageType.FINDING),
            None,
        )
        if finding_message is None or not isinstance(finding_message.payload, FindingPayload):
            raise ValueError("live Triage did not produce a FINDING hypothesis")
        finding_payload = finding_message.payload
        proof_table = ProofTable(triage_run.output.obligations)

        # INVESTIGATOR: exactly one LLM call. A context request is served locally,
        # but never triggers a fifth role call for this Batch.
        transition(AgentName.INVESTIGATOR, "INVESTIGATION")
        investigator_context = self._context(
            agent=AgentName.INVESTIGATOR,
            batch=batch,
            phase=0,
            message_ids=bus.ids(),
            evidence_ids=blackboard.ids(),
            available_chunk_ids=available,
            governor=governor,
        )
        self.source_token_transmissions += self._estimated_source_tokens(
            investigator_context.evidence_ids, blackboard
        )
        self.logical_calls += 1
        investigator_run = agents[AgentName.INVESTIGATOR].run(
            InvestigatorInput(
                context=investigator_context,
                obligations=proof_table.values(),
                finding_message_id=finding_message.message_id,
            )
        )
        self._publish(
            bus=bus,
            logger=logger,
            messages=investigator_run.output.messages,
            prompt_version=investigator_run.prompt_version,
            state=state,
        )
        investigator_message_ids = [
            item.message_id for item in investigator_run.output.messages
        ]
        proof_table.apply(investigator_run.output.obligation_updates)
        unresolved = list(investigator_run.output.unresolved_obligation_ids)
        logger.record(
            event_type="AGENT_CALL",
            agent=AgentName.INVESTIGATOR,
            from_state=state,
            to_state=state,
            prompt_version=investigator_run.prompt_version,
            message_type=(
                investigator_run.output.messages[0].message_type
                if investigator_run.output.messages
                else None
            ),
            input_evidence_ids=investigator_context.evidence_ids,
            output_message_ids=investigator_message_ids,
            usage=investigator_run.usage,
            detail_codes=["PHASE_0"],
        )
        request = next(
            (
                item
                for item in investigator_run.output.messages
                if item.message_type == MessageType.REQUEST_CONTEXT
            ),
            None,
        )
        if request is not None:
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
                    detail_codes=[payload.request_kind.value, "NO_EXTRA_LLM_CALL"],
                )
            except (TokenBudgetExceeded, KeyError, ValueError) as error:
                logger.record(
                    event_type="CONTEXT_DENIED",
                    agent=AgentName.INVESTIGATOR,
                    from_state=state,
                    to_state=state,
                    message_type=MessageType.REQUEST_CONTEXT,
                    output_message_ids=[request.message_id],
                    detail_codes=[error.__class__.__name__],
                )

        # CHALLENGER
        transition(AgentName.CHALLENGER, "CHALLENGE")
        challenger_context = self._context(
            agent=AgentName.CHALLENGER,
            batch=batch,
            phase=0,
            message_ids=bus.ids(),
            evidence_ids=blackboard.ids(),
            available_chunk_ids=available,
            governor=governor,
        )
        self.source_token_transmissions += self._estimated_source_tokens(
            challenger_context.evidence_ids, blackboard
        )
        self.logical_calls += 1
        challenger_run = agents[AgentName.CHALLENGER].run(
            ChallengerInput(
                context=challenger_context,
                obligations=proof_table.values(),
                finding_id=finding_payload.finding_id,
                investigator_message_ids=investigator_message_ids,
            )
        )
        self._publish(
            bus=bus,
            logger=logger,
            messages=challenger_run.output.messages,
            prompt_version=challenger_run.prompt_version,
            state=state,
        )
        proof_table.apply(challenger_run.output.obligation_updates)
        logger.record(
            event_type="AGENT_CALL",
            agent=AgentName.CHALLENGER,
            from_state=state,
            to_state=state,
            prompt_version=challenger_run.prompt_version,
            message_type=(
                challenger_run.output.messages[0].message_type
                if challenger_run.output.messages
                else None
            ),
            input_evidence_ids=challenger_context.evidence_ids,
            output_message_ids=[item.message_id for item in challenger_run.output.messages],
            usage=challenger_run.usage,
            detail_codes=[
                "CONTRADICTION_FOUND"
                if challenger_run.output.contradiction_found
                else "NO_CONTRADICTION"
            ],
        )

        # JUDGE: metadata/proof packet only; no source is materialized by context source.
        transition(AgentName.JUDGE, "JUDGMENT")
        contradiction_ids = [
            message_id
            for message_id in bus.ids()
            if bus.get(message_id).message_type == MessageType.CONTRADICTION
        ]
        judge_context = self._context(
            agent=AgentName.JUDGE,
            batch=batch,
            phase=0,
            message_ids=bus.ids(),
            evidence_ids=blackboard.ids(),
            available_chunk_ids=[],
            governor=governor,
        )
        judge_input = JudgeInput(
            context=judge_context,
            obligations=proof_table.values(),
            unresolved_obligation_ids=unresolved,
            finding_message_id=finding_message.message_id,
            contradiction_message_ids=contradiction_ids,
        )
        enforced = JudgeAgent.enforced_verdict(judge_input, governor)
        self.logical_calls += 1
        kernel_override = False
        try:
            judge_run = agents[AgentName.JUDGE].run(judge_input)
            final_verdict = judge_run.output.verdict
            rationale_code = judge_run.output.rationale_code
            judge_usage = judge_run.usage
            prompt_version = judge_run.prompt_version
        except ValueError as error:
            if "violates enforced rule" not in str(error):
                raise
            kernel_override = True
            final_verdict = enforced
            rationale_code = "DETERMINISTIC_PROOF_KERNEL_OVERRIDE"
            last = self.tracker.records[-1]
            judge_usage = type(triage_run.usage)(
                input_tokens=last.input_tokens,
                output_tokens=last.output_tokens,
                cached_tokens=last.cached_tokens,
                reasoning_tokens=last.reasoning_tokens,
            )
            governor.charge_call(AgentName.JUDGE, judge_usage)
            prompt_version = agents[AgentName.JUDGE].prompt_version
        logger.record(
            event_type="AGENT_CALL",
            agent=AgentName.JUDGE,
            from_state=state,
            to_state=state,
            prompt_version=prompt_version,
            input_evidence_ids=judge_context.evidence_ids,
            usage=judge_usage,
            detail_codes=[final_verdict.value, rationale_code],
        )
        transition(AgentName.JUDGE, "COMPLETE")
        blackboard.verify_all()

        required = [item for item in proof_table.values() if item.required]
        evidence = [
            {
                "evidence_id": blackboard.get(evidence_id).evidence_id,
                "chunk_id": blackboard.get(evidence_id).chunk_id,
                "path": blackboard.get(evidence_id).path,
                "start_line": blackboard.get(evidence_id).start_line,
                "end_line": blackboard.get(evidence_id).end_line,
                "content_hash": blackboard.get(evidence_id).content_hash,
            }
            for evidence_id in blackboard.ids()
        ]
        contradictions = [
            bus.get(message_id).model_dump(mode="json")
            for message_id in contradiction_ids
        ]
        result = {
            "batch_id": batch_id,
            "batch_order": order,
            "focus_path": batch["focus_path"],
            "focus_symbol": batch["focus_symbol"],
            "risk_tags": batch["risk_tags"],
            "verdict": final_verdict.value,
            "rationale_code": rationale_code,
            "kernel_override": kernel_override,
            "finding": finding_payload.model_dump(mode="json"),
            "contradictions": contradictions,
            "proof_obligations": [item.model_dump(mode="json") for item in proof_table.values()],
            "required_proof_counts": {
                "required": len(required),
                "supported": sum(item.status == ProofStatus.SUPPORTED for item in required),
                "refuted": sum(item.status == ProofStatus.REFUTED for item in required),
                "unknown": sum(item.status == ProofStatus.UNKNOWN for item in required),
            },
            "unresolved_obligation_ids": unresolved,
            "evidence": evidence,
            "agent_call_counts": {
                agent.value: sum(
                    event["event_type"] == "AGENT_CALL" and event["agent"] == agent.value
                    for event in logger.events
                )
                for agent in AgentName
            },
            "agent_order": [agent.value for agent in AgentName],
            "token_governor": governor.snapshot(),
        }
        self.all_events.extend(logger.events)
        return result

    def _run_batch(self, batch_id: str, order: int) -> dict[str, Any]:
        """Run every role once and fail closed without aborting the role chain."""

        batch = self.batches[batch_id]
        governor = self._live_governor()
        blackboard = EvidenceBlackboard(self.target)
        bus = StructuredMessageBus(blackboard, set(self.retriever.chunks))
        logger = EventLogger(self.run_id, batch_id)
        provider = self._provider(blackboard, governor, bus)
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
        del focus_packet.source
        state = "CREATED"
        failure_codes: list[str] = []
        forced_unknown_ids: set[str] = set()
        unresolved: list[str] = []
        investigator_message_ids: list[str] = []
        contradiction_ids: list[str] = []
        synthetic_finding = False

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

        def record_failure(agent: AgentName, error: Exception, evidence_ids: list[str]) -> None:
            code = (
                error.code.value
                if isinstance(error, ProviderInconclusive)
                else error.__class__.__name__
            )
            failure_codes.append(f"{agent.value}:{code}")
            logger.record(
                event_type="AGENT_CALL",
                agent=agent,
                from_state=state,
                to_state=state,
                prompt_version=agents[agent].prompt_version,
                input_evidence_ids=evidence_ids,
                detail_codes=["FAIL_SAFE", code],
            )

        def fallback_packet(reason: str) -> tuple[FindingPayload, StructuredMessage, ProofTable]:
            nonlocal synthetic_finding
            synthetic_finding = True
            finding_id = stable_id(
                "FND",
                {"batch": batch_id, "kind": "FAIL_SAFE_UNKNOWN"},
            )
            obligation_id = stable_id(
                "OBL",
                {"batch": batch_id, "kind": "UPSTREAM_FAILURE"},
            )
            forced_unknown_ids.add(obligation_id)
            obligation = ProofObligation(
                obligation_id=obligation_id,
                description_code="UPSTREAM_AGENT_FAILURE_REQUIRES_REVIEW",
                required=True,
                status=ProofStatus.UNKNOWN,
            )
            finding = FindingPayload(
                finding_id=finding_id,
                hypothesis_code="FAIL_SAFE_UNKNOWN_HYPOTHESIS",
                severity="LOW",
                obligation_ids=[obligation_id],
            )
            message = StructuredMessage(
                message_id=stable_id(
                    "MSG", {"batch": batch_id, "kind": "FAIL_SAFE_FINDING"}
                ),
                message_type=MessageType.FINDING,
                agent=AgentName.TRIAGE,
                batch_id=batch_id,
                evidence_ids=[focus_packet.evidence.evidence_id],
                payload=finding,
            )
            if message.message_id not in bus.ids():
                bus.publish(message)
                logger.record(
                    event_type="FAIL_SAFE_PACKET",
                    agent=AgentName.TRIAGE,
                    from_state=state,
                    to_state=state,
                    message_type=MessageType.FINDING,
                    input_evidence_ids=message.evidence_ids,
                    output_message_ids=[message.message_id],
                    detail_codes=[reason],
                )
            return finding, message, ProofTable([obligation])

        # TRIAGE: one logical Agent call, with provider-internal bounded retry.
        transition(AgentName.TRIAGE, "TRIAGE")
        provider.set_role_packet(
            AgentName.TRIAGE,
            {
                "focus_evidence_id": focus_packet.evidence.evidence_id,
                "security_sketch_code": "STAGE2_RESULT_BLIND_RISK_MANIFEST",
            },
        )
        triage_context = self._context(
            agent=AgentName.TRIAGE,
            batch=batch,
            phase=0,
            message_ids=[],
            evidence_ids=blackboard.ids(),
            available_chunk_ids=available,
            governor=governor,
        )
        self.source_token_transmissions += self._estimated_source_tokens(
            triage_context.evidence_ids, blackboard
        )
        self.logical_calls += 1
        try:
            triage_run = agents[AgentName.TRIAGE].run(
                TriageInput(
                    context=triage_context,
                    focus_evidence_id=focus_packet.evidence.evidence_id,
                    security_sketch_code="STAGE2_RESULT_BLIND_RISK_MANIFEST",
                )
            )
            self._publish(
                bus=bus,
                logger=logger,
                messages=triage_run.output.messages,
                prompt_version=triage_run.prompt_version,
                state=state,
            )
            finding_message = next(
                (
                    item
                    for item in triage_run.output.messages
                    if item.message_type == MessageType.FINDING
                    and isinstance(item.payload, FindingPayload)
                ),
                None,
            )
            if finding_message is None or not triage_run.output.obligations:
                raise ValueError("Triage omitted finding or proof obligations")
            finding_payload = finding_message.payload
            proof_table = ProofTable(triage_run.output.obligations)
            logger.record(
                event_type="AGENT_CALL",
                agent=AgentName.TRIAGE,
                from_state=state,
                to_state=state,
                prompt_version=triage_run.prompt_version,
                message_type=MessageType.FINDING,
                input_evidence_ids=triage_context.evidence_ids,
                output_message_ids=[item.message_id for item in triage_run.output.messages],
                usage=triage_run.usage,
                detail_codes=[triage_run.output.decision],
            )
        except (ProviderInconclusive, TokenBudgetExceeded, ValueError) as error:
            record_failure(AgentName.TRIAGE, error, triage_context.evidence_ids)
            finding_payload, finding_message, proof_table = fallback_packet(
                failure_codes[-1]
            )

        # INVESTIGATOR always runs once, even with a synthetic UNKNOWN packet.
        transition(AgentName.INVESTIGATOR, "INVESTIGATION")
        provider.set_role_packet(
            AgentName.INVESTIGATOR,
            {
                "finding_message_id": finding_message.message_id,
                "finding": finding_payload.model_dump(mode="json"),
                "proof_packet": [
                    item.model_dump(mode="json") for item in proof_table.values()
                ],
            },
        )
        investigator_context = self._context(
            agent=AgentName.INVESTIGATOR,
            batch=batch,
            phase=0,
            message_ids=bus.ids(),
            evidence_ids=blackboard.ids(),
            available_chunk_ids=available,
            governor=governor,
        )
        self.source_token_transmissions += self._estimated_source_tokens(
            investigator_context.evidence_ids, blackboard
        )
        self.logical_calls += 1
        try:
            investigator_run = agents[AgentName.INVESTIGATOR].run(
                InvestigatorInput(
                    context=investigator_context,
                    obligations=proof_table.values(),
                    finding_message_id=finding_message.message_id,
                )
            )
            self._publish(
                bus=bus,
                logger=logger,
                messages=investigator_run.output.messages,
                prompt_version=investigator_run.prompt_version,
                state=state,
            )
            investigator_message_ids = [
                item.message_id for item in investigator_run.output.messages
            ]
            proof_table.apply(investigator_run.output.obligation_updates)
            unresolved = list(investigator_run.output.unresolved_obligation_ids)
            logger.record(
                event_type="AGENT_CALL",
                agent=AgentName.INVESTIGATOR,
                from_state=state,
                to_state=state,
                prompt_version=investigator_run.prompt_version,
                message_type=(
                    investigator_run.output.messages[0].message_type
                    if investigator_run.output.messages
                    else None
                ),
                input_evidence_ids=investigator_context.evidence_ids,
                output_message_ids=investigator_message_ids,
                usage=investigator_run.usage,
                detail_codes=["PHASE_0"],
            )
            request = next(
                (
                    item
                    for item in investigator_run.output.messages
                    if item.message_type == MessageType.REQUEST_CONTEXT
                ),
                None,
            )
            if request is not None:
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
                        detail_codes=[payload.request_kind.value, "NO_EXTRA_LLM_CALL"],
                    )
                except (TokenBudgetExceeded, KeyError, ValueError) as error:
                    unresolved.append(payload.obligation_id)
                    logger.record(
                        event_type="CONTEXT_DENIED",
                        agent=AgentName.INVESTIGATOR,
                        from_state=state,
                        to_state=state,
                        message_type=MessageType.REQUEST_CONTEXT,
                        output_message_ids=[request.message_id],
                        detail_codes=[error.__class__.__name__],
                    )
        except (ProviderInconclusive, TokenBudgetExceeded, ValueError) as error:
            record_failure(
                AgentName.INVESTIGATOR, error, investigator_context.evidence_ids
            )
            unresolved.extend(
                item.obligation_id for item in proof_table.values() if item.required
            )

        # CHALLENGER always executes once and cannot erase forced UNKNOWN state.
        transition(AgentName.CHALLENGER, "CHALLENGE")
        provider.set_role_packet(
            AgentName.CHALLENGER,
            {
                "finding": finding_payload.model_dump(mode="json"),
                "investigator_message_ids": investigator_message_ids,
                "proof_packet": [
                    item.model_dump(mode="json") for item in proof_table.values()
                ],
            },
        )
        challenger_context = self._context(
            agent=AgentName.CHALLENGER,
            batch=batch,
            phase=0,
            message_ids=bus.ids(),
            evidence_ids=blackboard.ids(),
            available_chunk_ids=available,
            governor=governor,
        )
        self.source_token_transmissions += self._estimated_source_tokens(
            challenger_context.evidence_ids, blackboard
        )
        self.logical_calls += 1
        try:
            challenger_run = agents[AgentName.CHALLENGER].run(
                ChallengerInput(
                    context=challenger_context,
                    obligations=proof_table.values(),
                    finding_id=finding_payload.finding_id,
                    investigator_message_ids=investigator_message_ids,
                )
            )
            self._publish(
                bus=bus,
                logger=logger,
                messages=challenger_run.output.messages,
                prompt_version=challenger_run.prompt_version,
                state=state,
            )
            proof_table.apply(challenger_run.output.obligation_updates)
            contradiction_ids = [
                item.message_id
                for item in challenger_run.output.messages
                if item.message_type == MessageType.CONTRADICTION
            ]
            logger.record(
                event_type="AGENT_CALL",
                agent=AgentName.CHALLENGER,
                from_state=state,
                to_state=state,
                prompt_version=challenger_run.prompt_version,
                message_type=(
                    challenger_run.output.messages[0].message_type
                    if challenger_run.output.messages
                    else None
                ),
                input_evidence_ids=challenger_context.evidence_ids,
                output_message_ids=[item.message_id for item in challenger_run.output.messages],
                usage=challenger_run.usage,
                detail_codes=[
                    "CONTRADICTION_FOUND"
                    if challenger_run.output.contradiction_found
                    else "NO_CONTRADICTION"
                ],
            )
        except (ProviderInconclusive, TokenBudgetExceeded, ValueError) as error:
            record_failure(AgentName.CHALLENGER, error, challenger_context.evidence_ids)

        # Restore fail-safe obligations to UNKNOWN after untrusted downstream updates.
        proof_values = [
            item.model_copy(update={"status": ProofStatus.UNKNOWN, "evidence_ids": []})
            if item.obligation_id in forced_unknown_ids
            else item
            for item in proof_table.values()
        ]
        proof_table = ProofTable(proof_values)
        unresolved = sorted(
            set(unresolved)
            | forced_unknown_ids
            | {
                item.obligation_id
                for item in proof_table.values()
                if item.required and item.status == ProofStatus.UNKNOWN
            }
        )

        # JUDGE always executes once. Provider failure/mismatch falls back to kernel.
        transition(AgentName.JUDGE, "JUDGMENT")
        provider.set_role_packet(
            AgentName.JUDGE,
            {
                "finding": finding_payload.model_dump(mode="json"),
                "contradiction_message_ids": contradiction_ids,
                "proof_packet": [
                    item.model_dump(mode="json") for item in proof_table.values()
                ],
                "unresolved_obligation_ids": unresolved,
            },
        )
        judge_context = self._context(
            agent=AgentName.JUDGE,
            batch=batch,
            phase=0,
            message_ids=bus.ids(),
            evidence_ids=blackboard.ids(),
            available_chunk_ids=[],
            governor=governor,
        )
        judge_input = JudgeInput(
            context=judge_context,
            obligations=proof_table.values(),
            unresolved_obligation_ids=unresolved,
            finding_message_id=finding_message.message_id,
            contradiction_message_ids=contradiction_ids,
        )
        enforced = JudgeAgent.enforced_verdict(judge_input, governor)
        self.logical_calls += 1
        kernel_override = False
        try:
            judge_run = agents[AgentName.JUDGE].run(judge_input)
            final_verdict = judge_run.output.verdict
            rationale_code = judge_run.output.rationale_code
            judge_usage = judge_run.usage
            judge_prompt_version = judge_run.prompt_version
        except (ProviderInconclusive, TokenBudgetExceeded, ValueError) as error:
            kernel_override = True
            record_failure(AgentName.JUDGE, error, judge_context.evidence_ids)
            final_verdict = Verdict.INCONCLUSIVE if failure_codes else enforced
            rationale_code = "DETERMINISTIC_FAIL_SAFE_KERNEL"
            judge_usage = None
            judge_prompt_version = agents[AgentName.JUDGE].prompt_version
        else:
            logger.record(
                event_type="AGENT_CALL",
                agent=AgentName.JUDGE,
                from_state=state,
                to_state=state,
                prompt_version=judge_prompt_version,
                input_evidence_ids=judge_context.evidence_ids,
                usage=judge_usage,
                detail_codes=[final_verdict.value, rationale_code],
            )
        if failure_codes:
            final_verdict = Verdict.INCONCLUSIVE
            rationale_code = "UPSTREAM_FAILURE_UNKNOWN_PROOF"
        transition(AgentName.JUDGE, "COMPLETE")
        blackboard.verify_all()

        required = [item for item in proof_table.values() if item.required]
        evidence = [
            {
                "evidence_id": blackboard.get(evidence_id).evidence_id,
                "chunk_id": blackboard.get(evidence_id).chunk_id,
                "path": blackboard.get(evidence_id).path,
                "start_line": blackboard.get(evidence_id).start_line,
                "end_line": blackboard.get(evidence_id).end_line,
                "start_byte": blackboard.get(evidence_id).start_byte,
                "end_byte": blackboard.get(evidence_id).end_byte,
                "content_hash": blackboard.get(evidence_id).content_hash,
            }
            for evidence_id in blackboard.ids()
        ]
        contradictions = [
            bus.get(message_id).model_dump(mode="json")
            for message_id in contradiction_ids
        ]
        result = {
            "batch_id": batch_id,
            "batch_order": order,
            "focus_path": batch["focus_path"],
            "focus_symbol": batch["focus_symbol"],
            "risk_tags": batch["risk_tags"],
            "verdict": final_verdict.value,
            "rationale_code": rationale_code,
            "kernel_override": kernel_override,
            "synthetic_fail_safe_finding": synthetic_finding,
            "failure_codes": failure_codes,
            "finding": finding_payload.model_dump(mode="json"),
            "contradictions": contradictions,
            "proof_obligations": [item.model_dump(mode="json") for item in proof_table.values()],
            "required_proof_counts": {
                "required": len(required),
                "supported": sum(item.status == ProofStatus.SUPPORTED for item in required),
                "refuted": sum(item.status == ProofStatus.REFUTED for item in required),
                "unknown": sum(item.status == ProofStatus.UNKNOWN for item in required),
            },
            "unresolved_obligation_ids": unresolved,
            "evidence": evidence,
            "agent_call_counts": {
                agent.value: sum(
                    event["event_type"] == "AGENT_CALL" and event["agent"] == agent.value
                    for event in logger.events
                )
                for agent in AgentName
            },
            "agent_order": [agent.value for agent in AgentName],
            "token_governor": governor.snapshot(),
        }
        self.all_events.extend(logger.events)
        return result

    def _write_events(self) -> None:
        normalized = []
        for sequence, event in enumerate(self.all_events, 1):
            item = dict(event)
            item["event_id"] = f"EVT-{sequence:05d}"
            item["sequence"] = sequence
            normalized.append(item)
        (self.run_dir / "events.jsonl").write_text(
            "".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
                for item in normalized
            ),
            encoding="utf-8",
        )

    def _savings(self) -> dict[str, Any]:
        repository_source_tokens = sum(
            int(item["estimated_tokens"]) for item in self.retriever.chunks.values()
        )
        selected_source_tokens = sum(
            int(self.batches[batch_id]["source_token_estimate"])
            for batch_id in self.selection["selected_batch_ids"]
        )
        actual_input = sum(item.input_tokens for item in self.tracker.records)
        cached_calls = sum(item.status == "CACHE_HIT" for item in self.tracker.records)
        repository_four_agent_baseline = repository_source_tokens * 4
        selected_four_agent_baseline = selected_source_tokens * 4

        def reduction(baseline: int, actual: int) -> float:
            if baseline <= 0:
                return 0.0
            return round((1 - actual / baseline) * 100, 4)

        return {
            "schema_version": "token-savings-report-v1",
            "methodology": {
                "repository_direct_baseline": "all function chunks sent once to each of four agents",
                "selected_batch_baseline": "all selected Batch source sent once to each of four agents",
                "actual_api_input": "provider-reported input tokens including prompts and schemas",
                "actual_source_transmission": "lexical estimate of only verified source materialized for non-Judge calls",
            },
            "repository_source_token_estimate": repository_source_tokens,
            "repository_four_agent_baseline_tokens": repository_four_agent_baseline,
            "selected_batch_source_token_estimate": selected_source_tokens,
            "selected_four_agent_baseline_tokens": selected_four_agent_baseline,
            "actual_api_input_tokens": actual_input,
            "actual_source_token_estimate_transmitted": self.source_token_transmissions,
            "repository_baseline_reduction_percent": reduction(
                repository_four_agent_baseline, actual_input
            ),
            "selected_code_only_reduction_percent": reduction(
                selected_four_agent_baseline, self.source_token_transmissions
            ),
            "content_hash_cache_hits": cached_calls,
            "model_calls_skipped_by_cache": cached_calls,
            "logical_agent_calls": self.logical_calls,
            "actual_api_attempts": self.prior_api_attempts + self.transport.calls_started,
            "current_run_api_attempts": self.transport.calls_started,
            "prior_api_attempts": self.prior_api_attempts,
            "retry_attempts": sum(item.retry for item in self.tracker.records),
        }

    def _security_report(self, summary: dict[str, Any], savings: dict[str, Any]) -> str:
        lines = [
            "# Gemini Multi-Agent SAST Security Report",
            "",
            f"- Run ID: `{self.run_id}`",
            f"- Selection hash: `{self.selection['selection_hash']}`",
            f"- Provider/model: `gemini-generate-content` / `gemini-3.6-flash`",
            f"- API attempts: {summary['api_attempts']}",
            f"- Repository baseline reduction: {savings['repository_baseline_reduction_percent']}%",
            "",
            "## Batch results",
            "",
        ]
        for item in self.batch_results:
            counts = item["required_proof_counts"]
            lines.extend(
                [
                    f"### {item['batch_id']} — {item['verdict']}",
                    "",
                    f"- Focus: `{item['focus_path']}::{item['focus_symbol']}`",
                    f"- Hypothesis: `{item['finding']['hypothesis_code']}` ({item['finding']['severity']})",
                    f"- Judge rationale: `{item['rationale_code']}`",
                    f"- Required proof: supported={counts['supported']}, refuted={counts['refuted']}, unknown={counts['unknown']}",
                    f"- Challenger contradictions: {len(item['contradictions'])}",
                    "- Evidence:",
                ]
            )
            for evidence in item["evidence"]:
                lines.append(
                    f"  - `{evidence['evidence_id']}` — `{evidence['path']}:{evidence['start_line']}`"
                )
            lines.append("")
        lines.extend(
            [
                "## Safety notes",
                "",
                "- Verdicts are enforced by the deterministic proof-obligation kernel.",
                "- UNKNOWN proof or exhausted budget forces INCONCLUSIVE.",
                "- TargetCode remained read-only; source and API keys are excluded from artifacts.",
                "- Free-tier content handling is governed by the Gemini project policy.",
                "",
            ]
        )
        return "\n".join(lines)

    def run(self) -> dict[str, Any]:
        clean_before = _target_clean(self.target)
        preflight = self.preflight()
        if not preflight["passed"]:
            raise RuntimeError("Gemini live preflight failed")
        errors: list[dict[str, str]] = []
        for order, batch_id in enumerate(self.selection["selected_batch_ids"], 1):
            try:
                result = self._run_batch(batch_id, order)
            except (ProviderInconclusive, TokenBudgetExceeded, ValueError) as error:
                result = {
                    "batch_id": batch_id,
                    "batch_order": order,
                    "focus_path": self.batches[batch_id]["focus_path"],
                    "focus_symbol": self.batches[batch_id]["focus_symbol"],
                    "verdict": Verdict.INCONCLUSIVE.value,
                    "rationale_code": error.__class__.__name__,
                    "finding": None,
                    "contradictions": [],
                    "proof_obligations": [],
                    "required_proof_counts": {
                        "required": 0,
                        "supported": 0,
                        "refuted": 0,
                        "unknown": 0,
                    },
                    "unresolved_obligation_ids": [],
                    "evidence": [],
                    "agent_call_counts": {agent.value: 0 for agent in AgentName},
                    "agent_order": [agent.value for agent in AgentName],
                    "token_governor": {},
                }
                errors.append({"batch_id": batch_id, "error_code": error.__class__.__name__})
            self.batch_results.append(result)
            _write_json(self.run_dir / "batch-results.json", self.batch_results)
            self._write_events()

        clean_after = _target_clean(self.target)
        selection_unchanged = self.selection_path.read_bytes() == self.selection_bytes
        savings = self._savings()
        _write_json(self.run_dir / "token-savings-report.json", savings)
        summary = {
            "schema_version": "gemini-live-run-summary-v1",
            "orchestrator_version": self.version,
            "run_id": self.run_id,
            "provider": self.router.provider,
            "model": "gemini-3.6-flash",
            "selection_hash": self.selection["selection_hash"],
            "selected_batch_ids": self.selection["selected_batch_ids"],
            "verdicts": {
                item["batch_id"]: item["verdict"] for item in self.batch_results
            },
            "agent_call_counts": {
                agent.value: sum(
                    item["agent_call_counts"].get(agent.value, 0)
                    for item in self.batch_results
                )
                for agent in AgentName
            },
            "logical_agent_calls": self.logical_calls,
            "api_attempts": self.prior_api_attempts + self.transport.calls_started,
            "api_attempts_current_run": self.transport.calls_started,
            "api_attempts_prior_run": self.prior_api_attempts,
            "api_attempt_cap": self.transport.max_calls,
            "retry_attempts": sum(item.retry for item in self.tracker.records),
            "cache_hits": sum(item.status == "CACHE_HIT" for item in self.tracker.records),
            "errors": errors,
            "target_clean_before": clean_before,
            "target_clean_after": clean_after,
            "selection_unchanged": selection_unchanged,
            "continuation_of": self.continuation_of,
            "actual_three_batch_analysis": True,
            "additional_analysis_performed": False,
        }
        _write_json(self.run_dir / "run-summary.json", summary)
        (self.run_dir / "security-report.md").write_text(
            self._security_report(summary, savings), encoding="utf-8"
        )
        return summary
