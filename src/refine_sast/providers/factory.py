from __future__ import annotations

from pathlib import Path
from typing import Any

from ..cache import ContentHashCache
from .base import LLMProvider
from .gemini_transport import GeminiClient
from .local_capabilities import LocalModelCapabilityRegistry
from .local_context import LocalContextSource
from .model_router import ModelRouter
from .ollama_transport import OllamaClient, OllamaTransport
from .retry import RetryPolicy
from .usage_tracker import UsageTracker


class ProviderFactory:
    """Create only the provider selected by the routing profile."""

    @staticmethod
    def create(
        *,
        workspace: Path,
        router: ModelRouter,
        tracker: UsageTracker,
        context_source: LocalContextSource | None = None,
        transport: OllamaClient | None = None,
        cache: ContentHashCache | None = None,
        retry_policy: RetryPolicy | None = None,
        openai_client: Any | None = None,
        gemini_transport: GeminiClient | None = None,
    ) -> LLMProvider:
        if router.provider == "ollama-local":
            if context_source is None:
                raise ValueError("local provider requires a verified context source")
            from .local_llm import LocalLLMProvider

            endpoint = router.profile.endpoint
            if not endpoint:
                raise ValueError("local provider profile requires an endpoint")
            local_transport = transport or OllamaTransport(endpoint)
            capabilities = LocalModelCapabilityRegistry(
                workspace / "config" / "local-model-capabilities.json"
            )
            return LocalLLMProvider(
                workspace=workspace,
                router=router,
                tracker=tracker,
                transport=local_transport,
                context_source=context_source,
                capabilities=capabilities,
                cache=cache,
                retry_policy=retry_policy,
            )
        if router.provider == "openai-responses":
            from .openai_responses import OpenAIResponsesProvider

            return OpenAIResponsesProvider(
                workspace=workspace,
                router=router,
                tracker=tracker,
                client=openai_client,
                retry_policy=retry_policy,
            )
        if router.provider == "gemini-generate-content":
            if context_source is None:
                raise ValueError("Gemini provider requires a verified context source")
            from .gemini_provider import GeminiProvider

            return GeminiProvider(
                workspace=workspace,
                router=router,
                tracker=tracker,
                context_source=context_source,
                transport=gemini_transport,
                cache=cache,
                retry_policy=retry_policy,
            )
        raise ValueError(f"unsupported provider: {router.provider}")
