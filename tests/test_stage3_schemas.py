from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from refine_sast.stage3_schemas import (
    AgentContext,
    AgentName,
    MessageType,
    ProofObligation,
    ProofStatus,
    StructuredMessage,
    TriageInput,
    Verdict,
    export_role_schemas,
)
from refine_sast.agents.triage import TriageAgent
from refine_sast.providers.mock_provider import MockProvider
from refine_sast.runtime.token_governor import TokenGovernor


WORKSPACE = Path(__file__).resolve().parents[1]


class Stage3SchemaTests(unittest.TestCase):
    def test_unknown_message_type_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            StructuredMessage.model_validate(
                {
                    "message_id": "MSG-X",
                    "message_type": "FREE_TEXT",
                    "agent": "TRIAGE",
                    "batch_id": "BAT-X",
                    "evidence_ids": [],
                    "payload": {},
                }
            )

    def test_raw_source_field_is_rejected_by_structured_payload(self) -> None:
        with self.assertRaises(ValidationError):
            StructuredMessage.model_validate(
                {
                    "message_id": "MSG-X",
                    "message_type": "FINDING",
                    "agent": "TRIAGE",
                    "batch_id": "BAT-X",
                    "evidence_ids": [],
                    "payload": {
                        "finding_id": "FND-X",
                        "hypothesis_code": "HYP",
                        "severity": "HIGH",
                        "obligation_ids": ["OBL-X"],
                        "source": "forbidden raw source",
                    },
                }
            )

    def test_supported_or_refuted_obligation_requires_evidence(self) -> None:
        for status in (ProofStatus.SUPPORTED, ProofStatus.REFUTED):
            with self.assertRaises(ValidationError):
                ProofObligation(
                    obligation_id="OBL-X",
                    description_code="TEST",
                    status=status,
                )

    def test_role_schemas_and_prompts_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            export_role_schemas(Path(temporary))
            role_files = sorted(Path(temporary).glob("*-output.schema.json"))
            self.assertEqual(len(role_files), 4)
            roles = {
                json.loads(path.read_text(encoding="utf-8"))["x-agent-role"]
                for path in role_files
            }
            self.assertEqual(roles, {item.value for item in AgentName})
            input_files = sorted(Path(temporary).glob("*-input.schema.json"))
            self.assertEqual(len(input_files), 4)
            input_roles = {
                json.loads(path.read_text(encoding="utf-8"))["x-agent-role"]
                for path in input_files
            }
            self.assertEqual(input_roles, roles)
        prompt_texts = [
            (WORKSPACE / "prompts" / f"{role.value.lower()}.md").read_text(encoding="utf-8")
            for role in AgentName
        ]
        self.assertEqual(len(set(prompt_texts)), 4)

    def test_agent_rejects_another_roles_independent_context(self) -> None:
        context = AgentContext(
            agent=AgentName.INVESTIGATOR,
            batch_id="BAT-X",
            scenario=Verdict.CONFIRMED,
        )
        triage = TriageAgent(MockProvider(), TokenGovernor(), WORKSPACE)
        with self.assertRaises(ValueError):
            triage.run(
                TriageInput(
                    context=context,
                    focus_evidence_id="EVD-X",
                    security_sketch_code="TEST",
                )
            )


if __name__ == "__main__":
    unittest.main()
