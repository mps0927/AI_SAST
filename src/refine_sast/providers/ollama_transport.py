from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Callable, Protocol


class OllamaConnectionError(ConnectionError):
    pass


class OllamaUnavailableError(RuntimeError):
    pass


class OllamaHTTPError(RuntimeError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Ollama HTTP request failed ({status_code})")


JsonRequester = Callable[[str, str, dict[str, Any] | None, float], dict[str, Any]]


class OllamaClient(Protocol):
    def chat(self, request: dict[str, Any], timeout_seconds: float) -> dict[str, Any]: ...

    def model_digest(self, model: str, timeout_seconds: float) -> str: ...

    def unload(self, model: str, timeout_seconds: float) -> None: ...


class OllamaTransport:
    """Minimal stdlib HTTP transport for the local Ollama API."""

    def __init__(self, endpoint: str, requester: JsonRequester | None = None):
        self.endpoint = endpoint.rstrip("/")
        self._requester = requester or self._default_requester
        self._digests: dict[str, str] = {}

    @staticmethod
    def _default_requester(
        method: str,
        url: str,
        payload: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise OllamaHTTPError(error.code) from None
        except (urllib.error.URLError, ConnectionError) as error:
            reason = getattr(error, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise TimeoutError("Ollama request timed out") from None
            raise OllamaConnectionError("Ollama is not reachable") from None
        except (TimeoutError, socket.timeout):
            raise TimeoutError("Ollama request timed out") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise OllamaUnavailableError("Ollama returned invalid JSON") from None

    def chat(self, request: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        return self._requester("POST", f"{self.endpoint}/api/chat", request, timeout_seconds)

    def model_digest(self, model: str, timeout_seconds: float) -> str:
        if model in self._digests:
            return self._digests[model]
        response = self._requester("GET", f"{self.endpoint}/api/tags", None, timeout_seconds)
        for item in response.get("models", []):
            if item.get("name") == model or item.get("model") == model:
                digest = str(item.get("digest", ""))
                if not digest:
                    break
                self._digests[model] = digest
                return digest
        error = OllamaHTTPError(404)
        raise error

    def unload(self, model: str, timeout_seconds: float) -> None:
        self._requester(
            "POST",
            f"{self.endpoint}/api/generate",
            {"model": model, "prompt": "", "keep_alive": 0, "stream": False},
            timeout_seconds,
        )
