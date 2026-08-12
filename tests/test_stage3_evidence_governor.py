from __future__ import annotations

import json
import unittest
from pathlib import Path

from refine_sast.runtime.event_log import _assert_log_safe
from refine_sast.runtime.evidence import EvidenceBlackboard, EvidenceValidationError
from refine_sast.runtime.token_governor import TokenBudgetExceeded, TokenGovernor
from refine_sast.stage3_schemas import AgentName, MockUsage


WORKSPACE = Path(__file__).resolve().parents[1]


def first_chunk() -> dict[str, object]:
    line = (WORKSPACE / "artifacts" / "chunks" / "chunks.jsonl").read_text(encoding="utf-8").splitlines()[0]
    return json.loads(line)


class EvidenceGovernorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.chunk = first_chunk()
        self.board = EvidenceBlackboard(WORKSPACE / "target" / "userland")

    def _register(self, **updates: object):
        values = {
            "chunk_id": self.chunk["chunk_id"],
            "path": self.chunk["path"],
            "start_line": self.chunk["start_line"],
            "end_line": self.chunk["end_line"],
            "start_byte": self.chunk["start_byte"],
            "end_byte": self.chunk["end_byte"],
            "expected_hash": self.chunk["content_hash"],
        }
        values.update(updates)
        return self.board.register(**values)

    def test_valid_evidence_and_tampered_hash_line_byte_path_rejection(self) -> None:
        record = self._register()
        self.assertEqual(self.board.get(record.evidence_id), record)
        cases = [
            {"expected_hash": "sha256:" + "0" * 64},
            {"start_line": int(self.chunk["start_line"]) + 1},
            {"end_byte": 10**9},
            {"path": "../outside.c"},
            {"path": "does/not/exist.c"},
        ]
        for updates in cases:
            with self.subTest(updates=updates), self.assertRaises(EvidenceValidationError):
                self._register(**updates)

    def test_token_governor_enforces_role_and_context_limits(self) -> None:
        governor = TokenGovernor()
        governor.charge_call(AgentName.TRIAGE, MockUsage(input_tokens=100, output_tokens=50))
        with self.assertRaises(TokenBudgetExceeded):
            governor.charge_call(AgentName.TRIAGE, MockUsage(input_tokens=2_000, output_tokens=1))
        context_governor = TokenGovernor()
        context_governor.authorize_context(AgentName.INVESTIGATOR, 100)
        context_governor.authorize_context(AgentName.INVESTIGATOR, 100)
        with self.assertRaises(TokenBudgetExceeded):
            context_governor.authorize_context(AgentName.INVESTIGATOR, 100)
        self.assertTrue(context_governor.exhausted)
        self.assertEqual(context_governor.context_requests, 2)

    def test_event_log_safety_rejects_raw_source_or_secrets(self) -> None:
        for value in ({"source": "code"}, {"nested": {"api_key": "secret"}}, {"raw": [1]}):
            with self.assertRaises(ValueError):
                _assert_log_safe(value)


if __name__ == "__main__":
    unittest.main()
