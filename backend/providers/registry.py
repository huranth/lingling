"""Provider registry: build the active providers from configuration.

OpenCode credentials come from a JSON file (see ``accounts.example.json``).
Because OpenCode rate-limits by IP, not key, the key pool is optional -- the
real countermeasure is the egress proxy pool.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from core import config
from providers.key_pool import KeyPool
from providers.proxy_pool import ProxyPool
from providers.opencode import OpenCodeProvider
from providers.base import Provider


def _load_json_list(path: Path) -> List[Any]:
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _opencode_keys() -> KeyPool:
    """Build the OpenCode key pool from the environment, then the JSON file.

    ``LINGLING_OPENCODE_KEYS`` (comma-separated) is preferred: it keeps the
    secret out of the working tree entirely. ``accounts.json`` still works for
    convenience but is gitignored, and either source is optional -- OpenCode's
    free tier is keyless, so an empty pool is the normal case.
    """
    items: List[Any] = []
    env_keys = os.getenv("LINGLING_OPENCODE_KEYS", "")
    for i, secret in enumerate(env_keys.split(",")):
        secret = secret.strip()
        if secret:
            items.append({
                "id": f"env-key-{i + 1}",
                "label": "LINGLING_OPENCODE_KEYS",
                "secret": secret,
            })
    items.extend(_load_json_list(config.OPENCODE_ACCOUNTS_FILE))
    return KeyPool.from_list(items)


def build_proxy_pool() -> ProxyPool:
    """Build the egress proxy pool from the JSON file and/or LINGLING_PROXIES env.

    Sources are merged; file entries first, then env entries. When neither is
    configured the pool is empty and Lingling connects directly (backward compatible).
    """
    items: List[Any] = list(_load_json_list(config.PROXIES_FILE))
    if config.PROXIES_ENV:
        for i, url in enumerate(config.PROXIES_ENV.split(",")):
            url = url.strip()
            if url:
                items.append({"id": f"env-proxy-{i + 1}", "label": "LINGLING_PROXIES", "url": url})
    return ProxyPool.from_list(items)


def build_providers() -> Dict[str, Provider]:
    """Construct all providers, keyed by id, in priority order."""
    providers: List[Provider] = [
        OpenCodeProvider(_opencode_keys()),
    ]
    providers.sort(key=lambda p: p.priority)
    return {p.id: p for p in providers}
