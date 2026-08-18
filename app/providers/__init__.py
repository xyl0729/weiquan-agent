"""LLM provider adapters."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.providers.base import LLMProvider
    from app.providers.catalog import (
        ProviderCatalog,
        ProviderCatalogEntry,
        ProviderResolver,
    )
    from app.providers.deepseek import DeepSeekProvider
    from app.providers.fake import FakeProvider
    from app.providers.factory import create_provider

__all__ = [
    "DeepSeekProvider",
    "FakeProvider",
    "LLMProvider",
    "ProviderCatalog",
    "ProviderCatalogEntry",
    "ProviderResolver",
    "create_provider",
]

_EXPORTS = {
    "LLMProvider": ("app.providers.base", "LLMProvider"),
    "ProviderCatalog": ("app.providers.catalog", "ProviderCatalog"),
    "ProviderCatalogEntry": (
        "app.providers.catalog",
        "ProviderCatalogEntry",
    ),
    "ProviderResolver": ("app.providers.catalog", "ProviderResolver"),
    "DeepSeekProvider": ("app.providers.deepseek", "DeepSeekProvider"),
    "FakeProvider": ("app.providers.fake", "FakeProvider"),
    "create_provider": ("app.providers.factory", "create_provider"),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
