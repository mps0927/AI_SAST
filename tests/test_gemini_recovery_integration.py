from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from refine_sast.providers.gemini_wire import domain_to_wire
from refine_sast.providers.mock_provider import MockProvider
from refine_sast.providers.model_router import ModelRouter
from refine_sast.runtime.live_gemini_analysis import LiveGeminiAnalysis, _target_clean
from refine_sast.stage3_schemas import (
    AgentContext,
    AgentName,
    ProofObligation,
    Verdict,
)


WORKSPACE = Path(__file__).resolve().parents[1]


class RecoveryFakeGeminiTransport:
    """Semantic Gemini fixture: no API key, socket, or raw response logging."""

    def __init__(self, scenarios: dict[str, Verdict]):
        self.scenarios = scenarios
        self.mock = MockProvider()
        self.calls: list[tuple[str, AgentName, str, dict]] = []

    def generate_content(self, model: str, request: dict, timeout_seconds: float) -> dict:
        del timeout_seconds
        payload = json.loads(request["contents"][0]["parts"][0]["text"])
        agent = AgentName(payload["context"]["agent"])
        batch_id = payload["context"]["batch_id"]
        context = AgentContext.model_validate(
            {**payload["context"], "scenario": self.scenarios[batch_id].value}
        )
        obligations = [
            ProofObligation.model_validate(item)
            for item in payload.get("obligations", [])
        ]
        if agent == AgentName.TRIAGE:
            output = self.mock.triage(context).output
        elif agent == AgentName.INVESTIGATOR:
            output = self.mock.investigate(context, obligations).output
        elif agent == AgentName.CHALLENGER:
            output = self.mock.challenge(
                context, obligations, payload["finding_id"]
            ).output
        else:
            output = self.mock.judge(context, obligations).output
        self.calls.append((batch_id, agent, model, request))
        return {
            "candidates": [
                {
                    "finishReason": "STOP",
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
                    },
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 240,
                "candidatesTokenCount": 90,
                "cachedContentTokenCount": 0,
                "thoughtsTokenCount": 8,
            },
            "modelVersion": "gemini-recovery-offline-fixture",
        }


class GeminiRecoveryIntegrationTests(unittest.TestCase):
    def test_fixed_three_batches_run_full_semantic_agent_chain_offline(self) -> None:
        selection = json.loads(
            (WORKSPACE / "artifacts" / "batches" / "selection.json").read_text(
                encoding="utf-8"
            )
        )
        batch_ids = selection["selected_batch_ids"]
        scenarios = dict(
            zip(
                batch_ids,
                [Verdict.CONFIRMED, Verdict.REJECTED, Verdict.INCONCLUSIVE],
            )
        )
        fake = RecoveryFakeGeminiTransport(scenarios)
        self.assertTrue(_target_clean(WORKSPACE / "target" / "userland"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = LiveGeminiAnalysis(
                WORKSPACE,
                transport=fake,
                run_id="gemini-recovery-offline-integration",
                max_api_calls=12,
                min_start_interval_seconds=0,
                output_root=root / "runs",
                cache_path=root / "cache.json",
                offline_test=True,
            )
            runner.router = ModelRouter(
                WORKSPACE / "config" / "model-routing.json",
                profile="gemini-recovery",
            )
            results = [
                runner._run_batch(batch_id, order)
                for order, batch_id in enumerate(batch_ids, 1)
            ]

        expected_order = list(AgentName)
        self.assertEqual(len(fake.calls), 12)
        self.assertEqual(runner.logical_calls, 12)
        self.assertEqual(
            {model for _, _, model, _ in fake.calls},
            {"gemini-3.5-flash-lite"},
        )
        for offset in range(0, 12, 4):
            self.assertEqual(
                [agent for _, agent, _, _ in fake.calls[offset : offset + 4]],
                expected_order,
            )
        self.assertEqual(
            [item["verdict"] for item in results],
            [
                Verdict.CONFIRMED.value,
                Verdict.REJECTED.value,
                Verdict.INCONCLUSIVE.value,
            ],
        )
        self.assertTrue(all(item["finding"] for item in results))
        self.assertTrue(all(item["proof_obligations"] for item in results))
        self.assertFalse(any(item["synthetic_fail_safe_finding"] for item in results))
        self.assertTrue(results[1]["contradictions"])
        self.assertEqual(
            [item["agent_order"] for item in results],
            [[agent.value for agent in AgentName]] * 3,
        )

        for _, agent, _, request in fake.calls:
            schema_text = json.dumps(request["generationConfig"]["responseJsonSchema"])
            for host_field in (
                "message_id",
                "finding_id",
                "obligation_id",
                "batch_id",
                "evidence_id",
            ):
                self.assertNotIn(host_field, schema_text)
            payload = json.loads(request["contents"][0]["parts"][0]["text"])
            verified = payload["verified_context"]
            self.assertIn("role_packet", verified)
            self.assertIn("structured_messages", verified)
            self.assertNotIn("source", json.dumps(verified["structured_messages"]))
            if agent == AgentName.JUDGE:
                self.assertTrue(
                    all("source" not in item for item in verified["verified_evidence"])
                )
                self.assertIn(
                    "unresolved_obligation_ids", verified["role_packet"]
                )
            elif agent != AgentName.TRIAGE:
                self.assertTrue(verified["structured_messages"])
        self.assertTrue(_target_clean(WORKSPACE / "target" / "userland"))


if __name__ == "__main__":
    unittest.main()
