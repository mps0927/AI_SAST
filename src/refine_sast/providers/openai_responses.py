from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from ..hashing import stable_digest
from ..stage3_schemas import (
    AgentContext,
    AgentName,
    ChallengerOutput,
    InvestigatorOutput,
    JudgeOutput,
    MockUsage,
    ProofObligation,
    TriageOutput,
)
from .base import ProviderResponse
from .model_router import ModelRouter
from .retry import (
    DEFAULT_SLEEPER,
    RETRYABLE,
    ErrorCode,
    ProviderInconclusive,
    RetryPolicy,
    SchemaResponseError,
    Sleeper,
    classify_error,
    retry_after_seconds,
)
from .usage_tracker import UsageTracker


T = TypeVar("T", bound=BaseModel)


class OpenAIResponsesProvider:
    """Responses API provider using official SDK structured parsing.

    The SDK and API key are loaded only when a real call is attempted. Tests inject
    a contract client, so Stage 4 never performs a network request.
    """

    name = "openai-responses"

    def __init__(
        self,
        *,
        workspace: Path,
        router: ModelRouter,
        tracker: UsageTracker,
        client: Any | None = None,
        retry_policy: RetryPolicy | None = None,
        sleeper: Sleeper = DEFAULT_SLEEPER,
        clock: Callable[[], float] = time.perf_counter,
        timeout_seconds: float = 60.0,
    ):
        if router.provider != self.name:
            raise ValueError(f"routing profile provider must be {self.name}")
        self.workspace = workspace.resolve()
        self.router = router
        self.tracker = tracker
        self._client = client
        self.retry_policy = retry_policy or RetryPolicy()
        self.sleeper = sleeper
        self.clock = clock
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def api_key_present() -> bool:
        return bool(os.environ.get("OPENAI_API_KEY"))

    @staticmethod
    def sdk_present() -> bool:
        return importlib.util.find_spec("openai") is not None

    def live_available(self) -> bool:
        return self._client is not None or (self.api_key_present() and self.sdk_present())

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.api_key_present():
            raise ProviderInconclusive(ErrorCode.PROVIDER_UNAVAILABLE, 0)
        if not self.sdk_present():
            raise ProviderInconclusive(ErrorCode.PROVIDER_UNAVAILABLE, 0)
        try:
            from openai import OpenAI  # type: ignore[import-not-found]

            # Disable SDK retries so this project's bounded policy is the sole authority.
            self._client = OpenAI(
                api_key=os.environ["OPENAI_API_KEY"],
                max_retries=0,
                timeout=self.timeout_seconds,
            )
        except Exception:
            raise ProviderInconclusive(ErrorCode.PROVIDER_UNAVAILABLE, 0) from None
        return self._client

    def _prompt(self, agent: AgentName) -> tuple[str, str]:
        text = (self.workspace / "prompts" / f"{agent.value.lower()}.md").read_text(encoding="utf-8")
        return text, f"prompt-v1:{stable_digest(text, 16).lower()}"

    @staticmethod
    def _schema_version(output_model: type[BaseModel]) -> str:
        schema = json.dumps(output_model.model_json_schema(), sort_keys=True, separators=(",", ":"))
        return f"schema-v1:{stable_digest(schema, 16).lower()}"

    @staticmethod
    def _context_payload(context: AgentContext) -> dict[str, Any]:
        # `scenario` is a Mock-only test control and must never bias a real model.
        return {
            "agent": context.agent.value,
            "batch_id": context.batch_id,
            "phase": context.phase,
            "input_message_ids": context.input_message_ids,
            "evidence_ids": context.evidence_ids,
            "available_chunk_ids": context.available_chunk_ids,
            "risk_tags": context.risk_tags,
            "budget_snapshot": context.budget_snapshot,
        }

    @staticmethod
    def _usage(response: Any) -> MockUsage:
        usage = getattr(response, "usage", None)
        if usage is None:
            return MockUsage(input_tokens=0, output_tokens=0)

        def value(obj: Any, key: str) -> int:
            if isinstance(obj, dict):
                return int(obj.get(key, 0) or 0)
            return int(getattr(obj, key, 0) or 0)

        input_details = (
            usage.get("input_tokens_details", {})
            if isinstance(usage, dict)
            else getattr(usage, "input_tokens_details", None)
        )
        output_details = (
            usage.get("output_tokens_details", {})
            if isinstance(usage, dict)
            else getattr(usage, "output_tokens_details", None)
        )
        return MockUsage(
            input_tokens=value(usage, "input_tokens"),
            output_tokens=value(usage, "output_tokens"),
            cached_tokens=value(input_details or {}, "cached_tokens"),
            reasoning_tokens=value(output_details or {}, "reasoning_tokens"),
        )

    def _invoke(
        self,
        *,
        agent: AgentName,
        context: AgentContext,
        payload: dict[str, Any],
        output_model: type[T],
    ) -> ProviderResponse[T]:
        route = self.router.route(agent)
        prompt_text, prompt_version = self._prompt(agent)
        schema_version = self._schema_version(output_model)
        started = self.clock()
        try:
            client = self._get_client()
        except ProviderInconclusive as terminal:
            self.tracker.record(
                provider=self.name,
                model=route.model,
                agent=agent,
                batch=context.batch_id,
                usage=MockUsage(input_tokens=0, output_tokens=0),
                retry=0,
                latency_ms=max(0, int((self.clock() - started) * 1000)),
                prompt_version=prompt_version,
                schema_version=schema_version,
                status="INCONCLUSIVE",
                error_code=terminal.code.value,
            )
            raise terminal from None

        last_code = ErrorCode.UNKNOWN
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                response = client.responses.parse(
                    model=route.model,
                    instructions=prompt_text,
                    input=[{"role": "user", "content": json.dumps(payload, sort_keys=True)}],
                    reasoning={"effort": route.reasoning_effort},
                    text_format=output_model,
                    max_output_tokens=route.max_output_tokens,
                    store=False,
                )
                parsed = getattr(response, "output_parsed", None)
                if parsed is None:
                    raise SchemaResponseError("structured response was absent")
                try:
                    output = parsed if isinstance(parsed, output_model) else output_model.model_validate(parsed)
                except ValidationError:
                    raise SchemaResponseError("structured response did not match schema") from None
                usage = self._usage(response)
                latency_ms = max(0, int((self.clock() - started) * 1000))
                self.tracker.record(
                    provider=self.name,
                    model=route.model,
                    agent=agent,
                    batch=context.batch_id,
                    usage=usage,
                    retry=attempt - 1,
                    latency_ms=latency_ms,
                    prompt_version=prompt_version,
                    schema_version=schema_version,
                )
                return ProviderResponse(
                    output=output,
                    usage=usage,
                    provider=self.name,
                    model=route.model,
                    retries=attempt - 1,
                    latency_ms=latency_ms,
                )
            except Exception as error:
                last_code = classify_error(error)
                if last_code not in RETRYABLE or attempt >= self.retry_policy.max_attempts:
                    break
                self.sleeper(self.retry_policy.delay(attempt, retry_after_seconds(error)))

        retries = max(0, min(self.retry_policy.max_attempts, attempt) - 1)
        latency_ms = max(0, int((self.clock() - started) * 1000))
        self.tracker.record(
            provider=self.name,
            model=route.model,
            agent=agent,
            batch=context.batch_id,
            usage=MockUsage(input_tokens=0, output_tokens=0),
            retry=retries,
            latency_ms=latency_ms,
            prompt_version=prompt_version,
            schema_version=schema_version,
            status="INCONCLUSIVE",
            error_code=last_code.value,
        )
        raise ProviderInconclusive(last_code, retries + 1) from None

    def triage(self, context: AgentContext) -> ProviderResponse[TriageOutput]:
        return self._invoke(
            agent=AgentName.TRIAGE,
            context=context,
            payload={"context": self._context_payload(context)},
            output_model=TriageOutput,
        )

    def investigate(
        self, context: AgentContext, obligations: list[ProofObligation]
    ) -> ProviderResponse[InvestigatorOutput]:
        return self._invoke(
            agent=AgentName.INVESTIGATOR,
            context=context,
            payload={
                "context": self._context_payload(context),
                "obligations": [item.model_dump(mode="json") for item in obligations],
            },
            output_model=InvestigatorOutput,
        )

    def challenge(
        self,
        context: AgentContext,
        obligations: list[ProofObligation],
        finding_id: str,
    ) -> ProviderResponse[ChallengerOutput]:
        return self._invoke(
            agent=AgentName.CHALLENGER,
            context=context,
            payload={
                "context": self._context_payload(context),
                "obligations": [item.model_dump(mode="json") for item in obligations],
                "finding_id": finding_id,
            },
            output_model=ChallengerOutput,
        )

    def judge(
        self, context: AgentContext, obligations: list[ProofObligation]
    ) -> ProviderResponse[JudgeOutput]:
        return self._invoke(
            agent=AgentName.JUDGE,
            context=context,
            payload={
                "context": self._context_payload(context),
                "obligations": [item.model_dump(mode="json") for item in obligations],
            },
            output_model=JudgeOutput,
        )
