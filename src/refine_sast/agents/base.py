from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from ..hashing import stable_digest
from ..providers.base import LLMProvider
from ..stage3_schemas import AgentContext, AgentName, MockUsage
from ..runtime.token_governor import TokenGovernor


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class AgentRun(Generic[T]):
    output: T
    usage: MockUsage
    prompt_version: str


class BaseAgent:
    role: AgentName
    prompt_file: str

    def __init__(self, provider: LLMProvider, governor: TokenGovernor, workspace: Path):
        self.provider = provider
        self.governor = governor
        self.prompt_path = workspace / "prompts" / self.prompt_file
        self.prompt_text = self.prompt_path.read_text(encoding="utf-8")
        self.prompt_version = f"prompt-v1:{stable_digest(self.prompt_text, 16).lower()}"

    def _validate_context(self, context: AgentContext) -> None:
        if context.agent != self.role:
            raise ValueError(f"{self.role} received {context.agent} context")

    def _complete(self, output: T, usage: MockUsage) -> AgentRun[T]:
        self.governor.charge_call(self.role, usage)
        return AgentRun(output=output, usage=usage, prompt_version=self.prompt_version)
