from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..providers.model_router import ModelRouter
from ..stage3_schemas import AgentName
from .live_gemini_analysis import LiveGeminiAnalysis, _target_clean, _write_json


class RecoveryGeminiAnalysis(LiveGeminiAnalysis):
    """Approved recovery routing over the unchanged Multi-Agent pipeline."""

    version = "gemini-recovery-entrypoint-v1"
    profile_name = "gemini-recovery"
    approved_model = "gemini-3.5-flash-lite"
    smoke_model = approved_model

    def __init__(self, workspace: Path, **kwargs: Any):
        kwargs.setdefault(
            "run_id",
            "gemini-recovery-3batch-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        )
        kwargs.setdefault("max_api_calls", 19)
        kwargs.setdefault("min_start_interval_seconds", 4.1)
        super().__init__(workspace, **kwargs)

        # Replace routing metadata before an Agent or Provider is constructed.
        # The inherited Agent classes, prompts, schemas, Orchestrator, Evidence
        # machinery, cache and deterministic Judge kernel remain unchanged.
        self.router = ModelRouter(
            self.workspace / "config" / "model-routing.json",
            profile=self.profile_name,
        )
        if self.router.provider != "gemini-generate-content":
            raise ValueError("recovery profile selected a non-Gemini provider")
        if any(
            self.router.route(agent).model != self.approved_model
            for agent in AgentName
        ):
            raise ValueError("recovery profile selected an unapproved model")

    def _successful_smoke(self) -> tuple[str | None, bool]:
        candidates = sorted(
            (self.artifacts / "runs").glob("gemini-recovery-smoke-*/smoke-result.json"),
            reverse=True,
        )
        for path in candidates:
            result = json.loads(path.read_text(encoding="utf-8"))
            valid = (
                result.get("success") is True
                and result.get("model") == self.smoke_model
                and result.get("agent_order")
                == [agent.value for agent in AgentName]
                and result.get("logical_agent_calls") == 4
                and result.get("api_attempts") == 4
                and result.get("retry_attempts") == 0
                and result.get("failure_codes") == []
                and result.get("actual_three_batch_analysis") is False
            )
            if valid:
                return result.get("run_id"), True
        return None, False

    def preflight(self) -> dict[str, Any]:
        smoke_run_id, smoke_success = self._successful_smoke()
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
                        self.retriever.chunks[batch["focus_chunk_id"]][
                            "estimated_tokens"
                        ]
                    ),
                    "risk_tags": batch["risk_tags"],
                }
            )
        checks = {
            "gemini_api_key_present": bool(os.environ.get("GEMINI_API_KEY"))
            or self.offline_test,
            "recovery_profile_selected": self.router.profile_name
            == self.profile_name,
            "approved_model_only": all(
                self.router.route(agent).model == self.approved_model
                for agent in AgentName
            ),
            "exactly_three_fixed_batches": len(
                self.selection["selected_batch_ids"]
            )
            == 3,
            "selection_result_blind": self.selection["result_blind"] is True,
            "target_clean": _target_clean(self.target),
            "four_agent_smoke_success": smoke_success or self.offline_test,
            "base_calls_fit_attempt_cap": max(
                0, 12 - self.prior_api_attempts
            )
            <= self.transport.max_calls,
            "rpm_interval_enforced": self.transport.min_start_interval_seconds
            >= 4.1
            or self.offline_test,
        }
        result = {
            "schema_version": "gemini-recovery-preflight-v1",
            "passed": all(checks.values()),
            "checks": checks,
            "profile": self.profile_name,
            "model": self.approved_model,
            "smoke_run_id": smoke_run_id,
            "selection_hash": self.selection["selection_hash"],
            "selected_batch_ids": self.selection["selected_batch_ids"],
            "agent_order": [agent.value for agent in AgentName],
            "batch_estimates": batch_estimates,
            "base_logical_calls": 12,
            "base_api_calls_remaining_after_continuation": max(
                0, 12 - self.prior_api_attempts
            ),
            "global_api_attempt_cap": self.transport.max_calls,
            "max_extra_attempts": self.transport.max_calls
            - max(0, 12 - self.prior_api_attempts),
            "retry_policy": {
                "per_role_max_extra_attempts": 1,
                "transient_errors_only": [
                    "TIMEOUT",
                    "RATE_LIMIT",
                    "CONNECTION",
                    "SERVER",
                ],
                "deterministic_errors_not_retried": [
                    "OUTPUT_MISSING",
                    "MAX_TOKENS",
                    "JSON_INVALID",
                    "WIRE_SCHEMA_INVALID",
                    "DOMAIN_RULE_INVALID",
                ],
            },
            "min_call_start_interval_seconds": (
                self.transport.min_start_interval_seconds
            ),
            "network_calls_so_far": self.prior_api_attempts,
            "continuation_of": self.continuation_of,
        }
        _write_json(self.run_dir / "preflight.json", result)
        return result

    def _security_report(
        self, summary: dict[str, Any], savings: dict[str, Any]
    ) -> str:
        return super()._security_report(summary, savings).replace(
            "gemini-3.6-flash", self.approved_model
        )

    def run(self) -> dict[str, Any]:
        summary = super().run()
        summary["profile"] = self.profile_name
        summary["model"] = self.approved_model
        _write_json(self.run_dir / "run-summary.json", summary)
        savings = json.loads(
            (self.run_dir / "token-savings-report.json").read_text(encoding="utf-8")
        )
        (self.run_dir / "security-report.md").write_text(
            self._security_report(summary, savings), encoding="utf-8"
        )
        return summary
