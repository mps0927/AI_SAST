from __future__ import annotations

import random
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable


class ErrorCode(StrEnum):
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    CONNECTION = "CONNECTION"
    SERVER = "SERVER"
    SCHEMA_VALIDATION = "SCHEMA_VALIDATION"
    OUTPUT_MISSING = "OUTPUT_MISSING"
    MAX_TOKENS = "MAX_TOKENS"
    JSON_INVALID = "JSON_INVALID"
    WIRE_SCHEMA_INVALID = "WIRE_SCHEMA_INVALID"
    DOMAIN_RULE_INVALID = "DOMAIN_RULE_INVALID"
    CALL_CAP_EXHAUSTED = "CALL_CAP_EXHAUSTED"
    AUTHENTICATION = "AUTHENTICATION"
    PERMISSION = "PERMISSION"
    BAD_REQUEST = "BAD_REQUEST"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


RETRYABLE = {
    ErrorCode.TIMEOUT,
    ErrorCode.RATE_LIMIT,
    ErrorCode.CONNECTION,
    ErrorCode.SERVER,
    ErrorCode.SCHEMA_VALIDATION,
}


class SchemaResponseError(Exception):
    pass


class ProviderInconclusive(RuntimeError):
    """Safe terminal failure. It intentionally never retains the provider exception."""

    def __init__(self, code: ErrorCode, attempts: int):
        self.code = code
        self.attempts = attempts
        self.verdict = "INCONCLUSIVE"
        super().__init__(f"provider call ended INCONCLUSIVE ({code.value}, attempts={attempts})")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0
    jitter_ratio: float = 0.20

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays must be non-negative")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")

    def delay(self, retry_number: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(max(retry_after, 0.0), self.max_delay_seconds)
        base = min(self.base_delay_seconds * (2 ** max(retry_number - 1, 0)), self.max_delay_seconds)
        return max(0.0, base + random.uniform(-base * self.jitter_ratio, base * self.jitter_ratio))


def classify_error(error: Exception) -> ErrorCode:
    if isinstance(error, SchemaResponseError):
        return ErrorCode.SCHEMA_VALIDATION
    name = error.__class__.__name__
    by_name = {
        "GeminiOutputMissingError": ErrorCode.OUTPUT_MISSING,
        "GeminiMaxTokensError": ErrorCode.MAX_TOKENS,
        "GeminiJsonInvalidError": ErrorCode.JSON_INVALID,
        "GeminiWireSchemaError": ErrorCode.WIRE_SCHEMA_INVALID,
        "GeminiDomainRuleError": ErrorCode.DOMAIN_RULE_INVALID,
        "APITimeoutError": ErrorCode.TIMEOUT,
        "TimeoutError": ErrorCode.TIMEOUT,
        "RateLimitError": ErrorCode.RATE_LIMIT,
        "APIConnectionError": ErrorCode.CONNECTION,
        "ConnectionError": ErrorCode.CONNECTION,
        "URLError": ErrorCode.CONNECTION,
        "OllamaConnectionError": ErrorCode.CONNECTION,
        "OllamaUnavailableError": ErrorCode.PROVIDER_UNAVAILABLE,
        "GeminiConnectionError": ErrorCode.CONNECTION,
        "GeminiUnavailableError": ErrorCode.PROVIDER_UNAVAILABLE,
        "GeminiCallBudgetExceededError": ErrorCode.CALL_CAP_EXHAUSTED,
        "InternalServerError": ErrorCode.SERVER,
        "AuthenticationError": ErrorCode.AUTHENTICATION,
        "PermissionDeniedError": ErrorCode.PERMISSION,
        "BadRequestError": ErrorCode.BAD_REQUEST,
    }
    if name in by_name:
        return by_name[name]
    status = getattr(error, "status_code", None)
    if status == 429:
        return ErrorCode.RATE_LIMIT
    if isinstance(status, int) and status >= 500:
        return ErrorCode.SERVER
    if status in {401}:
        return ErrorCode.AUTHENTICATION
    if status in {403}:
        return ErrorCode.PERMISSION
    if isinstance(status, int) and 400 <= status < 500:
        return ErrorCode.BAD_REQUEST
    return ErrorCode.UNKNOWN


def retry_after_seconds(error: Exception) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


Sleeper = Callable[[float], None]
DEFAULT_SLEEPER: Sleeper = time.sleep
