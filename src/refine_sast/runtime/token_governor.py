from __future__ import annotations

from dataclasses import dataclass, field

from ..stage3_schemas import AgentName, MockUsage


class TokenBudgetExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class RoleBudget:
    input_limit: int
    output_limit: int
    used_input: int = 0
    used_output: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0


@dataclass(slots=True)
class TokenGovernor:
    batch_input_limit: int = 22_000
    batch_output_limit: int = 5_000
    context_request_limit: int = 2
    context_request_token_limit: int = 2_500
    roles: dict[AgentName, RoleBudget] = field(default_factory=lambda: {
        AgentName.TRIAGE: RoleBudget(2_000, 400),
        AgentName.INVESTIGATOR: RoleBudget(6_000, 1_200),
        AgentName.CHALLENGER: RoleBudget(4_000, 800),
        AgentName.JUDGE: RoleBudget(3_500, 800),
    })
    context_requests: int = 0
    context_tokens: int = 0
    denied_reasons: list[str] = field(default_factory=list)

    def charge_call(self, agent: AgentName, usage: MockUsage) -> None:
        budget = self.roles[agent]
        if budget.used_input + usage.input_tokens > budget.input_limit:
            self._deny(f"{agent}:input-budget")
        if budget.used_output + usage.output_tokens > budget.output_limit:
            self._deny(f"{agent}:output-budget")
        if self.total_input + usage.input_tokens > self.batch_input_limit:
            self._deny("batch-input-budget")
        if self.total_output + usage.output_tokens > self.batch_output_limit:
            self._deny("batch-output-budget")
        budget.used_input += usage.input_tokens
        budget.used_output += usage.output_tokens
        budget.reasoning_tokens += usage.reasoning_tokens
        budget.cached_tokens += usage.cached_tokens

    def authorize_context(self, agent: AgentName, estimated_tokens: int) -> None:
        if self.context_requests >= self.context_request_limit:
            self._deny("context-request-count")
        if estimated_tokens > self.context_request_token_limit:
            self._deny("context-request-size")
        budget = self.roles[agent]
        if budget.used_input + estimated_tokens > budget.input_limit:
            self._deny(f"{agent}:context-input-budget")
        if self.total_input + estimated_tokens > self.batch_input_limit:
            self._deny("batch-context-budget")
        self.context_requests += 1
        self.context_tokens += estimated_tokens
        budget.used_input += estimated_tokens

    def _deny(self, reason: str) -> None:
        self.denied_reasons.append(reason)
        raise TokenBudgetExceeded(reason)

    @property
    def total_input(self) -> int:
        return sum(value.used_input for value in self.roles.values())

    @property
    def total_output(self) -> int:
        return sum(value.used_output for value in self.roles.values())

    @property
    def exhausted(self) -> bool:
        return bool(self.denied_reasons)

    def snapshot(self) -> dict[str, int | bool | str | list[str]]:
        return {
            "batch_input_limit": self.batch_input_limit,
            "batch_output_limit": self.batch_output_limit,
            "used_input": self.total_input,
            "used_output": self.total_output,
            "context_requests": self.context_requests,
            "context_tokens": self.context_tokens,
            "exhausted": self.exhausted,
            "denied_reasons": list(self.denied_reasons),
        }
