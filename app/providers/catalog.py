from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.errors import (
    InvalidProviderError,
    ProviderUnavailableError,
)
from app.config import Settings
from app.execution.bounded import BoundedExecutor
from app.providers.base import LLMProvider
from app.providers.deepseek import DeepSeekProvider
from app.providers.fake import FakeProvider

if TYPE_CHECKING:
    from app.health.service import ProviderHealthService


class ProviderCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,49}$")
    display_name: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    available: bool
    unavailable_reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )
    offline: bool
    is_default: bool

    @model_validator(mode="after")
    def availability_is_consistent(self) -> "ProviderCatalogEntry":
        if self.available and self.unavailable_reason is not None:
            raise ValueError("可用 Provider 不得包含不可用原因")
        if not self.available and self.unavailable_reason is None:
            raise ValueError("不可用 Provider 必须包含公开原因")
        return self


class ProviderCatalog:
    def __init__(self, entries: list[ProviderCatalogEntry]) -> None:
        if not entries:
            raise ValueError("Provider 目录不能为空")
        by_id: dict[str, ProviderCatalogEntry] = {}
        for entry in entries:
            if entry.id in by_id:
                raise ValueError(f"Provider ID 重复: {entry.id}")
            by_id[entry.id] = entry
        defaults = [entry.id for entry in entries if entry.is_default]
        if len(defaults) != 1:
            raise ValueError("Provider 目录必须有且仅有一个默认项")
        self._entries = tuple(entries)
        self._by_id = MappingProxyType(by_id)
        self._default_provider_id = defaults[0]

    @classmethod
    def from_settings(cls, settings: Settings) -> "ProviderCatalog":
        deepseek_available = settings.deepseek_api_key is not None
        deepseek = ProviderCatalogEntry(
            id="deepseek",
            display_name="DeepSeek",
            model=settings.deepseek_model,
            available=deepseek_available,
            unavailable_reason=(
                None
                if deepseek_available
                else "服务端尚未配置 DeepSeek"
            ),
            offline=False,
            is_default=(
                settings.deployment_mode == "production"
                or settings.llm_provider == "deepseek"
            ),
        )
        if settings.deployment_mode == "production":
            return cls([deepseek])
        return cls(
            [
                ProviderCatalogEntry(
                    id="fake",
                    display_name="离线演示",
                    model=FakeProvider.model,
                    available=True,
                    offline=True,
                    is_default=settings.llm_provider == "fake",
                ),
                deepseek,
            ]
        )

    @property
    def entries(self) -> tuple[ProviderCatalogEntry, ...]:
        return self._entries

    @property
    def public_entries(self) -> tuple[ProviderCatalogEntry, ...]:
        deepseek = self.get("deepseek")
        if deepseek is None:
            return ()
        return (
            deepseek.model_copy(update={"is_default": True}),
        )

    @property
    def default_provider_id(self) -> str:
        return self._default_provider_id

    def get(self, provider_id: str) -> ProviderCatalogEntry | None:
        return self._by_id.get(provider_id)


class ProviderResolver:
    def __init__(
        self,
        catalog: ProviderCatalog,
        providers: Mapping[str, LLMProvider],
    ) -> None:
        self.catalog = catalog
        self._providers = MappingProxyType(dict(providers))
        missing = {
            entry.id
            for entry in catalog.entries
            if entry.available and entry.id not in self._providers
        }
        if missing:
            raise ValueError(
                "可用 Provider 缺少实现: " + ", ".join(sorted(missing))
            )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        executor: BoundedExecutor | None = None,
        health_service: ProviderHealthService | None = None,
    ) -> "ProviderResolver":
        catalog = ProviderCatalog.from_settings(settings)
        providers: dict[str, LLMProvider] = {}
        if settings.deployment_mode != "production":
            providers["fake"] = FakeProvider()
        if settings.deepseek_api_key is not None:
            providers["deepseek"] = DeepSeekProvider(
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
        return cls(catalog, providers)

    def resolve(self, provider_id: str | None = None) -> LLMProvider:
        selected = (
            self.catalog.default_provider_id
            if provider_id is None
            else provider_id.strip()
        )
        entry = self.catalog.get(selected)
        if entry is None:
            raise InvalidProviderError()
        if not entry.available:
            raise ProviderUnavailableError(
                entry.unavailable_reason or "所选模型服务当前不可用"
            )
        provider = self._providers.get(selected)
        if provider is None:
            raise ProviderUnavailableError()
        return provider
