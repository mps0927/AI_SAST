from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
import time
from types import SimpleNamespace
from typing import Any, Callable, Protocol


class GeminiConnectionError(ConnectionError):
    pass


class GeminiUnavailableError(RuntimeError):
    pass


class GeminiCallBudgetExceededError(RuntimeError):
    pass


class GeminiHTTPError(RuntimeError):
    """Sanitized HTTP failure that never retains a response body or API key."""

    def __init__(self, status_code: int, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.headers = headers or {}
        self.response = SimpleNamespace(headers=self.headers)
        super().__init__(f"Gemini HTTP request failed ({status_code})")


JsonRequester = Callable[
    [str, str, dict[str, str], dict[str, Any], float], dict[str, Any]
]


class GeminiClient(Protocol):
    def generate_content(
        self, model: str, request: dict[str, Any], timeout_seconds: float
    ) -> dict[str, Any]: ...


class GeminiTransport:
    """Minimal REST transport for Gemini generateContent.

    The key is sent only in the ``x-goog-api-key`` header. It is never placed in
    the URL, request JSON, exception text, or returned metadata.
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        requester: JsonRequester | None = None,
    ):
        if not api_key:
            raise ValueError("Gemini API key is required")
        self.endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._requester = requester or self._default_requester

    @staticmethod
    def _default_requester(
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={**headers, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            safe_headers = {
                key: value
                for key, value in error.headers.items()
                if key.lower() == "retry-after"
            }
            raise GeminiHTTPError(error.code, safe_headers) from None
        except (urllib.error.URLError, ConnectionError) as error:
            reason = getattr(error, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise TimeoutError("Gemini request timed out") from None
            raise GeminiConnectionError("Gemini is not reachable") from None
        except (TimeoutError, socket.timeout):
            raise TimeoutError("Gemini request timed out") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise GeminiUnavailableError("Gemini returned invalid JSON") from None

    def generate_content(
        self, model: str, request: dict[str, Any], timeout_seconds: float
    ) -> dict[str, Any]:
        encoded_model = urllib.parse.quote(model, safe="")
        url = f"{self.endpoint}/models/{encoded_model}:generateContent"
        return self._requester(
            "POST",
            url,
            {"x-goog-api-key": self._api_key},
            request,
            timeout_seconds,
        )


class RateLimitedGeminiClient:
    """Global request guard for RPM/RPD-safe analysis runs."""

    def __init__(
        self,
        inner: GeminiClient,
        *,
        max_calls: int,
        min_start_interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        if min_start_interval_seconds < 0:
            raise ValueError("minimum interval must be non-negative")
        self.inner = inner
        self.max_calls = max_calls
        self.min_start_interval_seconds = min_start_interval_seconds
        self.clock = clock
        self.sleeper = sleeper
        self.calls_started = 0
        self.start_times: list[float] = []

    @property
    def remaining_calls(self) -> int:
        return max(0, self.max_calls - self.calls_started)

    def generate_content(
        self, model: str, request: dict[str, Any], timeout_seconds: float
    ) -> dict[str, Any]:
        if self.calls_started >= self.max_calls:
            raise GeminiCallBudgetExceededError("Gemini call budget exhausted")
        now = self.clock()
        if self.start_times:
            remaining = self.min_start_interval_seconds - (now - self.start_times[-1])
            if remaining > 0:
                self.sleeper(remaining)
                now = self.clock()
        self.calls_started += 1
        self.start_times.append(now)
        return self.inner.generate_content(model, request, timeout_seconds)
