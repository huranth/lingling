"""Lingling provider package.

Exposes the concrete provider (OpenCode) and a registry that builds the
active set from configuration.
"""

from providers.key_pool import Key, KeyPool
from providers.base import (
    Provider,
    ProviderModel,
    OpenAICompatibleProvider,
    UpstreamError,
    extract_assistant_text,
    extract_usage,
)

__all__ = [
    "Key",
    "KeyPool",
    "Provider",
    "ProviderModel",
    "OpenAICompatibleProvider",
    "UpstreamError",
    "extract_assistant_text",
    "extract_usage",
]
