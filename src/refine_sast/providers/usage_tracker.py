from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..stage3_schemas import AgentName, MockUsage


class UsageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str
    provider: str
    model: str
    agent: AgentName
    batch: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)
    retry: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    prompt_version: str
    schema_version: str
    status: str
    error_code: str | None = None
    model_digest: str | None = None
    context_hash: str | None = None
    fallback_from: str | None = None
    cached_tokens_reported: bool = True
    reasoning_tokens_reported: bool = True
    usage_reported: bool = True
    load_duration_ms: int = Field(default=0, ge=0)
    prompt_eval_duration_ms: int = Field(default=0, ge=0)
    eval_duration_ms: int = Field(default=0, ge=0)
    finish_reason: str | None = None
    response_present: bool | None = None
    response_chars: int | None = Field(default=None, ge=0)
    validation_stage: str | None = None
    validation_error_paths: list[str] = Field(default_factory=list)
    validation_error_types: list[str] = Field(default_factory=list)


class UsageTracker:
    schema_version = "token-ledger-v1"
    _forbidden_keys = {
        "source", "source_code", "content", "raw", "raw_code", "prompt",
        "instructions", "api_key", "secret", "environment", "env",
    }

    def __init__(self, run_id: str, output_path: Path):
        self.run_id = run_id
        self.output_path = output_path
        self.records: list[UsageRecord] = []

    def record(
        self,
        *,
        provider: str,
        model: str,
        agent: AgentName,
        batch: str,
        usage: MockUsage,
        retry: int,
        latency_ms: int,
        prompt_version: str,
        schema_version: str,
        status: str = "SUCCESS",
        error_code: str | None = None,
        model_digest: str | None = None,
        context_hash: str | None = None,
        fallback_from: str | None = None,
        cached_tokens_reported: bool = True,
        reasoning_tokens_reported: bool = True,
        usage_reported: bool = True,
        load_duration_ms: int = 0,
        prompt_eval_duration_ms: int = 0,
        eval_duration_ms: int = 0,
        finish_reason: str | None = None,
        response_present: bool | None = None,
        response_chars: int | None = None,
        validation_stage: str | None = None,
        validation_error_paths: list[str] | None = None,
        validation_error_types: list[str] | None = None,
    ) -> UsageRecord:
        item = UsageRecord(
            call_id=f"CALL-{len(self.records) + 1:04d}",
            provider=provider,
            model=model,
            agent=agent,
            batch=batch,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            retry=retry,
            latency_ms=latency_ms,
            prompt_version=prompt_version,
            schema_version=schema_version,
            status=status,
            error_code=error_code,
            model_digest=model_digest,
            context_hash=context_hash,
            fallback_from=fallback_from,
            cached_tokens_reported=cached_tokens_reported,
            reasoning_tokens_reported=reasoning_tokens_reported,
            usage_reported=usage_reported,
            load_duration_ms=load_duration_ms,
            prompt_eval_duration_ms=prompt_eval_duration_ms,
            eval_duration_ms=eval_duration_ms,
            finish_reason=finish_reason,
            response_present=response_present,
            response_chars=response_chars,
            validation_stage=validation_stage,
            validation_error_paths=validation_error_paths or [],
            validation_error_types=validation_error_types or [],
        )
        self.records.append(item)
        self.write()
        return item

    def document(self) -> dict[str, object]:
        calls = [item.model_dump(mode="json") for item in self.records]
        totals = {
            field: sum(int(item[field]) for item in calls)
            for field in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "retry", "latency_ms")
        }
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "contains_source": False,
            "contains_prompt_body": False,
            "calls": calls,
            "totals": totals,
        }

    @classmethod
    def assert_safe(cls, value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).lower() in cls._forbidden_keys:
                    raise ValueError(f"forbidden token ledger field: {key}")
                cls.assert_safe(nested)
        elif isinstance(value, list):
            for nested in value:
                cls.assert_safe(nested)

    def write(self) -> None:
        document = self.document()
        self.assert_safe(document)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
