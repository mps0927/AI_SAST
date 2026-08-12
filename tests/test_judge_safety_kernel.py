from __future__ import annotations

import unittest
from pathlib import Path

from refine_sast.agents import JudgeAgent
from refine_sast.providers.mock_provider import MockProvider
from refine_sast.runtime.token_governor import TokenGovernor
from refine_sast.stage3_schemas import (
    AgentContext,
    AgentName,
    JudgeInput,
    ProofObligation,
    ProofStatus,
    Verdict,
)


WORKSPACE = Path(__file__).resolve().parents[1]


def obligation(status: ProofStatus) -> ProofObligation:
    return ProofObligation(
        obligation_id=f"OBL-{status.value}",
        description_code="DIRECT_SAFETY_KERNEL_TEST",
        status=status,
        evidence_ids=[] if status == ProofStatus.UNKNOWN else ["EVD-TEST"],
    )


def judge_input(verdict: Verdict, status: ProofStatus) -> JudgeInput:
    return JudgeInput(
        context=AgentContext(
            agent=AgentName.JUDGE,
            batch_id="BAT-JUDGE-KERNEL",
            scenario=verdict,
            evidence_ids=["EVD-TEST"],
        ),
        obligations=[obligation(status)],
        unresolved_obligation_ids=(
            [f"OBL-{status.value}"] if status == ProofStatus.UNKNOWN else []
        ),
        finding_message_id="MSG-FINDING",
    )


class JudgeSafetyKernelTests(unittest.TestCase):
    def test_proof_obligations_deterministically_select_all_three_verdicts(self) -> None:
        cases = (
            (ProofStatus.SUPPORTED, Verdict.CONFIRMED),
            (ProofStatus.REFUTED, Verdict.REJECTED),
            (ProofStatus.UNKNOWN, Verdict.INCONCLUSIVE),
        )
        for status, verdict in cases:
            with self.subTest(status=status):
                agent = JudgeAgent(MockProvider(), TokenGovernor(), WORKSPACE)
                self.assertEqual(agent.run(judge_input(verdict, status)).output.verdict, verdict)

    def test_provider_cannot_override_unknown_as_confirmed(self) -> None:
        agent = JudgeAgent(MockProvider(), TokenGovernor(), WORKSPACE)
        with self.assertRaisesRegex(ValueError, "violates enforced rule INCONCLUSIVE"):
            agent.run(judge_input(Verdict.CONFIRMED, ProofStatus.UNKNOWN))

    def test_exhausted_budget_forces_inconclusive(self) -> None:
        governor = TokenGovernor()
        governor.denied_reasons.append("DIRECT_TEST_EXHAUSTED")
        agent = JudgeAgent(MockProvider(), governor, WORKSPACE)
        with self.assertRaisesRegex(ValueError, "violates enforced rule INCONCLUSIVE"):
            agent.run(judge_input(Verdict.CONFIRMED, ProofStatus.SUPPORTED))


if __name__ == "__main__":
    unittest.main()
