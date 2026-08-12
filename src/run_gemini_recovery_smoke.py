from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from refine_sast.agents import ChallengerAgent, InvestigatorAgent, JudgeAgent, TriageAgent
from refine_sast.hashing import content_hash, stable_id
from refine_sast.providers.gemini_provider import GeminiProvider
from refine_sast.providers.gemini_transport import GeminiTransport, RateLimitedGeminiClient
from refine_sast.providers.local_context import VerifiedLocalContextSource
from refine_sast.providers.model_router import ModelRouter
from refine_sast.providers.retry import ProviderInconclusive, RetryPolicy
from refine_sast.providers.usage_tracker import UsageTracker
from refine_sast.runtime.evidence import EvidenceBlackboard, StructuredMessageBus
from refine_sast.runtime.live_gemini_analysis import _target_clean, _write_json
from refine_sast.runtime.proofs import ProofTable
from refine_sast.runtime.token_governor import TokenBudgetExceeded, TokenGovernor
from refine_sast.stage3_schemas import (
    AgentContext,
    AgentName,
    ChallengerInput,
    FindingPayload,
    InvestigatorInput,
    JudgeInput,
    MessageType,
    ProofObligation,
    ProofStatus,
    StructuredMessage,
    TriageInput,
    Verdict,
)


PROFILE = "gemini-recovery-smoke"
MODEL = "gemini-3.5-flash-lite"
SYNTHETIC_BATCH = "BAT-SMOKE-RECOVERY-NO-TARGET"
SYNTHETIC_SOURCE = (
    "#include <string.h>\n"
    "int copy_name(char *input) {\n"
    "    char buffer[8];\n"
    "    strcpy(buffer, input);\n"
    "    return buffer[0];\n"
    "}"
)


class SyntheticRetriever:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def materialize_evidence(self, record: Any) -> bytes:
        data = (self.root / record.path).read_bytes()
        fragment = data[record.start_byte : record.end_byte]
        if content_hash(fragment) != record.content_hash:
            raise ValueError("synthetic Evidence content changed")
        return fragment


def _context(
    agent: AgentName,
    *,
    evidence_ids: list[str],
    message_ids: list[str],
    chunk_id: str,
    governor: TokenGovernor,
) -> AgentContext:
    return AgentContext(
        agent=agent,
        batch_id=SYNTHETIC_BATCH,
        scenario=Verdict.INCONCLUSIVE,
        input_message_ids=message_ids,
        evidence_ids=evidence_ids,
        available_chunk_ids=[chunk_id],
        risk_tags=["SYNTHETIC_UNBOUNDED_STRING_COPY"],
        budget_snapshot=governor.snapshot(),
    )


def _fallback(
    *, blackboard: EvidenceBlackboard, bus: StructuredMessageBus, evidence_id: str
) -> tuple[FindingPayload, StructuredMessage, ProofTable]:
    finding_id = stable_id("FND", {"batch": SYNTHETIC_BATCH, "fallback": True})
    obligation = ProofObligation(
        obligation_id=stable_id("OBL", {"finding": finding_id, "fallback": True}),
        description_code="SMOKE_UPSTREAM_FAILURE_REQUIRES_REVIEW",
        status=ProofStatus.UNKNOWN,
    )
    finding = FindingPayload(
        finding_id=finding_id,
        hypothesis_code="SMOKE_FAIL_SAFE_UNKNOWN",
        severity="LOW",
        obligation_ids=[obligation.obligation_id],
    )
    message = StructuredMessage(
        message_id=stable_id("MSG", {"finding": finding_id}),
        message_type=MessageType.FINDING,
        agent=AgentName.TRIAGE,
        batch_id=SYNTHETIC_BATCH,
        evidence_ids=[evidence_id],
        payload=finding,
    )
    blackboard.verify_all()
    bus.publish(message)
    return finding, message, ProofTable([obligation])


def run_smoke(
    workspace: Path,
    *,
    transport: Any | None = None,
    run_id: str | None = None,
    output_root: Path | None = None,
    max_calls: int = 8,
    min_interval_seconds: float = 4.1,
    offline: bool = False,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    target = workspace / "target" / "userland"
    clean_before = _target_clean(target)
    key_present = bool(os.environ.get("GEMINI_API_KEY"))
    router = ModelRouter(workspace / "config" / "model-routing.json", profile=PROFILE)
    checks = {
        "gemini_api_key_present": key_present or offline,
        "profile_selected": router.profile_name == PROFILE,
        "model_only": all(router.route(agent).model == MODEL for agent in AgentName),
        "target_clean": clean_before,
        "synthetic_batch_only": SYNTHETIC_BATCH.startswith("BAT-SMOKE-"),
        "call_cap": max_calls == 8,
        "rpm_interval": min_interval_seconds >= 4.0 or offline,
    }
    if not all(checks.values()):
        raise RuntimeError("Gemini recovery smoke preflight failed")
    if transport is None:
        transport = GeminiTransport(
            router.profile.endpoint or "", os.environ["GEMINI_API_KEY"]
        )
    guarded = RateLimitedGeminiClient(
        transport,
        max_calls=max_calls,
        min_start_interval_seconds=min_interval_seconds,
    )
    run_id = run_id or (
        "gemini-recovery-smoke-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    run_dir = (output_root or (workspace / "artifacts" / "runs")) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    tracker = UsageTracker(run_id, run_dir / "token-ledger.json")
    _write_json(
        run_dir / "preflight.json",
        {
            "schema_version": "gemini-recovery-smoke-preflight-v1",
            "passed": True,
            "checks": checks,
            "profile": PROFILE,
            "model": MODEL,
            "base_calls": 4,
            "call_cap": max_calls,
            "target_code_used": False,
            "fixed_batches_used": False,
        },
    )

    with tempfile.TemporaryDirectory() as temporary:
        synthetic_root = Path(temporary)
        source_path = synthetic_root / "synthetic" / "smoke.c"
        source_path.parent.mkdir(parents=True)
        source_path.write_text(SYNTHETIC_SOURCE, encoding="utf-8")
        data = source_path.read_bytes()
        chunk_id = stable_id("CHK", {"kind": "recovery-smoke", "hash": content_hash(data)})
        blackboard = EvidenceBlackboard(synthetic_root)
        record = blackboard.register(
            chunk_id=chunk_id,
            path="synthetic/smoke.c",
            start_line=1,
            end_line=6,
            start_byte=0,
            end_byte=len(data),
            expected_hash=content_hash(data),
            evidence_kind="synthetic-smoke",
        )
        bus = StructuredMessageBus(blackboard, {chunk_id})
        source = VerifiedLocalContextSource(
            blackboard, SyntheticRetriever(synthetic_root), message_bus=bus
        )
        governor = TokenGovernor()
        provider = GeminiProvider(
            workspace=workspace,
            router=router,
            tracker=tracker,
            context_source=source,
            transport=guarded,
            retry_policy=RetryPolicy(max_attempts=2, jitter_ratio=0),
        )
        agents = {
            AgentName.TRIAGE: TriageAgent(provider, governor, workspace),
            AgentName.INVESTIGATOR: InvestigatorAgent(provider, governor, workspace),
            AgentName.CHALLENGER: ChallengerAgent(provider, governor, workspace),
            AgentName.JUDGE: JudgeAgent(provider, governor, workspace),
        }
        role_results: list[dict[str, Any]] = []
        failure_codes: list[str] = []

        blackboard.verify_all()
        triage_context = _context(
            AgentName.TRIAGE,
            evidence_ids=[record.evidence_id],
            message_ids=[],
            chunk_id=chunk_id,
            governor=governor,
        )
        source.set_role_packet(
            AgentName.TRIAGE,
            {
                "focus_evidence_id": record.evidence_id,
                "security_sketch_code": "SYNTHETIC_UNBOUNDED_STRCPY",
            },
        )
        try:
            triage = agents[AgentName.TRIAGE].run(
                TriageInput(
                    context=triage_context,
                    focus_evidence_id=record.evidence_id,
                    security_sketch_code="SYNTHETIC_UNBOUNDED_STRCPY",
                )
            )
            for message in triage.output.messages:
                bus.publish(message)
            finding_message = next(
                message
                for message in triage.output.messages
                if message.message_type == MessageType.FINDING
            )
            finding = finding_message.payload
            proof_table = ProofTable(triage.output.obligations)
            role_results.append(
                {
                    "agent": AgentName.TRIAGE.value,
                    "success": True,
                    "decision": triage.output.decision,
                    "finding": finding.model_dump(mode="json"),
                    "obligations": [item.model_dump(mode="json") for item in proof_table.values()],
                }
            )
        except (ProviderInconclusive, TokenBudgetExceeded, ValueError) as error:
            code = getattr(getattr(error, "code", None), "value", error.__class__.__name__)
            failure_codes.append(f"TRIAGE:{code}")
            finding, finding_message, proof_table = _fallback(
                blackboard=blackboard, bus=bus, evidence_id=record.evidence_id
            )
            role_results.append({"agent": "TRIAGE", "success": False, "error_code": code})

        blackboard.verify_all()
        investigator_context = _context(
            AgentName.INVESTIGATOR,
            evidence_ids=[record.evidence_id],
            message_ids=bus.ids(),
            chunk_id=chunk_id,
            governor=governor,
        )
        source.set_role_packet(
            AgentName.INVESTIGATOR,
            {
                "finding": finding.model_dump(mode="json"),
                "proof_packet": [item.model_dump(mode="json") for item in proof_table.values()],
            },
        )
        try:
            investigator = agents[AgentName.INVESTIGATOR].run(
                InvestigatorInput(
                    context=investigator_context,
                    obligations=proof_table.values(),
                    finding_message_id=finding_message.message_id,
                )
            )
            for message in investigator.output.messages:
                bus.publish(message)
            proof_table.apply(investigator.output.obligation_updates)
            unresolved = list(investigator.output.unresolved_obligation_ids)
            investigator_ids = [item.message_id for item in investigator.output.messages]
            role_results.append(
                {
                    "agent": "INVESTIGATOR",
                    "success": True,
                    "messages": [item.model_dump(mode="json") for item in investigator.output.messages],
                    "proof_updates": [item.model_dump(mode="json") for item in investigator.output.obligation_updates],
                }
            )
        except (ProviderInconclusive, TokenBudgetExceeded, ValueError) as error:
            code = getattr(getattr(error, "code", None), "value", error.__class__.__name__)
            failure_codes.append(f"INVESTIGATOR:{code}")
            unresolved = [item.obligation_id for item in proof_table.values() if item.required]
            investigator_ids = []
            role_results.append({"agent": "INVESTIGATOR", "success": False, "error_code": code})

        blackboard.verify_all()
        challenger_context = _context(
            AgentName.CHALLENGER,
            evidence_ids=[record.evidence_id],
            message_ids=bus.ids(),
            chunk_id=chunk_id,
            governor=governor,
        )
        source.set_role_packet(
            AgentName.CHALLENGER,
            {
                "finding": finding.model_dump(mode="json"),
                "investigator_message_ids": investigator_ids,
                "proof_packet": [item.model_dump(mode="json") for item in proof_table.values()],
            },
        )
        try:
            challenger = agents[AgentName.CHALLENGER].run(
                ChallengerInput(
                    context=challenger_context,
                    obligations=proof_table.values(),
                    finding_id=finding.finding_id,
                    investigator_message_ids=investigator_ids,
                )
            )
            for message in challenger.output.messages:
                bus.publish(message)
            proof_table.apply(challenger.output.obligation_updates)
            contradiction_ids = [
                item.message_id
                for item in challenger.output.messages
                if item.message_type == MessageType.CONTRADICTION
            ]
            role_results.append(
                {
                    "agent": "CHALLENGER",
                    "success": True,
                    "contradiction_found": challenger.output.contradiction_found,
                    "messages": [item.model_dump(mode="json") for item in challenger.output.messages],
                }
            )
        except (ProviderInconclusive, TokenBudgetExceeded, ValueError) as error:
            code = getattr(getattr(error, "code", None), "value", error.__class__.__name__)
            failure_codes.append(f"CHALLENGER:{code}")
            contradiction_ids = []
            role_results.append({"agent": "CHALLENGER", "success": False, "error_code": code})

        unresolved = sorted(
            set(unresolved)
            | {
                item.obligation_id
                for item in proof_table.values()
                if item.required and item.status == ProofStatus.UNKNOWN
            }
        )
        blackboard.verify_all()
        judge_context = _context(
            AgentName.JUDGE,
            evidence_ids=[record.evidence_id],
            message_ids=bus.ids(),
            chunk_id=chunk_id,
            governor=governor,
        )
        source.set_role_packet(
            AgentName.JUDGE,
            {
                "finding": finding.model_dump(mode="json"),
                "contradiction_message_ids": contradiction_ids,
                "proof_packet": [item.model_dump(mode="json") for item in proof_table.values()],
                "unresolved_obligation_ids": unresolved,
            },
        )
        judge_input = JudgeInput(
            context=judge_context,
            obligations=proof_table.values(),
            unresolved_obligation_ids=unresolved,
            finding_message_id=finding_message.message_id,
            contradiction_message_ids=contradiction_ids,
        )
        enforced = JudgeAgent.enforced_verdict(judge_input, governor)
        try:
            judge = agents[AgentName.JUDGE].run(judge_input)
            role_results.append(
                {
                    "agent": "JUDGE",
                    "success": True,
                    "provider_verdict": judge.output.verdict.value,
                    "enforced_verdict": enforced.value,
                    "rationale_code": judge.output.rationale_code,
                }
            )
        except (ProviderInconclusive, TokenBudgetExceeded, ValueError) as error:
            code = getattr(getattr(error, "code", None), "value", error.__class__.__name__)
            failure_codes.append(f"JUDGE:{code}")
            role_results.append(
                {
                    "agent": "JUDGE",
                    "success": False,
                    "error_code": code,
                    "enforced_verdict": enforced.value,
                }
            )
        blackboard.verify_all()

    clean_after = _target_clean(target)
    calls = [item.model_dump(mode="json") for item in tracker.records]
    summary = {
        "schema_version": "gemini-recovery-smoke-result-v1",
        "run_id": run_id,
        "success": not failure_codes and len(role_results) == 4,
        "profile": PROFILE,
        "model": MODEL,
        "agent_order": [agent.value for agent in AgentName],
        "logical_agent_calls": 4,
        "api_attempts": guarded.calls_started,
        "api_attempt_cap": guarded.max_calls,
        "retry_attempts": sum(item.retry for item in tracker.records),
        "failure_codes": failure_codes,
        "role_results": role_results,
        "usage": {
            "input_tokens": sum(item.input_tokens for item in tracker.records),
            "output_tokens": sum(item.output_tokens for item in tracker.records),
            "reasoning_tokens": sum(item.reasoning_tokens for item in tracker.records),
            "cached_tokens": sum(item.cached_tokens for item in tracker.records),
        },
        "call_diagnostics": [
            {
                "agent": item["agent"],
                "status": item["status"],
                "finish_reason": item["finish_reason"],
                "input_tokens": item["input_tokens"],
                "output_tokens": item["output_tokens"],
                "reasoning_tokens": item["reasoning_tokens"],
                "cached_tokens": item["cached_tokens"],
                "latency_ms": item["latency_ms"],
                "validation_stage": item["validation_stage"],
                "error_code": item["error_code"],
                "retry": item["retry"],
            }
            for item in calls
        ],
        "target_code_used": False,
        "fixed_batches_used": False,
        "target_clean_before": clean_before,
        "target_clean_after": clean_after,
        "contains_api_key": False,
        "contains_source": False,
        "actual_three_batch_analysis": False,
    }
    _write_json(run_dir / "smoke-result.json", summary)
    return summary


def main() -> None:
    workspace = Path(__file__).resolve().parents[1]
    print(json.dumps(run_smoke(workspace), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
