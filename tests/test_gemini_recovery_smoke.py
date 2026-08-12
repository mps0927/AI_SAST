from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_gemini_recovery_smoke import MODEL, PROFILE, SYNTHETIC_BATCH, run_smoke
from tests.test_gemini_recovery_integration import RecoveryFakeGeminiTransport
from refine_sast.stage3_schemas import AgentName, Verdict


WORKSPACE = Path(__file__).resolve().parents[1]


class GeminiRecoverySmokeTests(unittest.TestCase):
    def test_four_role_smoke_is_synthetic_network_free_and_bounded(self) -> None:
        fake = RecoveryFakeGeminiTransport({SYNTHETIC_BATCH: Verdict.INCONCLUSIVE})
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"GEMINI_API_KEY": ""}, clear=False
        ):
            root = Path(temporary)
            result = run_smoke(
                WORKSPACE,
                transport=fake,
                run_id="gemini-recovery-smoke-offline",
                output_root=root,
                max_calls=8,
                min_interval_seconds=0,
                offline=True,
            )
            persisted = "".join(
                path.read_text(encoding="utf-8")
                for path in (root / "gemini-recovery-smoke-offline").iterdir()
                if path.is_file()
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["profile"], PROFILE)
        self.assertEqual(result["model"], MODEL)
        self.assertEqual(result["logical_agent_calls"], 4)
        self.assertEqual(result["api_attempts"], 4)
        self.assertEqual(result["api_attempt_cap"], 8)
        self.assertEqual(result["agent_order"], [agent.value for agent in AgentName])
        self.assertEqual([call[1] for call in fake.calls], list(AgentName))
        self.assertEqual({call[0] for call in fake.calls}, {SYNTHETIC_BATCH})
        self.assertEqual({call[2] for call in fake.calls}, {MODEL})
        self.assertFalse(result["target_code_used"])
        self.assertFalse(result["fixed_batches_used"])
        self.assertTrue(result["target_clean_before"])
        self.assertTrue(result["target_clean_after"])
        self.assertNotIn("strcpy(buffer, input)", persisted)
        self.assertNotIn("GEMINI_API_KEY", persisted)
        self.assertNotIn("AIza", persisted)
        self.assertEqual(len(result["call_diagnostics"]), 4)
        self.assertTrue(all(item["status"] == "SUCCESS" for item in result["call_diagnostics"]))


if __name__ == "__main__":
    unittest.main()
