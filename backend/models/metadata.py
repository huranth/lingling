"""Global model metadata from models.dev.

models.dev is a freely-licensed catalog of model capabilities (modalities,
pricing, context limits). We flatten it into one lookup keyed by *bare* model id
(any ``provider/`` prefix stripped) so a model can be enriched regardless of how
the gateway exposes it. Pricing is deliberately not consulted -- it mislabels
some paid OpenCode models as free.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

import httpx

from core import config

_cache_lock = threading.Lock()
_cache: Dict[str, Dict[str, Any]] = {}
_cache_at: float = 0.0
_CACHE_TTL = 3600.0  # models.dev changes slowly; cache for an hour


def _bare(model_id: str) -> str:
    """Strip a leading ``provider/`` prefix: ``google/gemini-2.5-flash`` -> ``gemini-2.5-flash``."""
    return model_id.split("/", 1)[1] if "/" in model_id else model_id


def _load(force: bool = False) -> Dict[str, Dict[str, Any]]:
    """Build/refresh the flat ``bare_id -> metadata`` map across all providers."""
    global _cache, _cache_at
    with _cache_lock:
        if not force and _cache and (time.time() - _cache_at) < _CACHE_TTL:
            return _cache
        try:
            resp = httpx.get(
                config.MODELS_DEV_API,
                timeout=config.REQUEST_TIMEOUT,
                headers={"User-Agent": config.UPSTREAM_USER_AGENT},
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            # Network failure: keep serving stale cache if we have it, else empty.
            return _cache

        flat: Dict[str, Dict[str, Any]] = {}
        for provider_id, provider in payload.items():
            if not isinstance(provider, dict):
                continue
            for model_id, meta in (provider.get("models") or {}).items():
                meta = {**(meta or {}), "_provider": provider_id}
                # Key by both the full id and the bare id; first writer wins so a
                # provider-qualified entry is not clobbered by a generic one.
                flat.setdefault(model_id, meta)
                flat.setdefault(_bare(model_id), meta)
        _cache = flat
        _cache_at = time.time()
        return _cache


def lookup(model_id: str, prefer_provider: Optional[str] = None) -> Dict[str, Any]:
    """Return metadata for a model id (full or bare), or ``{}`` if unknown."""
    flat = _load()
    if not flat:
        return {}
    # Exact full-id match first.
    meta = flat.get(model_id)
    if meta and (not prefer_provider or meta.get("_provider") == prefer_provider):
        return meta
    # Bare-id match (strip a leading ``provider/`` prefix).
    # Skip when bare == model_id to avoid re-finding the same entry that just
    # failed the provider preference check above.
    bare = _bare(model_id)
    if bare != model_id:
        bare_meta = flat.get(bare)
        if bare_meta:
            return bare_meta
    # Fall back to the original match even if the provider didn't match, or
    # return empty when nothing was found at all.
    return meta or {}


def is_vision(meta: Dict[str, Any]) -> bool:
    if meta.get("attachment") is True:
        return True
    inputs = (meta.get("modalities") or {}).get("input") or []
    if isinstance(inputs, str):
        inputs = [inputs]
    return "image" in [str(x).lower() for x in inputs]


def is_reasoning(model_id: str, meta: Dict[str, Any]) -> bool:
    if isinstance(meta.get("reasoning"), bool):
        return meta["reasoning"]
    if meta.get("reasoning") in ("true", "yes", 1, "1"):
        return True
    return "reason" in model_id.lower()


def context_length(meta: Dict[str, Any]) -> Optional[int]:
    limit = meta.get("limit") or {}
    for key in ("context", "context_length", "context_window"):
        if limit.get(key):
            try:
                return int(limit[key])
            except (TypeError, ValueError):
                pass
    if meta.get("context_length"):
        try:
            return int(meta["context_length"])
        except (TypeError, ValueError):
            return None
    return None


def max_output(meta: Dict[str, Any]) -> Optional[int]:
    limit = meta.get("limit") or {}
    for key in ("output", "max_output", "output_tokens"):
        if limit.get(key):
            try:
                return int(limit[key])
            except (TypeError, ValueError):
                pass
    return None


def input_modalities(meta: Dict[str, Any]) -> List[str]:
    inputs = (meta.get("modalities") or {}).get("input") or []
    if isinstance(inputs, str):
        inputs = [inputs]
    return [str(x).lower() for x in inputs]


def reasoning_effort_values(meta: Dict[str, Any]) -> List[str]:
    """Effort levels a model actually honours, from models.dev.

    ``reasoning_options`` is not uniform across models (deepseek offers
    low/high/max; mimo offers none) -- the same data OpenCode's CLI shows under
    ``/variants``. Reading it matters because OpenCode answers 200 for a value a
    model doesn't implement, so it can't be probed. Empty means "send nothing".
    """
    out: List[str] = []
    for opt in meta.get("reasoning_options") or []:
        if isinstance(opt, dict) and opt.get("type") == "effort":
            for value in opt.get("values") or []:
                if isinstance(value, str) and value and value not in out:
                    out.append(value)
    return out


def reasoning_toggle(meta: Dict[str, Any]) -> bool:
    """True when thinking can be switched on/off independently of depth.

    A ``toggle`` option is what the CLI shows as "Default" alongside the effort
    levels. It is a separate control, not the weakest rung, so it is reported
    separately rather than folded into the effort list.
    """
    return any(
        isinstance(opt, dict) and opt.get("type") == "toggle"
        for opt in meta.get("reasoning_options") or []
    )


def enrich(model_id: str, prefer_provider: Optional[str] = None) -> Dict[str, Any]:
    """Return a normalized capability dict for a model id."""
    meta = lookup(model_id, prefer_provider)
    return {
        "name": meta.get("name"),
        "vision": is_vision(meta),
        "reasoning": is_reasoning(model_id, meta),
        "context_length": context_length(meta),
        "max_output": max_output(meta),
        "modalities": input_modalities(meta),
        "effort": reasoning_effort_values(meta),
        "reasoning_toggle": reasoning_toggle(meta),
        "_meta": meta,
    }
