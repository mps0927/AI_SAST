from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from refine_sast.cache import ContentHashCache
from refine_sast.providers.base import LLMProvider
from refine_sast.providers.factory import ProviderFactory
from refine_sast.providers.gemini_provider import GeminiProvider
from refine_sast.providers.gemini_transport import (
    GeminiCallBudgetExceededError,
    GeminiHTTPError,
    GeminiTransport,
)
from refine_sast.providers.gemini_wire import (
    ALLOWED_BY_ROLE,
    GeminiInvestigatorWireOutput,
    ObligationAssessmentWire,
    domain_to_wire,
    wire_output_model,
    wire_to_domain,
)
from refine_sast.providers.local_context import StaticLocalContextSource
from refine_sast.providers.mock_provider import MockProvider
from refine_sast.providers.model_router import ModelRouter
from refine_sast.providers.retry import ErrorCode, ProviderInconclusive, RetryPolicy
from refine_sast.providers.usage_tracker import UsageTracker
from refine_sast.stage3_schemas import (
    AgentContext,
    AgentName,
    MessageType,
    ProofObligation,
    ProofStatus,
    RequestKind,
    Verdict,
)


WORKSPACE = Path(__file__).resolve().parents[1]


def context(agent: AgentName) -> AgentContext:
    return AgentContext(
        agent=agent,
        batch_id="BAT-GEMINI-CONTRACT",
        scenario=Verdict.CONFIRMED,
        evidence_ids=["EVD-1"],
        available_chunk_ids=["CHK-1"],
        risk_tags=["MEMORY_UNSAFE"],
    )


class FakeGeminiTransport:
    def __init__(self, outcomes: list[object]):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict[str, object], float]] = []

    def generate_content(
        self, model: str, request: dict[str, object], timeout_seconds: float
    ) -> dict[str, object]:
        self.calls.append((model, request, timeout_seconds))
        if not self.outcomes:
            raise AssertionError("fake Gemini outcome queue is empty")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, dict):
            return outcome
        if isinstance(outcome, str):
            text = outcome
        else:
            text = json.dumps(domain_to_wire(outcome), ensure_ascii=False)
        return {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": text}]}}
            ],
            "usageMetadata": {
                "promptTokenCount": 101,
                "candidatesTokenCount": 37,
                "cachedContentTokenCount": 11,
                "thoughtsTokenCount": 13,
                "totalTokenCount": 151,
            },
            "modelVersion": "gemini-3.6-flash-test-version",
        }


def raw_response(
    text: str | None,
    *,
    finish_reason: str = "STOP",
    include_candidate: bool = True,
) -> dict[str, object]:
    candidates: list[dict[str, object]] = []
    if include_candidate:
        candidate: dict[str, object] = {"finishReason": finish_reason}
        if text is not None:
            candidate["content"] = {
                "role": "model",
                "parts": [{"text": text}],
            }
        candidates.append(candidate)
    return {
        "candidates": candidates,
        "usageMetadata": {
            "promptTokenCount": 71,
            "candidatesTokenCount": 19,
            "cachedContentTokenCount": 3,
            "thoughtsTokenCount": 5,
        },
        "modelVersion": "gemini-diagnostic-fake",
    }


class GeminiProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = ModelRouter(
            WORKSPACE / "config" / "model-routing.json", profile="gemini-free"
        )

    def make_provider(
        self,
        temporary: str,
        transport: FakeGeminiTransport | None,
        *,
        cache: ContentHashCache | None = None,
    ) -> tuple[GeminiProvider, UsageTracker]:
        tracker = UsageTracker("gemini-test", Path(temporary) / "token-ledger.json")
        provider = GeminiProvider(
            workspace=WORKSPACE,
            router=self.router,
            tracker=tracker,
            context_source=StaticLocalContextSource(
                {"verified_evidence": [{"evidence_id": "EVD-1"}]}
            ),
            transport=transport,
            cache=cache,
            retry_policy=RetryPolicy(max_attempts=2, jitter_ratio=0),
            sleeper=lambda _: None,
        )
        return provider, tracker

    def test_transport_uses_header_not_query_string_and_fake_requester(self) -> None:
        fake_secret = "gemini-secret-must-not-leak"
        calls: list[tuple[str, str, dict[str, str], dict[str, object], float]] = []

        def requester(
            method: str,
            url: str,
            headers: dict[str, str],
            payload: dict[str, object],
            timeout: float,
        ) -> dict[str, object]:
            calls.append((method, url, headers, payload, timeout))
            return {"candidates": []}

        transport = GeminiTransport(
            "https://generativelanguage.googleapis.com/v1beta/",
            fake_secret,
            requester=requester,
        )
        transport.generate_content("gemini/test", {"contents": []}, 9)
        method, url, headers, payload, timeout = calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/models/gemini%2Ftest:generateContent"))
        self.assertNotIn(fake_secret, url)
        self.assertEqual(headers, {"x-goog-api-key": fake_secret})
        self.assertEqual(payload, {"contents": []})
        self.assertEqual(timeout, 9)

    def test_mock_and_gemini_share_four_role_contract_and_structured_schema(self) -> None:
        mock = MockProvider()
        triage = mock.triage(context(AgentName.TRIAGE)).output
        investigator = mock.investigate(
            context(AgentName.INVESTIGATOR), triage.obligations
        ).output
        challenger = mock.challenge(
            context(AgentName.CHALLENGER),
            investigator.obligation_updates,
            triage.messages[0].payload.finding_id,
        ).output
        judge = mock.judge(
            context(AgentName.JUDGE), investigator.obligation_updates
        ).output
        transport = FakeGeminiTransport(
            [triage, investigator, challenger, judge]
        )
        with tempfile.TemporaryDirectory() as temporary:
            provider, tracker = self.make_provider(temporary, transport)
            self.assertIsInstance(provider, LLMProvider)
            actual_triage = provider.triage(context(AgentName.TRIAGE))
            actual_investigator = provider.investigate(
                context(AgentName.INVESTIGATOR), actual_triage.output.obligations
            )
            actual_challenger = provider.challenge(
                context(AgentName.CHALLENGER),
                actual_investigator.output.obligation_updates,
                actual_triage.output.messages[0].payload.finding_id,
            )
            actual_judge = provider.judge(
                context(AgentName.JUDGE),
                actual_investigator.output.obligation_updates,
            )
            self.assertEqual(
                [
                    type(actual_triage.output),
                    type(actual_investigator.output),
                    type(actual_challenger.output),
                    type(actual_judge.output),
                ],
                [type(triage), type(investigator), type(challenger), type(judge)],
            )
            self.assertEqual(len(tracker.records), 4)
            first = tracker.records[0]
            self.assertEqual(
                (
                    first.input_tokens,
                    first.output_tokens,
                    first.cached_tokens,
                    first.reasoning_tokens,
                ),
                (101, 37, 11, 13),
            )
            self.assertEqual(first.model_digest, "gemini-3.6-flash-test-version")
            expected_thinking = ["low", "medium", "medium", "high"]
            for index, (_, request, _) in enumerate(transport.calls):
                generation = request["generationConfig"]
                self.assertEqual(generation["responseMimeType"], "application/json")
                self.assertIsInstance(generation["responseJsonSchema"], dict)
                self.assertEqual(
                    generation["thinkingConfig"]["thinkingLevel"],
                    expected_thinking[index],
                )
                body = json.loads(request["contents"][0]["parts"][0]["text"])
                self.assertNotIn("scenario", json.dumps(body))
                self.assertEqual(
                    body["verified_context"]["verified_evidence"][0]["evidence_id"],
                    "EVD-1",
                )
                schema_text = json.dumps(generation["responseJsonSchema"])
                self.assertNotIn('"anyOf"', schema_text)
                self.assertNotIn('"oneOf"', schema_text)

    def test_investigator_wire_schema_is_flat_and_round_trips_strict_domain(self) -> None:
        mock = MockProvider()
        triage = mock.triage(context(AgentName.TRIAGE)).output
        investigator = mock.investigate(
            context(AgentName.INVESTIGATOR), triage.obligations
        ).output
        wire = wire_output_model(AgentName.INVESTIGATOR)
        schema = wire.model_json_schema()
        schema_text = json.dumps(schema, sort_keys=True)
        self.assertNotIn('"anyOf"', schema_text)
        self.assertNotIn('"oneOf"', schema_text)
        transport = FakeGeminiTransport([investigator])
        with tempfile.TemporaryDirectory() as temporary:
            provider, _ = self.make_provider(temporary, transport)
            response = provider.investigate(
                context(AgentName.INVESTIGATOR), triage.obligations
            )
        self.assertEqual(
            [item.status for item in response.output.obligation_updates],
            [ProofStatus.SUPPORTED] * len(triage.obligations),
        )
        self.assertTrue(response.output.messages)
        self.assertTrue(
            all(item.message_type == MessageType.EVIDENCE for item in response.output.messages)
        )

    def test_wire_adapter_preserves_allowlist_and_rejects_unverified_indexes(self) -> None:
        self.assertEqual(
            set().union(*ALLOWED_BY_ROLE.values()),
            set(MessageType),
        )
        invalid = GeminiInvestigatorWireOutput(
            assessments=[
                ObligationAssessmentWire(
                    obligation_index=0,
                    status=ProofStatus.SUPPORTED,
                    summary_code="INVALID_INDEX",
                    evidence_indexes=[99],
                )
            ],
            request_context=False,
            request_kind=RequestKind.GET_DATAFLOW_SLICE,
            requested_chunk_index=0,
            requested_obligation_index=0,
            request_reason_code="NOT_REQUESTED",
            terminate=True,
        )
        with self.assertRaises(ValueError):
            wire_to_domain(
                AgentName.INVESTIGATOR,
                invalid,
                context=context(AgentName.INVESTIGATOR),
                obligations=[ProofObligation(obligation_id="OBL-1", description_code="P")],
            )

    def test_json_failure_is_diagnosed_without_identical_retry(self) -> None:
        transport = FakeGeminiTransport(["not-json", "still-not-json"])
        with tempfile.TemporaryDirectory() as temporary:
            provider, tracker = self.make_provider(temporary, transport)
            with self.assertRaises(ProviderInconclusive) as caught:
                provider.triage(context(AgentName.TRIAGE))
            self.assertEqual(caught.exception.code, ErrorCode.JSON_INVALID)
            self.assertEqual(caught.exception.attempts, 1)
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(tracker.records[0].retry, 0)
            self.assertEqual(tracker.records[0].status, "INCONCLUSIVE")
            self.assertTrue(tracker.records[0].usage_reported)
            self.assertEqual(tracker.records[0].validation_stage, "JSON")
            self.assertEqual(tracker.records[0].input_tokens, 101)

    def test_output_missing_and_max_tokens_are_distinct_and_preserve_usage(self) -> None:
        cases = [
            (
                raw_response(None, finish_reason="STOP"),
                ErrorCode.OUTPUT_MISSING,
                "OUTPUT",
                "STOP",
            ),
            (
                raw_response(None, finish_reason="MAX_TOKENS"),
                ErrorCode.MAX_TOKENS,
                "OUTPUT",
                "MAX_TOKENS",
            ),
        ]
        for response, expected, stage, finish_reason in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temporary:
                provider, tracker = self.make_provider(
                    temporary, FakeGeminiTransport([response])
                )
                with self.assertRaises(ProviderInconclusive) as caught:
                    provider.triage(context(AgentName.TRIAGE))
                record = tracker.records[0]
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(caught.exception.attempts, 1)
                self.assertEqual(record.validation_stage, stage)
                self.assertEqual(record.finish_reason, finish_reason)
                self.assertFalse(record.response_present)
                self.assertEqual(record.response_chars, 0)
                self.assertTrue(record.usage_reported)
                self.assertEqual(record.input_tokens, 71)
                self.assertEqual(record.output_tokens, 19)

    def test_wire_and_domain_failures_are_distinct_and_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider, tracker = self.make_provider(
                temporary, FakeGeminiTransport([raw_response("{}")])
            )
            with self.assertRaises(ProviderInconclusive) as caught:
                provider.triage(context(AgentName.TRIAGE))
            self.assertEqual(caught.exception.code, ErrorCode.WIRE_SCHEMA_INVALID)
            self.assertEqual(tracker.records[0].validation_stage, "WIRE_SCHEMA")
            self.assertTrue(tracker.records[0].validation_error_paths)

        invalid_domain = GeminiInvestigatorWireOutput(
            assessments=[
                ObligationAssessmentWire(
                    obligation_index=0,
                    status=ProofStatus.SUPPORTED,
                    summary_code="OUT_OF_RANGE_EVIDENCE",
                    evidence_indexes=[99],
                )
            ],
            request_context=False,
            request_kind=RequestKind.GET_DATAFLOW_SLICE,
            requested_chunk_index=0,
            requested_obligation_index=0,
            request_reason_code="NOT_REQUESTED",
            terminate=True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            provider, tracker = self.make_provider(
                temporary,
                FakeGeminiTransport(
                    [raw_response(invalid_domain.model_dump_json())]
                ),
            )
            with self.assertRaises(ProviderInconclusive) as caught:
                provider.investigate(
                    context(AgentName.INVESTIGATOR),
                    [ProofObligation(obligation_id="OBL-1", description_code="P")],
                )
            self.assertEqual(caught.exception.code, ErrorCode.DOMAIN_RULE_INVALID)
            self.assertEqual(tracker.records[0].validation_stage, "DOMAIN_RULE")
            ledger = tracker.output_path.read_text(encoding="utf-8")
            self.assertNotIn("OUT_OF_RANGE_EVIDENCE", ledger)

    def test_rate_limit_retries_once_but_call_cap_does_not_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            transport = FakeGeminiTransport(
                [GeminiHTTPError(429), GeminiHTTPError(429)]
            )
            provider, tracker = self.make_provider(temporary, transport)
            with self.assertRaises(ProviderInconclusive) as caught:
                provider.triage(context(AgentName.TRIAGE))
            self.assertEqual(caught.exception.code, ErrorCode.RATE_LIMIT)
            self.assertEqual(caught.exception.attempts, 2)
            self.assertEqual(len(transport.calls), 2)
            self.assertEqual(tracker.records[0].retry, 1)

        with tempfile.TemporaryDirectory() as temporary:
            transport = FakeGeminiTransport([GeminiCallBudgetExceededError()])
            provider, tracker = self.make_provider(temporary, transport)
            with self.assertRaises(ProviderInconclusive) as caught:
                provider.triage(context(AgentName.TRIAGE))
            self.assertEqual(caught.exception.code, ErrorCode.CALL_CAP_EXHAUSTED)
            self.assertEqual(caught.exception.attempts, 1)
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(tracker.records[0].retry, 0)

    def test_usage_ledger_and_cache_never_store_source_or_secret(self) -> None:
        fake_secret = "gemini-key-must-not-appear"
        source_marker = "sensitive-source-must-not-appear"
        expected = MockProvider().triage(context(AgentName.TRIAGE)).output
        transport = FakeGeminiTransport([expected])
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"GEMINI_API_KEY": fake_secret}, clear=True
        ):
            cache_path = Path(temporary) / "cache.json"
            tracker = UsageTracker("secret", Path(temporary) / "ledger.json")
            provider = GeminiProvider(
                workspace=WORKSPACE,
                router=self.router,
                tracker=tracker,
                context_source=StaticLocalContextSource(
                    {
                        "verified_evidence": [
                            {"evidence_id": "EVD-1", "source": source_marker}
                        ]
                    }
                ),
                transport=transport,
                cache=ContentHashCache(cache_path),
                sleeper=lambda _: None,
            )
            first = provider.triage(context(AgentName.TRIAGE))
            second = provider.triage(context(AgentName.TRIAGE))
            self.assertFalse(first.cache_hit)
            self.assertTrue(second.cache_hit)
            self.assertEqual(len(transport.calls), 1)
            persisted = (
                cache_path.read_text(encoding="utf-8")
                + tracker.output_path.read_text(encoding="utf-8")
            )
            self.assertNotIn(fake_secret, persisted)
            self.assertNotIn(source_marker, persisted)
            self.assertEqual(tracker.records[-1].status, "CACHE_HIT")

    def test_missing_key_is_network_free_and_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {}, clear=True
        ):
            provider, tracker = self.make_provider(temporary, None)
            self.assertFalse(provider.api_key_present())
            self.assertFalse(provider.live_available())
            with self.assertRaises(ProviderInconclusive) as caught:
                provider.triage(context(AgentName.TRIAGE))
            self.assertEqual(caught.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)
            self.assertEqual(tracker.records[0].status, "INCONCLUSIVE")

    def test_http_secret_bearing_error_is_sanitized_and_not_retried(self) -> None:
        fake_secret = "gemini-auth-error-secret"
        transport = FakeGeminiTransport([GeminiHTTPError(401)])
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"GEMINI_API_KEY": fake_secret}, clear=True
        ):
            provider, tracker = self.make_provider(temporary, transport)
            with self.assertRaises(ProviderInconclusive) as caught:
                provider.triage(context(AgentName.TRIAGE))
            self.assertEqual(caught.exception.code, ErrorCode.AUTHENTICATION)
            self.assertEqual(caught.exception.attempts, 1)
            self.assertEqual(len(transport.calls), 1)
            ledger = tracker.output_path.read_text(encoding="utf-8")
            self.assertNotIn(fake_secret, ledger)
            self.assertNotIn(fake_secret, repr(caught.exception))

    def test_factory_selects_gemini_without_initializing_other_providers(self) -> None:
        expected = MockProvider().triage(context(AgentName.TRIAGE)).output
        transport = FakeGeminiTransport([expected])
        with tempfile.TemporaryDirectory() as temporary, patch(
            "refine_sast.providers.openai_responses.OpenAIResponsesProvider",
            side_effect=AssertionError("OpenAI provider must remain dormant"),
        ), patch(
            "refine_sast.providers.ollama_transport.OllamaTransport",
            side_effect=AssertionError("Ollama transport must remain dormant"),
        ):
            tracker = UsageTracker("factory", Path(temporary) / "ledger.json")
            provider = ProviderFactory.create(
                workspace=WORKSPACE,
                router=self.router,
                tracker=tracker,
                context_source=StaticLocalContextSource(),
                gemini_transport=transport,
            )
            response = provider.triage(context(AgentName.TRIAGE))
            self.assertEqual(response.provider, "gemini-generate-content")


if __name__ == "__main__":
    unittest.main()
