"""Executor: run a chat completion with failover across proxies and keys.

Given a chosen model, tries the providers that serve it, rotating egress proxies
(and keys, if configured) on retryable errors so a burned IP or key fails over
to the next. Non-streaming gets full failover; streaming binds to the first
provider whose stream yields a chunk (mid-stream failover lives in stream_guard).
"""

from __future__ import annotations

import time
from itertools import chain
from typing import Any, Callable, Dict, FrozenSet, Generator, List, Optional, Tuple

from core import config
from providers.base import Provider, UpstreamError  # noqa: F401
from providers.proxy_pool import ProxyPool
from routing import sampler

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


def _sampler_applies(prov: Provider) -> bool:
    """Whether the post-heal sampler's verdict can steer this provider.

    The sampler probed OpenCode's free-tier endpoint, so a per-(model, exit)
    verdict describes *that* upstream. A different-upstream provider (a direct
    OpenAI key, etc.) is untouched: an OpenCode-side "cooked" verdict says
    nothing about whether OpenAI will serve the model.
    """
    if getattr(prov, "id", "") == "opencode":
        return True
    base = getattr(prov, "base_url", "")
    return bool(base) and str(base).rstrip("/") == str(config.OPENCODE_BASE_URL).rstrip("/")


def _pick_proxy(
    prov: Provider, proxy_pool: Optional[ProxyPool], session_id: str, model_id: str,
    ok_set: Optional[FrozenSet[str]] = None,
) -> Optional[Any]:
    """Pick an egress proxy for this provider attempt, or None if not applicable.

    Providers that opt out (needs_proxy() -> False), or explicitly prefer a
    direct latency path for this model, bypass the pool.
    Sticky sessions (default) pin a conversation to one proxy via the
    session id; otherwise round-robin. Returns None when the pool is empty --
    callers then connect directly (backward compatible).

    ``ok_set`` (from the post-heal sampler) routes a model onto the subset of
    exits the sampler proved serve it (per-IP burn avoidance). None = no fresh
    sampler data -> normal selection; empty set = cooked -> the caller's
    fail-fast cap governs, and the single attempt prefers a Tor lane (a fresh
    exit whose route re-picks on restart, bypassing WARP's per-IP burn) before
    falling back to normal selection.
    """
    if proxy_pool is None or len(proxy_pool) == 0:
        return None
    if not prov.needs_proxy() or prov.prefer_direct(model_id):
        return None
    if ok_set:
        return proxy_pool.pick_from(ok_set, session_id=session_id)
    if ok_set is not None and not ok_set:
        # Empty sampler set = the model is cooked / every probed exit 429-ing.
        # The sampler's green exits are WARP-dominated, and Tor lanes re-pick
        # their route on restart (a fresh IP that bypasses OpenCode's per-IP
        # burn), so the single fail-fast attempt should land on a Tor lane
        # before the request is abandoned to the dispatcher's model fallback.
        # An empty frozenset is falsy, so without this branch the cooked case
        # falls through to plain ``pick()`` over the whole pool (~77% WARP),
        # re-hits the burned WARP exit, and never reaches the Tor lanes
        # uniquely able to serve it. ``pick_kind`` returns None when no Tor lane
        # is fresh, in which case normal selection (sticky/pick) takes over.
        tor = proxy_pool.pick_kind("tor", session_id=session_id)
        if tor is not None:
            return tor
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
    connection, not the JSON body). Exposed as a named parameter (kept out of
    ``**params``) so callers -- e.g. the dispatcher in ``app.py`` -- can set it
    without it being forwarded into the upstream request body.
    """
    if not providers:
        raise NoProviderError(f"no provider serves model '{model_id}'")

    attempts: List[Dict[str, Any]] = []
    last_error: Optional[UpstreamError] = None

    # Post-heal sampler: route a model onto the exits the sampler proved serve
    # it (per-IP burn avoidance) and fail fast when it proved an OpenCode-side
    # outage, so retrying IPs cannot help and the per-model fallback fires
    # instead of churning the whole pool. The verdict is upstream-specific (see
    # ``_sampler_applies``); ``None`` = no fresh sampler data -> the executor
    # behaves exactly as before.
    ok_set = sampler.ok_exits(model_id)
    cooked = ok_set is not None and not ok_set
    fail_fast = max(1, config.SAMPLER_FAIL_FAST_ATTEMPTS)

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
            if cooked and _sampler_applies(prov) and max_proxies > fail_fast:
                max_proxies = fail_fast
            for _ in range(max_proxies):
                proxy = _pick_proxy(
                    prov, proxy_pool, session_id, model_id,
                    ok_set=ok_set if _sampler_applies(prov) else None,
                )
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
        if cooked and _sampler_applies(prov) and max_keys > fail_fast:
            max_keys = fail_fast
        key_ok_set = ok_set if _sampler_applies(prov) else None
        for _ in range(max_keys):
            key = prov.keys.pick()
            if key is None:
                break
            if key.id in tried and len(tried) >= len(prov.keys):
                break
            tried.add(key.id)
            proxy = _pick_proxy(
                prov, proxy_pool, session_id, model_id, ok_set=key_ok_set,
            )
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
    on_attempt: Optional[Callable[[str, Dict[str, Any]], None]] = None,
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

    ``on_attempt`` is an optional lifecycle callback fired per inner retry
    attempt: ``"dispatch"`` right before calling the provider (with the
    proxy picked for this attempt), ``"first_token"`` once a chunk arrives
    (so the caller can confirm the lane is live — otherwise minutes of
    silent first-chunk waiting across burned exit IPs produce *zero* log
    lines and the only summary fires after ``execute_stream`` returns),
    ``"failure"`` on a non-token outcome, with the status code so the
    caller can see *why* a burned IP burned. ``None`` skips the callbacks
    entirely (the executor's own log lines stay silent), which is the
    behavior every test exercises.
    """
    if not providers:
        raise NoProviderError(f"no provider serves model '{model_id}'")

    attempts: List[Dict[str, Any]] = []
    last_error: Optional[UpstreamError] = None
    ok_set = sampler.ok_exits(model_id)
    cooked = ok_set is not None and not ok_set
    fail_fast = max(1, config.SAMPLER_FAIL_FAST_ATTEMPTS)
    for prov in providers:
        if not prov.is_configured():
            attempts.append({"provider": prov.id, "key": None, "status": "not_configured"})
            continue
        max_attempts = (
            min(len(proxy_pool), config.PROXY_MAX_ATTEMPTS_PER_REQUEST)
            if proxy_pool and prov.needs_proxy() and not prov.prefer_direct(model_id)
            else 1
        )
        if cooked and _sampler_applies(prov) and max_attempts > fail_fast:
            max_attempts = fail_fast
        stream_ok_set = ok_set if _sampler_applies(prov) else None
        for _ in range(max_attempts):
            proxy = _pick_proxy(
                prov, proxy_pool, session_id, model_id, ok_set=stream_ok_set,
            )
            proxy_url = proxy.url if proxy else None
            key = None if not prov.requires_key() else prov.keys.pick()
            if prov.requires_key() and key is None:
                break
            attempt_n = len(attempts)
            if on_attempt is not None:
                on_attempt("dispatch", {
                    "n": attempt_n, "prov": prov.id,
                    "proxy": proxy.id if proxy else None, "model": model_id,
                })
            dispatch_t = time.time()
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
                if on_attempt is not None:
                    on_attempt("first_token", {
                        "n": attempt_n, "prov": prov.id,
                        "proxy": proxy.id if proxy else None,
                        "elapsed_ms": (time.time() - dispatch_t) * 1000.0,
                    })
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
            if on_attempt is not None:
                on_attempt("failure", {
                    "n": attempt_n, "prov": prov.id,
                    "proxy": proxy.id if proxy else None,
                    "elapsed_ms": (time.time() - dispatch_t) * 1000.0,
                    "status": failure.status_code,
                })
            if failure.status_code not in _RETRYABLE:
                raise AllFailedError(failure, attempts)
    raise AllFailedError(last_error, attempts)
