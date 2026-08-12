from __future__ import annotations

from .base import AgentRun, BaseAgent
from ..stage3_schemas import AgentName, TriageInput, TriageOutput


class TriageAgent(BaseAgent):
    role = AgentName.TRIAGE
    prompt_file = "triage.md"

    def run(self, value: TriageInput) -> AgentRun[TriageOutput]:
        context = value.context
        self._validate_context(context)
        response = self.provider.triage(context)
        if any(message.agent != self.role for message in response.output.messages):
            raise ValueError("Triage output contains foreign-agent message")
        if not response.output.terminate:
            raise ValueError("Triage must terminate after one bounded decision")
        return self._complete(response.output, response.usage)
