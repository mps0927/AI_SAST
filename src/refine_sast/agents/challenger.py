from __future__ import annotations

from .base import AgentRun, BaseAgent
from ..stage3_schemas import AgentName, ChallengerInput, ChallengerOutput


class ChallengerAgent(BaseAgent):
    role = AgentName.CHALLENGER
    prompt_file = "challenger.md"

    def run(self, value: ChallengerInput) -> AgentRun[ChallengerOutput]:
        context = value.context
        self._validate_context(context)
        response = self.provider.challenge(context, value.obligations, value.finding_id)
        if any(message.agent != self.role for message in response.output.messages):
            raise ValueError("Challenger output contains foreign-agent message")
        if not response.output.terminate:
            raise ValueError("Challenger must terminate after bounded contradiction pass")
        return self._complete(response.output, response.usage)
