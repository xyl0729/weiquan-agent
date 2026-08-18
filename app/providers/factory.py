from __future__ import annotations

import httpx

from app.agent.errors import ProviderConfigurationError
from app.config import Settings
from app.execution.bounded import BoundedExecutor
from app.health.service import ProviderHealthService
from app.providers.base import LLMProvider
from app.providers.deepseek import DeepSeekProvider
from app.providers.fake import FakeProvider


def create_provider(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
    executor: BoundedExecutor | None = None,
    health_service: ProviderHealthService | None = None,
) -> LLMProvider:
    if settings.deployment_mode == "production":
        if settings.deepseek_api_key is None:
            raise ProviderConfigurationError("未配置 DEEPSEEK_API_KEY")
        return DeepSeekProvider(
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            base_url=settings.deepseek_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
            total_timeout_seconds=settings.llm_total_timeout_seconds,
            max_retries=settings.llm_max_retries,
            client=client,
            executor=executor,
            health_service=health_service,
        )
    if settings.llm_provider == "fake":
        return FakeProvider()
    if settings.llm_provider == "deepseek":
        if settings.deepseek_api_key is None:
            raise ProviderConfigurationError("未配置 DEEPSEEK_API_KEY")
        return DeepSeekProvider(
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            base_url=settings.deepseek_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
            total_timeout_seconds=settings.llm_total_timeout_seconds,
            max_retries=settings.llm_max_retries,
            client=client,
            executor=executor,
            health_service=health_service,
        )
    raise ProviderConfigurationError("未知 LLM_PROVIDER")
