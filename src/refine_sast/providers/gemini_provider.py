from __future__ import annotations

import json
import os
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
from .gemini_transport import GeminiClient, GeminiTransport
from .gemini_wire import wire_output_model, wire_to_domain
from .local_context import LocalContextSource
from .model_router import ModelRouter, RouteConfig
from .retry import (
    DEFAULT_SLEEPER,
    ErrorCode,
    ProviderInconclusive,
    RetryPolicy,
    Sleeper,
    classify_error,
    retry_after_seconds,
)
from .usage_tracker import UsageTracker


T = TypeVar("T", bound=BaseModel)


class GeminiResponseDiagnosticError(Exception):
    def __init__(
        self,
        message: str,
        *,
        stage: str,
        paths: list[str] | None = None,
        error_types: list[str] | None = None,
    ):
        self.stage = stage
        self.paths = paths or []
        self.error_types = error_types or []
        super().__init__(message)


class GeminiOutputMissingError(GeminiResponseDiagnosticError):
    pass


class GeminiMaxTokensError(GeminiResponseDiagnosticError):
    pass


class GeminiJsonInvalidError(GeminiResponseDiagnosticError):
    pass


class GeminiWireSchemaError(GeminiResponseDiagnosticError):
    pass


class GeminiDomainRuleError(GeminiResponseDiagnosticError):
    pass


class GeminiProvider:
    """Provider-neutral Agent implementation for Gemini generateContent.

    Live transport creation and environment access are lazy. Tests inject a
    network-free transport. Verified source is transient and is never persisted
    in the response cache or token ledger.
    """

    name = "gemini-generate-content"

    def __init__(
        self,
        *,
        workspace: Path,
        router: ModelRouter,
        tracker: UsageTracker,
        context_source: LocalContextSource,
        transport: GeminiClient | None = None,
        cache: ContentHashCache | None = None,
        retry_policy: RetryPolicy | None = None,
        sleeper: Sleeper = DEFAULT_SLEEPER,
        clock: Callable[[], float] = time.perf_counter,
        timeout_seconds: float = 60.0,
    ):
        if router.provider != self.name:
            raise ValueError(f"routing profile provider must be {self.name}")
        if not router.profile.endpoint:
            raise ValueError("Gemini profile requires an endpoint")
        if transport is None and any(
            router.route(agent).model.startswith("UNASSIGNED_")
            for agent in AgentName
        ):
            raise ValueError("Gemini recovery model requires explicit approval")
        self.workspace = workspace.resolve()
        self.router = router
        self.tracker = tracker
        self.context_source = context_source
        self._transport = transport
        self.cache = cache
        self.retry_policy = retry_policy or RetryPolicy(max_attempts=2)
        self.sleeper = sleeper
        self.clock = clock
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def api_key_present() -> bool:
        return bool(os.environ.get("GEMINI_API_KEY"))

    def live_available(self) -> bool:
        return self._transport is not None or self.api_key_present()

    def set_role_packet(self, agent: AgentName, packet: dict[str, Any]) -> None:
        setter = getattr(self.context_source, "set_role_packet", None)
        if setter is None:
            return
        setter(agent, packet)

    def _get_transport(self) -> GeminiClient:
        if self._transport is not None:
            return self._transport
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ProviderInconclusive(ErrorCode.PROVIDER_UNAVAILABLE, 0)
        self._transport = GeminiTransport(self.router.profile.endpoint or "", api_key)
        return self._transport

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
        usage = response.get("usageMetadata", {})
        if not isinstance(usage, dict):
            usage = {}
        return MockUsage(
            input_tokens=int(usage.get("promptTokenCount", 0) or 0),
            output_tokens=int(usage.get("candidatesTokenCount", 0) or 0),
            cached_tokens=int(usage.get("cachedContentTokenCount", 0) or 0),
            reasoning_tokens=int(usage.get("thoughtsTokenCount", 0) or 0),
        )

    @staticmethod
    def _finish_reason(response: dict[str, Any]) -> str | None:
        candidates = response.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return None
        value = candidates[0].get("finishReason")
        return str(value) if value is not None else None

    @staticmethod
    def _response_text(response: dict[str, Any]) -> str:
        candidates = response.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise GeminiOutputMissingError(
                "Gemini structured response was absent", stage="OUTPUT"
            )
        if str(candidates[0].get("finishReason") or "").upper() == "MAX_TOKENS":
            raise GeminiMaxTokensError(
                "Gemini reached the configured output limit", stage="OUTPUT"
            )
        content = candidates[0].get("content", {})
        parts = content.get("parts", []) if isinstance(content, dict) else []
        texts = [part.get("text") for part in parts if isinstance(part, dict)]
        if not texts or not all(isinstance(item, str) for item in texts):
            raise GeminiOutputMissingError(
                "Gemini structured response text was absent", stage="OUTPUT"
            )
        result = "".join(texts)
        if not result:
            raise GeminiOutputMissingError(
                "Gemini structured response text was empty", stage="OUTPUT"
            )
        return result

    @staticmethod
    def _request(
        *,
        route: RouteConfig,
        prompt: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        generation_config: dict[str, Any] = {
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
            "maxOutputTokens": route.max_output_tokens,
            "thinkingConfig": {
                "thinkingLevel": {
                    "none": "minimal",
                    "low": "low",
                    "medium": "medium",
                    "high": "high",
                    "xhigh": "high",
                    "max": "high",
                }[route.reasoning_effort]
            },
        }
        if route.temperature is not None:
            generation_config["temperature"] = route.temperature
        return {
            "systemInstruction": {"parts": [{"text": prompt}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": json.dumps(
                                payload, ensure_ascii=False, sort_keys=True
                            )
                        }
                    ],
                }
            ],
            "generationConfig": generation_config,
        }

    def _cache_key(
        self,
        *,
        model: str,
        prompt_version: str,
        schema_version: str,
        context_hash: str,
    ) -> str:
        value = {
            "provider": self.name,
            "model": model,
            "prompt_version": prompt_version,
            "schema_version": schema_version,
            "context_hash": context_hash,
        }
        return "llm-response-v1|" + stable_digest(value, 64).lower()

    def _record(
        self,
        *,
        model: str,
        model_version: str | None,
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
        usage_reported: bool = True,
        finish_reason: str | None = None,
        response_present: bool | None = None,
        response_chars: int | None = None,
        validation_stage: str | None = None,
        validation_error_paths: list[str] | None = None,
        validation_error_types: list[str] | None = None,
    ) -> None:
        self.tracker.record(
            provider=self.name,
            model=model,
            model_digest=model_version,
            agent=agent,
            batch=context.batch_id,
            usage=usage,
            retry=retries,
            latency_ms=latency_ms,
            prompt_version=prompt_version,
            schema_version=schema_version,
            context_hash=context_hash,
            cached_tokens_reported=True,
            reasoning_tokens_reported=True,
            status=status,
            error_code=error_code,
            usage_reported=usage_reported,
            finish_reason=finish_reason,
            response_present=response_present,
            response_chars=response_chars,
            validation_stage=validation_stage,
            validation_error_paths=validation_error_paths,
            validation_error_types=validation_error_types,
        )

    def _invoke(
        self,
        *,
        agent: AgentName,
        context: AgentContext,
        payload: dict[str, Any],
        output_model: type[T],
        obligations: list[ProofObligation] | None = None,
        finding_id: str = "",
    ) -> ProviderResponse[T]:
        route = self.router.route(agent)
        prompt, prompt_version = self._prompt(agent)
        wire_model = wire_output_model(agent)
        schema, schema_version = self._schema(wire_model)
        complete_payload = {
            **payload,
            "context": self._context_payload(context),
            "verified_context": self.context_source.build(context),
        }
        context_hash = "sha256:" + stable_digest(complete_payload, 64).lower()
        cache_key = self._cache_key(
            model=route.model,
            prompt_version=prompt_version,
            schema_version=schema_version,
            context_hash=context_hash,
        )
        started = self.clock()

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
                        model=route.model,
                        model_version=str(cached.get("model_version") or "") or None,
                        agent=agent,
                        context=context,
                        usage=usage,
                        retries=0,
                        latency_ms=latency_ms,
                        prompt_version=prompt_version,
                        schema_version=schema_version,
                        context_hash=context_hash,
                        status="CACHE_HIT",
                    )
                    return ProviderResponse(
                        output=output,
                        usage=usage,
                        provider=self.name,
                        model=route.model,
                        model_digest=str(cached.get("model_version") or "") or None,
                        latency_ms=latency_ms,
                        cache_hit=True,
                    )

        try:
            transport = self._get_transport()
        except ProviderInconclusive as terminal:
            latency_ms = max(0, int((self.clock() - started) * 1000))
            self._record(
                model=route.model,
                model_version=None,
                agent=agent,
                context=context,
                usage=MockUsage(input_tokens=0, output_tokens=0),
                retries=0,
                latency_ms=latency_ms,
                prompt_version=prompt_version,
                schema_version=schema_version,
                context_hash=context_hash,
                status="INCONCLUSIVE",
                error_code=terminal.code.value,
                usage_reported=False,
            )
            raise terminal from None

        request = self._request(
            route=route, prompt=prompt, payload=complete_payload, schema=schema
        )
        last_code = ErrorCode.UNKNOWN
        attempts = 0
        last_usage = MockUsage(input_tokens=0, output_tokens=0)
        usage_reported = False
        last_model_version: str | None = None
        finish_reason: str | None = None
        response_present: bool | None = None
        response_chars: int | None = None
        validation_stage: str | None = None
        validation_error_paths: list[str] = []
        validation_error_types: list[str] = []
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            attempts = attempt
            try:
                response = transport.generate_content(
                    route.model, request, self.timeout_seconds
                )
                last_usage = self._usage(response)
                usage_reported = True
                last_model_version = str(response.get("modelVersion") or "") or None
                finish_reason = self._finish_reason(response)
                try:
                    response_text = self._response_text(response)
                    response_present = True
                    response_chars = len(response_text)
                    try:
                        decoded = json.loads(response_text)
                    except json.JSONDecodeError as error:
                        raise GeminiJsonInvalidError(
                            "Gemini output was not complete JSON",
                            stage="JSON",
                            paths=[f"line:{error.lineno}:column:{error.colno}"],
                            error_types=["json_invalid"],
                        ) from None
                    try:
                        wire_output = wire_model.model_validate(decoded)
                    except ValidationError as error:
                        raise GeminiWireSchemaError(
                            "Gemini output did not match the provider wire schema",
                            stage="WIRE_SCHEMA",
                            paths=[
                                ".".join(str(part) for part in item["loc"])
                                for item in error.errors(include_url=False)
                            ],
                            error_types=[
                                str(item["type"])
                                for item in error.errors(include_url=False)
                            ],
                        ) from None
                    try:
                        converted = wire_to_domain(
                            agent,
                            wire_output,
                            context=context,
                            obligations=obligations,
                            finding_id=finding_id,
                        )
                        output = output_model.model_validate(
                            converted.model_dump(mode="json")
                        )
                    except (ValidationError, ValueError) as error:
                        if isinstance(error, ValidationError):
                            details = error.errors(include_url=False)
                            paths = [
                                ".".join(str(part) for part in item["loc"])
                                for item in details
                            ]
                            types = [str(item["type"]) for item in details]
                        else:
                            paths = ["domain"]
                            types = [error.__class__.__name__]
                        raise GeminiDomainRuleError(
                            "Gemini output violated verified domain rules",
                            stage="DOMAIN_RULE",
                            paths=paths,
                            error_types=types,
                        ) from None
                except GeminiResponseDiagnosticError:
                    if response_chars is None:
                        response_present = False
                        response_chars = 0
                    raise
                usage = last_usage
                model_version = last_model_version
                latency_ms = max(0, int((self.clock() - started) * 1000))
                self._record(
                    model=route.model,
                    model_version=model_version,
                    agent=agent,
                    context=context,
                    usage=usage,
                    retries=attempt - 1,
                    latency_ms=latency_ms,
                    prompt_version=prompt_version,
                    schema_version=schema_version,
                    context_hash=context_hash,
                    status="SUCCESS",
                    finish_reason=finish_reason,
                    response_present=True,
                    response_chars=response_chars,
                )
                if self.cache is not None:
                    self.cache.put(
                        cache_key,
                        {
                            "output": output.model_dump(mode="json"),
                            "model": route.model,
                            "model_version": model_version,
                            "schema_version": schema_version,
                        },
                    )
                    self.cache.save()
                return ProviderResponse(
                    output=output,
                    usage=usage,
                    provider=self.name,
                    model=route.model,
                    retries=attempt - 1,
                    latency_ms=latency_ms,
                    model_digest=model_version,
                )
            except Exception as error:
                last_code = classify_error(error)
                if isinstance(error, GeminiResponseDiagnosticError):
                    validation_stage = error.stage
                    validation_error_paths = error.paths
                    validation_error_types = error.error_types
                gemini_retryable = {
                    ErrorCode.TIMEOUT,
                    ErrorCode.RATE_LIMIT,
                    ErrorCode.CONNECTION,
                    ErrorCode.SERVER,
                }
                if (
                    last_code not in gemini_retryable
                    or attempt >= self.retry_policy.max_attempts
                ):
                    break
                self.sleeper(
                    self.retry_policy.delay(attempt, retry_after_seconds(error))
                )

        latency_ms = max(0, int((self.clock() - started) * 1000))
        self._record(
            model=route.model,
            model_version=last_model_version,
            agent=agent,
            context=context,
            usage=last_usage,
            retries=max(0, attempts - 1),
            latency_ms=latency_ms,
            prompt_version=prompt_version,
            schema_version=schema_version,
            context_hash=context_hash,
            status="INCONCLUSIVE",
            error_code=last_code.value,
            usage_reported=usage_reported,
            finish_reason=finish_reason,
            response_present=response_present,
            response_chars=response_chars,
            validation_stage=validation_stage,
            validation_error_paths=validation_error_paths,
            validation_error_types=validation_error_types,
        )
        raise ProviderInconclusive(last_code, attempts) from None

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
            payload={
                "obligations": [item.model_dump(mode="json") for item in obligations]
            },
            output_model=InvestigatorOutput,
            obligations=obligations,
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
            obligations=obligations,
            finding_id=finding_id,
        )

    def judge(
        self, context: AgentContext, obligations: list[ProofObligation]
    ) -> ProviderResponse[JudgeOutput]:
        return self._invoke(
            agent=AgentName.JUDGE,
            context=context,
            payload={
                "obligations": [item.model_dump(mode="json") for item in obligations]
            },
            output_model=JudgeOutput,
            obligations=obligations,
        )
