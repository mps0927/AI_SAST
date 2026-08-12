from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from refine_sast.providers.gemini_transport import (
    GeminiCallBudgetExceededError,
    RateLimitedGeminiClient,
)
from refine_sast.providers.gemini_wire import domain_to_wire
from refine_sast.providers.mock_provider import MockProvider
from refine_sast.runtime.live_gemini_analysis import LiveGeminiAnalysis
from refine_sast.stage3_schemas import (
    AgentContext,
    AgentName,
    ProofObligation,
    Verdict,
)


WORKSPACE = Path(__file__).resolve().parents[1]


class FakeLiveGeminiTransport:
    def __init__(
        self, failures: dict[tuple[str, AgentName], str] | None = None
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any], float]] = []
        self.mock = MockProvider()
        self.failures = failures or {}
        self.attempts: dict[tuple[str, AgentName], int] = {}

    def generate_content(
        self, model: str, request: dict[str, Any], timeout_seconds: float
    ) -> dict[str, Any]:
        self.calls.append((model, request, timeout_seconds))
        payload = json.loads(request["contents"][0]["parts"][0]["text"])
        context = AgentContext.model_validate(
            {**payload["context"], "scenario": Verdict.CONFIRMED.value}
        )
        key = (context.batch_id, context.agent)
        self.attempts[key] = self.attempts.get(key, 0) + 1
        failure = self.failures.get(key)
        if failure == "timeout":
            raise TimeoutError("offline fake timeout")
        if failure == "schema":
            return {
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": "not-json"}]}}
                ],
                "usageMetadata": {},
                "modelVersion": "gemini-3.6-flash-fake",
            }
        obligations = [
            ProofObligation.model_validate(item)
            for item in payload.get("obligations", [])
        ]
        if context.agent == AgentName.TRIAGE:
            output = self.mock.triage(context).output
        elif context.agent == AgentName.INVESTIGATOR:
            output = self.mock.investigate(context, obligations).output
        elif context.agent == AgentName.CHALLENGER:
            output = self.mock.challenge(
                context, obligations, payload["finding_id"]
            ).output
        else:
            output = self.mock.judge(context, obligations).output
        return {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "text": json.dumps(
                                    domain_to_wire(output, context=context),
                                    ensure_ascii=False,
                                )
                            }
                        ],
                    }
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 300,
                "candidatesTokenCount": 100,
                "cachedContentTokenCount": 0,
                "thoughtsTokenCount": 10,
            },
            "modelVersion": "gemini-3.6-flash-fake",
        }


class RateGuardTests(unittest.TestCase):
    def test_spacing_and_global_call_cap(self) -> None:
        now = [0.0]
        sleeps: list[float] = []

        class Inner:
            def generate_content(self, model: str, request: dict, timeout: float) -> dict:
                return {"ok": True}

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        guard = RateLimitedGeminiClient(
            Inner(),
            max_calls=2,
            min_start_interval_seconds=12,
            clock=lambda: now[0],
            sleeper=sleep,
        )
        guard.generate_content("m", {}, 1)
        guard.generate_content("m", {}, 1)
        self.assertEqual(sleeps, [12])
        self.assertEqual(guard.calls_started, 2)
        with self.assertRaises(GeminiCallBudgetExceededError):
            guard.generate_content("m", {}, 1)


class LiveGeminiAnalysisTests(unittest.TestCase):
    def test_live_governor_allows_observed_gemini_schema_overhead(self) -> None:
        governor = LiveGeminiAnalysis._live_governor()
        self.assertEqual(governor.roles[AgentName.TRIAGE].input_limit, 3_500)
        self.assertEqual(governor.roles[AgentName.TRIAGE].output_limit, 400)
        self.assertGreaterEqual(
            governor.roles[AgentName.INVESTIGATOR].input_limit, 6_000
        )

    def test_three_batches_four_agents_and_safe_artifacts_offline(self) -> None:
        fake = FakeLiveGeminiTransport()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = LiveGeminiAnalysis(
                WORKSPACE,
                transport=fake,
                run_id="offline-full-flow",
                max_api_calls=12,
                min_start_interval_seconds=0,
                output_root=root / "runs",
                cache_path=root / "cache.json",
                offline_test=True,
            )
            summary = runner.run()
            run_dir = root / "runs" / "offline-full-flow"
            self.assertEqual(summary["api_attempts"], 12)
            self.assertEqual(summary["logical_agent_calls"], 12)
            self.assertTrue(summary["target_clean_before"])
            self.assertTrue(summary["target_clean_after"])
            self.assertTrue(summary["selection_unchanged"])
            self.assertEqual(len(runner.batch_results), 3)
            for result in runner.batch_results:
                self.assertEqual(result["verdict"], Verdict.CONFIRMED.value)
                self.assertEqual(
                    result["agent_call_counts"],
                    {agent.value: 1 for agent in AgentName},
                )
            expected = {
                "events.jsonl",
                "token-ledger.json",
                "batch-results.json",
                "run-summary.json",
                "token-savings-report.json",
                "security-report.md",
            }
            self.assertTrue(expected.issubset({item.name for item in run_dir.iterdir()}))
            ledger = json.loads((run_dir / "token-ledger.json").read_text("utf-8"))
            self.assertEqual(len(ledger["calls"]), 12)
            self.assertEqual({item["model"] for item in ledger["calls"]}, {"gemini-3.6-flash"})
            for index, (_, request, _) in enumerate(fake.calls):
                payload = json.loads(request["contents"][0]["parts"][0]["text"])
                evidence = payload["verified_context"]["verified_evidence"]
                if index % 4 == 3:
                    self.assertTrue(all("source" not in item for item in evidence))
                else:
                    self.assertTrue(any("source" in item for item in evidence))

    def test_failures_still_execute_exactly_four_roles_per_batch(self) -> None:
        selection = json.loads(
            (WORKSPACE / "artifacts" / "batches" / "selection.json").read_text(
                "utf-8"
            )
        )
        first, second, third = selection["selected_batch_ids"]
        fake = FakeLiveGeminiTransport(
            {
                (first, AgentName.INVESTIGATOR): "schema",
                (second, AgentName.TRIAGE): "timeout",
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = LiveGeminiAnalysis(
                WORKSPACE,
                transport=fake,
                run_id="offline-fail-safe-flow",
                max_api_calls=20,
                min_start_interval_seconds=0,
                output_root=root / "runs",
                cache_path=root / "cache.json",
                offline_test=True,
            )
            summary = runner.run()
            events = [
                json.loads(line)
                for line in (root / "runs" / "offline-fail-safe-flow" / "events.jsonl")
                .read_text("utf-8")
                .splitlines()
            ]

        self.assertEqual(summary["logical_agent_calls"], 12)
        self.assertEqual(summary["api_attempts"], 13)
        self.assertEqual(
            summary["agent_call_counts"], {agent.value: 3 for agent in AgentName}
        )
        results = {item["batch_id"]: item for item in runner.batch_results}
        self.assertEqual(results[first]["verdict"], Verdict.INCONCLUSIVE.value)
        self.assertEqual(results[second]["verdict"], Verdict.INCONCLUSIVE.value)
        self.assertEqual(results[third]["verdict"], Verdict.CONFIRMED.value)
        self.assertTrue(results[second]["synthetic_fail_safe_finding"])
        for result in results.values():
            self.assertEqual(
                result["agent_call_counts"], {agent.value: 1 for agent in AgentName}
            )
            self.assertEqual(result["agent_order"], [agent.value for agent in AgentName])
        calls_by_batch = {
            batch_id: [
                event["agent"]
                for event in events
                if event["batch_id"] == batch_id
                and event["event_type"] == "AGENT_CALL"
            ]
            for batch_id in (first, second, third)
        }
        for order in calls_by_batch.values():
            self.assertEqual(order, [agent.value for agent in AgentName])
        self.assertEqual(fake.attempts[(first, AgentName.INVESTIGATOR)], 1)
        self.assertEqual(fake.attempts[(second, AgentName.TRIAGE)], 2)


if __name__ == "__main__":
    unittest.main()
