from __future__ import annotations

from .base import AgentRun, BaseAgent
from ..stage3_schemas import AgentName, InvestigatorInput, InvestigatorOutput


class InvestigatorAgent(BaseAgent):
    role = AgentName.INVESTIGATOR
    prompt_file = "investigator.md"

    def run(self, value: InvestigatorInput) -> AgentRun[InvestigatorOutput]:
        context = value.context
        self._validate_context(context)
        response = self.provider.investigate(context, value.obligations)
        if any(message.agent != self.role for message in response.output.messages):
            raise ValueError("Investigator output contains foreign-agent message")
        return self._complete(response.output, response.usage)
