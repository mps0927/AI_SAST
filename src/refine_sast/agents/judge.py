from __future__ import annotations

from .base import AgentRun, BaseAgent
from ..stage3_schemas import (
    AgentName,
    JudgeInput,
    JudgeOutput,
    ProofObligation,
    ProofStatus,
    Verdict,
)


class JudgeAgent(BaseAgent):
    role = AgentName.JUDGE
    prompt_file = "judge.md"

    @staticmethod
    def enforced_verdict(
        value: JudgeInput, governor: TokenGovernor
    ) -> Verdict:
        required = [item for item in value.obligations if item.required]
        if any(item.status == ProofStatus.REFUTED for item in required):
            return Verdict.REJECTED
        if (
            value.unresolved_obligation_ids
            or governor.exhausted
            or any(item.status == ProofStatus.UNKNOWN for item in required)
        ):
            return Verdict.INCONCLUSIVE
        if required and all(item.status == ProofStatus.SUPPORTED for item in required):
            return Verdict.CONFIRMED
        return Verdict.INCONCLUSIVE

    def run(self, value: JudgeInput) -> AgentRun[JudgeOutput]:
        context = value.context
        obligations = value.obligations
        self._validate_context(context)
        enforced = self.enforced_verdict(value, self.governor)
        response = self.provider.judge(context, obligations)
        if response.output.verdict != enforced:
            raise ValueError(
                f"Judge provider verdict {response.output.verdict} violates enforced rule {enforced}"
            )
        if not response.output.terminate:
            raise ValueError("Judge must terminate after one verdict")
        return self._complete(response.output, response.usage)
