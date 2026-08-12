from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from ..cache import ContentHashCache
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
from .local_capabilities import LocalModelCapabilityRegistry
from .local_context import LocalContextSource
from .model_router import ModelRouter, RouteConfig
from .ollama_transport import OllamaClient
from .retry import (
    DEFAULT_SLEEPER,
    RETRYABLE,
    ErrorCode,
    ProviderInconclusive,
    RetryPolicy,
    SchemaResponseError,
    Sleeper,
    classify_error,
)
from .usage_tracker import UsageTracker


T = TypeVar("T", bound=BaseModel)


class LocalLLMProvider:
    """Provider-neutral Agent interface backed by a local Ollama server.

    Source is obtained from a verified ephemeral context source and is never
    written to the cache or token ledger. Tests inject an in-memory transport.
    """

    name = "ollama-local"

    def __init__(
        self,
        *,
        workspace: Path,
        router: ModelRouter,
        tracker: UsageTracker,
        transport: OllamaClient,
        context_source: LocalContextSource,
        capabilities: LocalModelCapabilityRegistry,
        cache: ContentHashCache | None = None,
        retry_policy: RetryPolicy | None = None,
        sleeper: Sleeper = DEFAULT_SLEEPER,
        clock: Callable[[], float] = time.perf_counter,
        timeout_seconds: float = 120.0,
    ):
        if router.provider != self.name:
            raise ValueError(f"routing profile provider must be {self.name}")
        self.workspace = workspace.resolve()
        self.router = router
        self.tracker = tracker
        self.transport = transport
        self.context_source = context_source
        self.capabilities = capabilities
        self.cache = cache
        self.retry_policy = retry_policy or RetryPolicy(max_attempts=2)
        self.sleeper = sleeper
        self.clock = clock
        self.timeout_seconds = timeout_seconds
        self._validate_routes()

    def _validate_routes(self) -> None:
        for agent in AgentName:
            route = self.router.route(agent)
            for model in (route.model, route.fallback_model):
                if model is None:
                    continue
                capability = self.capabilities.require(model)
                if not capability.structured_output:
                    raise ValueError(f"local model lacks Structured Output capability: {model}")
                if route.think and not capability.thinking:
                    raise ValueError(f"local model lacks thinking capability: {model}")

    def _prompt(self, agent: AgentName) -> tuple[str, str]:
        text = (self.workspace / "prompts" / f"{agent.value.lower()}.md").read_text(
            encoding="utf-8"
        )
        return text, f"prompt-v1:{stable_digest(text, 16).lower()}"

    @staticmethod
    def _schema(output_model: type[BaseModel]) -> tuple[dict[str, Any], str]:
        schema = output_model.model_json_schema()
        return schema, f"schema-v1:{stable_digest(schema, 16).lower()}"

    @staticmethod
    def _context_payload(context: AgentContext) -> dict[str, Any]:
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
    def _usage(response: dict[str, Any]) -> MockUsage:
        return MockUsage(
            input_tokens=int(response.get("prompt_eval_count", 0) or 0),
            output_tokens=int(response.get("eval_count", 0) or 0),
            cached_tokens=0,
            reasoning_tokens=0,
        )

    @staticmethod
    def _duration_ms(response: dict[str, Any], key: str) -> int:
        return max(0, int(int(response.get(key, 0) or 0) / 1_000_000))

    def _cache_key(
        self,
        *,
        model_digest: str,
        prompt_version: str,
        schema_version: str,
        context_hash: str,
    ) -> str:
        value = {
            "provider": self.name,
            "model_digest": model_digest,
            "prompt_version": prompt_version,
            "schema_version": schema_version,
            "context_hash": context_hash,
        }
        return "llm-response-v1|" + stable_digest(value, 64).lower()

    def _request(
        self,
        *,
        model: str,
        route: RouteConfig,
        prompt: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "num_predict": route.max_output_tokens,
            "seed": 0,
        }
        if route.num_ctx is not None:
            options["num_ctx"] = route.num_ctx
        if route.temperature is not None:
            options["temperature"] = route.temperature
        request: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
            ],
            "format": schema,
            "stream": False,
            "options": options,
        }
        if route.think:
            request["think"] = True
        if route.keep_alive is not None:
            request["keep_alive"] = route.keep_alive
        return self.transport.chat(request, self.timeout_seconds)

    def _record(
        self,
        *,
        model: str,
        model_digest: str | None,
        agent: AgentName,
        context: AgentContext,
        usage: MockUsage,
        retries: int,
        latency_ms: int,
        prompt_version: str,
        schema_version: str,
        context_hash: str,
        status: str,
        error_code: str | None = None,
        fallback_from: str | None = None,
        response: dict[str, Any] | None = None,
    ) -> None:
        response = response or {}
        self.tracker.record(
            provider=self.name,
            model=model,
            model_digest=model_digest,
            agent=agent,
            batch=context.batch_id,
            usage=usage,
            retry=retries,
            latency_ms=latency_ms,
            prompt_version=prompt_version,
            schema_version=schema_version,
            context_hash=context_hash,
            fallback_from=fallback_from,
            cached_tokens_reported=False,
            reasoning_tokens_reported=False,
            load_duration_ms=self._duration_ms(response, "load_duration"),
            prompt_eval_duration_ms=self._duration_ms(response, "prompt_eval_duration"),
            eval_duration_ms=self._duration_ms(response, "eval_duration"),
            status=status,
            error_code=error_code,
        )

    def _invoke_model(
        self,
        *,
        model: str,
        fallback_from: str | None,
        route: RouteConfig,
        agent: AgentName,
        context: AgentContext,
        prompt: str,
        prompt_version: str,
        schema: dict[str, Any],
        schema_version: str,
        payload: dict[str, Any],
        context_hash: str,
        output_model: type[T],
    ) -> ProviderResponse[T]:
        started = self.clock()
        model_digest: str | None = None
        try:
            model_digest = self.transport.model_digest(model, self.timeout_seconds)
        except Exception as error:
            code = classify_error(error)
            latency_ms = max(0, int((self.clock() - started) * 1000))
            self._record(
                model=model,
                model_digest=None,
                agent=agent,
                context=context,
                usage=MockUsage(input_tokens=0, output_tokens=0),
                retries=0,
                latency_ms=latency_ms,
                prompt_version=prompt_version,
                schema_version=schema_version,
                context_hash=context_hash,
                fallback_from=fallback_from,
                status="INCONCLUSIVE",
                error_code=code.value,
            )
            raise ProviderInconclusive(code, 1) from None

        cache_key = self._cache_key(
            model_digest=model_digest,
            prompt_version=prompt_version,
            schema_version=schema_version,
            context_hash=context_hash,
        )
        if self.cache is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                try:
                    output = output_model.model_validate(cached["output"])
                except (KeyError, TypeError, ValidationError):
                    output = None
                if output is not None:
                    latency_ms = max(0, int((self.clock() - started) * 1000))
                    usage = MockUsage(input_tokens=0, output_tokens=0)
                    self._record(
                        model=model,
                        model_digest=model_digest,
                        agent=agent,
                        context=context,
                        usage=usage,
                        retries=0,
                        latency_ms=latency_ms,
                        prompt_version=prompt_version,
                        schema_version=schema_version,
                        context_hash=context_hash,
                        fallback_from=fallback_from,
                        status="CACHE_HIT",
                    )
                    return ProviderResponse(
                        output=output,
                        usage=usage,
                        provider=self.name,
                        model=model,
                        model_digest=model_digest,
                        latency_ms=latency_ms,
                        cache_hit=True,
                    )

        last_code = ErrorCode.UNKNOWN
        last_response: dict[str, Any] = {}
        attempts = 0
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            attempts = attempt
            try:
                response = self._request(
                    model=model,
                    route=route,
                    prompt=prompt,
                    payload=payload,
                    schema=schema,
                )
                last_response = response
                content = response.get("message", {}).get("content")
                if not isinstance(content, str):
                    raise SchemaResponseError("Ollama structured content was absent")
                try:
                    output = output_model.model_validate_json(content)
                except ValidationError:
                    raise SchemaResponseError("Ollama output did not match schema") from None
                usage = self._usage(response)
                latency_ms = max(0, int((self.clock() - started) * 1000))
                self._record(
                    model=model,
                    model_digest=model_digest,
                    agent=agent,
                    context=context,
                    usage=usage,
                    retries=attempt - 1,
                    latency_ms=latency_ms,
                    prompt_version=prompt_version,
                    schema_version=schema_version,
                    context_hash=context_hash,
                    fallback_from=fallback_from,
                    response=response,
                    status="SUCCESS" if fallback_from is None else "FALLBACK_SUCCESS",
                )
                if self.cache is not None:
                    self.cache.put(
                        cache_key,
                        {
                            "output": output.model_dump(mode="json"),
                            "model": model,
                            "model_digest": model_digest,
                            "schema_version": schema_version,
                        },
                    )
                    self.cache.save()
                return ProviderResponse(
                    output=output,
                    usage=usage,
                    provider=self.name,
                    model=model,
                    retries=attempt - 1,
                    latency_ms=latency_ms,
                    model_digest=model_digest,
                )
            except Exception as error:
                last_code = classify_error(error)
                schema_limit_reached = last_code == ErrorCode.SCHEMA_VALIDATION and attempt >= 2
                if (
                    last_code not in RETRYABLE
                    or attempt >= self.retry_policy.max_attempts
                    or schema_limit_reached
                ):
                    break
                self.sleeper(self.retry_policy.delay(attempt))

        latency_ms = max(0, int((self.clock() - started) * 1000))
        self._record(
            model=model,
            model_digest=model_digest,
            agent=agent,
            context=context,
            usage=MockUsage(input_tokens=0, output_tokens=0),
            retries=max(0, attempts - 1),
            latency_ms=latency_ms,
            prompt_version=prompt_version,
            schema_version=schema_version,
            context_hash=context_hash,
            fallback_from=fallback_from,
            response=last_response,
            status="INCONCLUSIVE",
            error_code=last_code.value,
        )
        raise ProviderInconclusive(last_code, attempts) from None

    def _invoke(
        self,
        *,
        agent: AgentName,
        context: AgentContext,
        payload: dict[str, Any],
        output_model: type[T],
    ) -> ProviderResponse[T]:
        route = self.router.route(agent)
        prompt, prompt_version = self._prompt(agent)
        schema, schema_version = self._schema(output_model)
        verified_context = self.context_source.build(context)
        complete_payload = {
            **payload,
            "context": self._context_payload(context),
            "verified_context": verified_context,
        }
        context_hash = "sha256:" + stable_digest(complete_payload, 64).lower()
        candidates = [(route.model, None)]
        if route.fallback_model and route.fallback_model != route.model:
            candidates.append((route.fallback_model, route.model))
        last_error = ProviderInconclusive(ErrorCode.UNKNOWN, 0)
        for model, fallback_from in candidates:
            try:
                return self._invoke_model(
                    model=model,
                    fallback_from=fallback_from,
                    route=route,
                    agent=agent,
                    context=context,
                    prompt=prompt,
                    prompt_version=prompt_version,
                    schema=schema,
                    schema_version=schema_version,
                    payload=complete_payload,
                    context_hash=context_hash,
                    output_model=output_model,
                )
            except ProviderInconclusive as error:
                last_error = error
                # Invalid structured output is a semantic failure, not a model
                # availability failure. One repair attempt is allowed globally;
                # silently asking another model would weaken determinism.
                if error.code == ErrorCode.SCHEMA_VALIDATION:
                    raise error from None
        raise last_error from None

    def triage(self, context: AgentContext) -> ProviderResponse[TriageOutput]:
        return self._invoke(
            agent=AgentName.TRIAGE,
            context=context,
            payload={},
            output_model=TriageOutput,
        )

    def investigate(
        self, context: AgentContext, obligations: list[ProofObligation]
    ) -> ProviderResponse[InvestigatorOutput]:
        return self._invoke(
            agent=AgentName.INVESTIGATOR,
            context=context,
            payload={"obligations": [item.model_dump(mode="json") for item in obligations]},
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
            payload={"obligations": [item.model_dump(mode="json") for item in obligations]},
            output_model=JudgeOutput,
        )
