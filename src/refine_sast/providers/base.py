from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar, runtime_checkable

from ..stage3_schemas import (
    AgentContext,
    ChallengerOutput,
    InvestigatorOutput,
    JudgeOutput,
    MockUsage,
    ProofObligation,
    TriageOutput,
)


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ProviderResponse(Generic[T]):
    """Provider-neutral response returned to an Agent.

    Only the typed output and normalized token usage are consumed by Stage 3.
    Provider metadata is safe operational metadata used by Stage 4 accounting.
    """

    output: T
    usage: MockUsage
    provider: str
    model: str
    retries: int = 0
    latency_ms: int = 0
    model_digest: str | None = None
    cache_hit: bool = False


@runtime_checkable
class LLMProvider(Protocol):
    """The role interface shared by deterministic Mock and live providers."""

    name: str

    def triage(self, context: AgentContext) -> ProviderResponse[TriageOutput]: ...

    def investigate(
        self, context: AgentContext, obligations: list[ProofObligation]
    ) -> ProviderResponse[InvestigatorOutput]: ...

    def challenge(
        self,
        context: AgentContext,
        obligations: list[ProofObligation],
        finding_id: str,
    ) -> ProviderResponse[ChallengerOutput]: ...

    def judge(
        self, context: AgentContext, obligations: list[ProofObligation]
    ) -> ProviderResponse[JudgeOutput]: ...
