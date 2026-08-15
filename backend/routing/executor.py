"""Executor: run a chat completion with failover across proxies and keys.

Given a chosen model, tries the providers that serve it, rotating egress proxies
(and keys, if configured) on retryable errors so a burned IP or key fails over
to the next. Non-streaming gets full failover; streaming binds to the first
provider whose stream yields a chunk (mid-stream failover lives in stream_guard).
"""

from __future__ import annotations

from itertools import chain
from typing import Any, Dict, Generator, List, Optional, Tuple

from core import config
from providers.base import Provider, UpstreamError  # noqa: F401
from providers.proxy_pool import ProxyPool

# Statuses worth retrying on a different proxy/key: rate-limit, auth, and
# transient server errors.
_RETRYABLE = (401, 403, 426, 409, 410, 428, 429, 500, 502, 503, 504)


class NoProviderError(Exception):
    """No provider serves the requested model."""


class AllFailedError(Exception):
    """Every provider/key attempt failed."""

    def __init__(self, last_error: Optional[UpstreamError], attempts: List[Dict[str, Any]]) -> None:
        super().__init__(str(last_error) if last_error else "all attempts failed")
        self.last_error = last_error
        self.attempts = attempts


def _pick_proxy(
    prov: Provider, proxy_pool: Optional[ProxyPool], session_id: str, model_id: str,
) -> Optional[Any]:
    """Pick an egress proxy for this provider attempt, or None if not applicable.

    Providers that opt out (needs_proxy() -> False), or explicitly prefer a
    direct latency path for this model, bypass the pool.
    Sticky sessions (default) pin a conversation to one proxy via the
    session id; otherwise round-robin. Returns None when the pool is empty --
    callers then connect directly (backward compatible).
    """
    if proxy_pool is None or len(proxy_pool) == 0:
        return None
    if not prov.needs_proxy() or prov.prefer_direct(model_id):
        return None
    if config.PROXY_STICKY_SESSIONS and session_id:
        return proxy_pool.pick_sticky(session_id)
    return proxy_pool.pick()


def execute_nonstream(
    messages: List[Dict[str, Any]],
    model_id: str,
    providers: List[Provider],
    proxy_pool: Optional[ProxyPool] = None,
    session_id: str = "",
    timeout: Optional[float] = None,
    **params: Any,
) -> Tuple[Dict[str, Any], Provider, Any, List[Dict[str, Any]]]:
    """Run a non-streaming completion with full cross-provider failover.

    Returns ``(response, provider, key, attempts)`` on success.
    Raises ``NoProviderError`` / ``AllFailedError`` otherwise.

    For IP-limited providers (OpenCode free tier), each attempt picks an egress
    proxy from ``proxy_pool``; a 429/403 burns that proxy's IP and the next
    attempt uses a different proxy.

    ``timeout`` is the per-attempt httpx transport timeout (applies to the
    connection, not the JSON body).  Extracted from ``**params`` explicitly so
    callers (e.g. the dispatcher) can set it without it being swallowed into
    the upstream request body.
    """
    if not providers:
        raise NoProviderError(f"no provider serves model '{model_id}'")

    attempts: List[Dict[str, Any]] = []
    last_error: Optional[UpstreamError] = None

    for prov in providers:
        if not prov.is_configured():
            attempts.append({"provider": prov.id, "key": None, "status": "not_configured"})
            continue
        # Keyless provider (e.g. OpenCode Zen free tier): one attempt per proxy
        # IP. Each burned IP (429/403) cools that proxy and we retry through a
        # different egress until the pool is exhausted, then fall through to the
        # next provider that serves the model.
        if not prov.requires_key():
            max_proxies = (
                min(len(proxy_pool), config.PROXY_MAX_ATTEMPTS_PER_REQUEST)
                if proxy_pool and prov.needs_proxy() and not prov.prefer_direct(model_id)
                else 1
            )
            for _ in range(max_proxies):
                proxy = _pick_proxy(prov, proxy_pool, session_id, model_id)
                proxy_url = proxy.url if proxy else None
                try:
                    resp = prov.chat_completions(messages, model_id, "", proxy_url=proxy_url, timeout=timeout, proxy_id=proxy.id if proxy else None, **params)
                    if proxy is not None and proxy_pool is not None:
                        proxy_pool.mark_success(proxy)
                    return resp, prov, None, attempts
                except UpstreamError as exc:
                    if proxy is not None and proxy_pool is not None:
                        proxy_pool.mark_failure(proxy, exc.status_code)
                    attempts.append(
                        {"provider": prov.id, "key": None, "status": exc.status_code,
                         "proxy": proxy.id if proxy else None}
                    )
                    last_error = exc
                    if exc.status_code not in _RETRYABLE:
                        raise AllFailedError(exc, attempts)
                    continue
            continue
        tried: set = set()
        max_keys = max(1, len(prov.keys))
        for _ in range(max_keys):
            key = prov.keys.pick()
            if key is None:
                break
            if key.id in tried and len(tried) >= len(prov.keys):
                break
            tried.add(key.id)
            proxy = _pick_proxy(prov, proxy_pool, session_id, model_id)
            proxy_url = proxy.url if proxy else None
            try:
                resp = prov.chat_completions(messages, model_id, key.secret, proxy_url=proxy_url, timeout=timeout, proxy_id=proxy.id if proxy else None, **params)
                prov.keys.mark_success(key)
                if proxy is not None and proxy_pool is not None:
                    proxy_pool.mark_success(proxy)
                return resp, prov, key, attempts
            except UpstreamError as exc:
                prov.keys.mark_failure(key, exc.status_code)
                if proxy is not None and proxy_pool is not None:
                    proxy_pool.mark_failure(proxy, exc.status_code)
                attempts.append(
                    {"provider": prov.id, "key": key.id, "status": exc.status_code,
                     "proxy": proxy.id if proxy else None}
                )
                last_error = exc
                if exc.status_code not in _RETRYABLE:
                    raise AllFailedError(exc, attempts)  # non-retryable: stop now
                continue  # rotate to next key / provider

    raise AllFailedError(last_error, attempts)

def execute_stream(
    messages: List[Dict[str, Any]],
    model_id: str,
    providers: List[Provider],
    proxy_pool: Optional[ProxyPool] = None,
    session_id: str = "",
    timeout: Optional[float] = None,
    **params: Any,
) -> Tuple[Generator[bytes, None, None], Provider, Any, List[Dict[str, Any]]]:
    """Open a native stream and obtain its first chunk before replying to client.

    Waiting for one chunk is intentional: transport errors and the configured
    first-token timeout occur before FastAPI sends a 200 response, so a dead
    SOCKS proxy can be cooled and another egress tried. Once a chunk is emitted,
    HTTP cannot change status mid-stream and remaining errors simply end it.

    ``timeout`` is the time-to-first-token budget — applied to the httpx
    transport, *not* the request body (avoids the **params swallowing bug
    where a ``timeout`` kwarg went into the JSON body instead of the
    client kwargs).
    """
    if not providers:
        raise NoProviderError(f"no provider serves model '{model_id}'")

    attempts: List[Dict[str, Any]] = []
    last_error: Optional[UpstreamError] = None
    for prov in providers:
        if not prov.is_configured():
            attempts.append({"provider": prov.id, "key": None, "status": "not_configured"})
            continue
        max_attempts = (
            min(len(proxy_pool), config.PROXY_MAX_ATTEMPTS_PER_REQUEST)
            if proxy_pool and prov.needs_proxy() and not prov.prefer_direct(model_id)
            else 1
        )
        for _ in range(max_attempts):
            proxy = _pick_proxy(prov, proxy_pool, session_id, model_id)
            proxy_url = proxy.url if proxy else None
            key = None if not prov.requires_key() else prov.keys.pick()
            if prov.requires_key() and key is None:
                break
            failure: Optional[UpstreamError] = None
            try:
                stream = prov.stream_chat(
                    messages, model_id, key.secret if key is not None else "",
                    proxy_url=proxy_url, timeout=timeout,
                    proxy_id=proxy.id if proxy else None,
                    **params,
                )
                first = next(stream)
                if proxy is not None and proxy_pool is not None:
                    proxy_pool.mark_success(proxy)
                if key is not None:
                    prov.keys.mark_success(key)
                return chain((first,), stream), prov, key, attempts
            except StopIteration:
                failure = UpstreamError(502, "upstream closed stream before its first chunk", prov.id)
            except UpstreamError as exc:
                failure = exc
            assert failure is not None
            if proxy is not None and proxy_pool is not None:
                proxy_pool.mark_failure(proxy, failure.status_code)
            if key is not None:
                prov.keys.mark_failure(key, failure.status_code)
            attempts.append({
                "provider": prov.id, "key": key.id if key is not None else None,
                "status": failure.status_code, "proxy": proxy.id if proxy else None,
            })
            last_error = failure
            if failure.status_code not in _RETRYABLE:
                raise AllFailedError(failure, attempts)
    raise AllFailedError(last_error, attempts)
