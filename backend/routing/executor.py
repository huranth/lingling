"""Executor with failover across the OpenCode provider and its key pool.

Given a chosen model, the executor tries OpenCode (rotating keys if any are
configured) and rotates egress proxies on retryable errors (rate limit / auth /
server errors). This is the mechanism behind "keep trying until some free
model somewhere succeeds" -- when one proxy IP burns, the next attempt uses a
different IP.

Non-streaming requests get full failover. Streaming requests bind to the best
available provider/key up front (mid-stream failover is not attempted).
"""

from __future__ import annotations

import queue
import threading
import time
from itertools import chain
from typing import Any, Dict, Generator, List, Optional, Tuple

from core import config
from providers.active_streams import active_streams
from providers.base import Provider, UpstreamError  # noqa: F401
from providers.proxy_pool import ProxyPool

# Retryable upstream statuses: rate-limit (429), auth (401/403), and the usual
# server errors. Kept broad so a transient 5xx from the upstream doesn't fail
# the whole request when another proxy/key would succeed. This tuple is the
# single source of truth -- providers.config.RECONCILED_FAILURE_STATUSES is
# shared verbatim with ProxyPool and KeyPool mark_failure, so a status that
# retries also records a cooldown, and a model-class 404 does neither (it must
# not cool a healthy egress IP).
_RETRYABLE = config.RECONCILED_FAILURE_STATUSES

# Statuses that mean the upstream rejected the *model id itself*, not our
# connection / egress / key. Only these bump the catalog recycler; the proxy
# pool already avoids cooling an exit IP on them (since the same exit works
# fine for every other model). Each UpstreamError we see carries one of these
# status codes, so we fire ``record_model_failure`` once per failed attempt
# and let the catalog's counter decide whether to burn.
_MODEL_CLASS_ERROR_STATUSES = frozenset({400, 404, 422})


def _maybe_record_model_failure(model_id: str, status_code: int, catalog: Any) -> None:
    """Burn the catalog's recycler when upstream blames the model id.

    Called from every ``except UpstreamError`` arm in this module (non-stream
    keyless, non-stream with-key, streaming first-token wait) -- the single
    source of truth so chat / Responses / messages paths can't drift. A
    non-model-class status (rate-limit, auth, server) does nothing: those are
    the proxy pool's concern, never the catalog's.
    """
    if catalog is None or status_code not in _MODEL_CLASS_ERROR_STATUSES:
        return
    catalog.record_model_failure(model_id)


def _first_chunk_with_deadline(
    stream: Generator[bytes, None, None], deadline_s: float, provider_id: str,
) -> bytes:
    """Pull the first chunk from ``stream`` within ``deadline_s`` seconds.

    The httpx client's *read* timeout is now deliberate generous (see
    ``core.config.STREAM_READ_TIMEOUT``) so a live stream's mid-answer
    thinking pauses are not misread by httpx as a 504. But that same relaxed
    ceiling would also let a dead-but-connectable lane sit on ``next(stream)``
    for the whole read budget before the executor can rotate. The very first
    ``next()`` is therefore bounded here by the first-token budget instead.

    On expiry the reader thread is abandoned -- it is reclaimed by the
    provider's own connection timeout, the same contract ``stream_idle``
    relies on (its module docstring, "What happens to the abandoned
    upstream"). The caller treats the resulting ``UpstreamError`` like any
    other stream-open failure: ``mark_failure``, then rotate.
    """
    result: "queue.Queue[Any]" = queue.Queue(maxsize=1)

    def _pull() -> None:
        try:
            result.put(("ok", next(stream)))
        except BaseException as exc:  # noqa: BLE001 -- forward StopIteration etc.
            result.put(("err", exc))

    reader = threading.Thread(target=_pull, name="lingling-first-chunk", daemon=True)
    reader.start()
    try:
        kind, payload = result.get(timeout=deadline_s)
    except queue.Empty:
        raise UpstreamError(
            504, f"upstream first chunk took over {deadline_s:g}s", provider_id,
        )
    if kind == "err":
        raise payload
    return payload


class NoProviderError(Exception):
    """No provider serves the requested model."""


class AllFailedError(Exception):
    """Every provider/key attempt failed."""

    def __init__(self, last_error: Optional[UpstreamError], attempts: List[Dict[str, Any]]) -> None:
        super().__init__(str(last_error) if last_error else "all attempts failed")
        self.last_error = last_error
        self.attempts = attempts

    def is_model_unavailable(self) -> bool:
        """True iff every attempt returned a model-class unavailability status.

        A model is "unavailable" in the burn-recycler's sense when upstream
        itself rejects the model id -- 400 (bad request that names an unservable
        id), 404 (model not found), 422 (unprocessable). A purely egress-related
        crash (proxy timeouts, 429 IP-bans, 5xx cluster) does NOT implicate the
        model and must NOT burn it -- those are the proxy-pool's concern.

        We say "burn the model" iff every recorded attempt shares a
        model-class status, so a single mixed-attempt AllFailedError (some
        404s + some 504s) leaves the model alone: half the attempts reached a
        live upstream so the path itself isn't the problem.
        """
        statuses = {a.get("status") for a in (self.attempts or []) if a.get("status")}
        return bool(statuses) and statuses <= {400, 404, 422}


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


def _is_proxy_saturated(proxy: Any) -> bool:
    """True when an already-picked proxy is carrying enough concurrent streams
    that opening another on the same egress IP would burn the per-IP free-tier
    quota mid-stream (the 40 s firstchunk + 90 s silence pattern under stress).

    Reads ``active_streams`` cheaply (one dict lookup under its own lock).
    Generators with id="" or None are reported as not-saturated so callers
    that bypass the pool (direct egress) keep their previous behaviour.
    """
    if proxy is None or not getattr(proxy, "id", ""):
        return False
    return active_streams.active(proxy.id) >= config.PROXY_MAX_PARALLEL_STREAMS


def execute_nonstream(
    messages: List[Dict[str, Any]],
    model_id: str,
    providers: List[Provider],
    proxy_pool: Optional[ProxyPool] = None,
    session_id: str = "",
    timeout: Optional[float] = None,
    catalog: Any = None,
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
                # Wall-clock the round-trip so the picker can actually distinguish
                # lanes. Without this, ``_avg_latency`` stays None for every
                # streaming path and the load-band filter essentially degenerates
                # to "fan out by recent decayed_load only" -- i.e., exactly the
                # "everyone stacks on the same fast lane" pattern that turned a
                # 3-CLI stress run into a brownout.
                t_start = time.monotonic()
                try:
                    resp = prov.chat_completions(messages, model_id, "", proxy_url=proxy_url, timeout=timeout, **params)
                    if proxy is not None and proxy_pool is not None:
                        latency_ms = (time.monotonic() - t_start) * 1000.0
                        proxy_pool.mark_success(proxy, latency_ms=latency_ms)
                    # Model answered: reset its burn streak; un-burn if it just cleared a cooldown trial.
                    if catalog is not None:
                        catalog.record_model_success(model_id)
                    return resp, prov, None, attempts
                except UpstreamError as exc:
                    if proxy is not None and proxy_pool is not None:
                        proxy_pool.mark_failure(proxy, exc.status_code)
                    # Model-class failure (400/404/422): must reach the recycler
                    # here, not later, so a stream caller also bumps the counter
                    # even when its catch path does not call ``record_model_failure``.
                    _maybe_record_model_failure(model_id, exc.status_code, catalog)
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
            t_start = time.monotonic()
            try:
                resp = prov.chat_completions(messages, model_id, key.secret, proxy_url=proxy_url, timeout=timeout, **params)
                prov.keys.mark_success(key)
                if proxy is not None and proxy_pool is not None:
                    latency_ms = (time.monotonic() - t_start) * 1000.0
                    proxy_pool.mark_success(proxy, latency_ms=latency_ms)
                # Model answered: reset its burn streak; un-burn if it just cleared a cooldown trial.
                if catalog is not None:
                    catalog.record_model_success(model_id)
                return resp, prov, key, attempts
            except UpstreamError as exc:
                prov.keys.mark_failure(key, exc.status_code)
                if proxy is not None and proxy_pool is not None:
                    proxy_pool.mark_failure(proxy, exc.status_code)
                # Model-class failure: bump the recycler so a 400 from upstream
                # on this model id evicts it from rotation, regardless of how
                # many keys or exits the executor has tried. With-key paths
                # otherwise never reach app.py's catch-site call.
                _maybe_record_model_failure(model_id, exc.status_code, catalog)
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
    catalog: Any = None,
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
        # Per-stream saturation rejections are tracked separately from
        # ``max_attempts``: if every lane is over ``PROXY_MAX_PARALLEL_STREAMS``
        # we'd otherwise burn the whole attempt budget on saturated picks and
        # never actually send a request. Cap rejects at len(pool) attempts so
        # the loop *can* exhaust, and the AllFailedError that follows tells
        # the caller "the cohort is full, parking is your only hope" rather
        # than "we burned all our chances on busy lanes".
        saturated_rejections = 0
        for _ in range(max_attempts):
            proxy = _pick_proxy(prov, proxy_pool, session_id, model_id)
            # Pick-time cap: the chosen lane already carries
            # ``PROXY_MAX_PARALLEL_STREAMS`` live streams. Without this gate,
            # three concurrent CLI sessions could each pick the same fastest
            # lane forever (the picker doesn't categorically refuse a
            # high-active lane, only down-weights it via the load-band
            # ``_ACTIVE_STREAMS_WEIGHT```). Skip and try again -- ``saturated_rejections``
            # is bounded so the inner loop eventually returns either a free
            # lane or an honest AllFailedError.
            if proxy is not None and _is_proxy_saturated(proxy):
                saturated_rejections += 1
                if saturated_rejections >= max_attempts:
                    break
                continue
            proxy_url = proxy.url if proxy else None
            key = None if not prov.requires_key() else prov.keys.pick()
            if prov.requires_key() and key is None:
                break
            failure: Optional[UpstreamError] = None
            try:
                stream = prov.stream_chat(
                    messages, model_id, key.secret if key is not None else "",
                    proxy_url=proxy_url, timeout=timeout, **params,
                )
                t_start = time.monotonic()
                first = _first_chunk_with_deadline(
                    stream,
                    float(timeout or config.STREAM_FIRST_TOKEN_TIMEOUT),
                    prov.id,
                )
                if proxy is not None and proxy_pool is not None:
                    latency_ms = (time.monotonic() - t_start) * 1000.0
                    proxy_pool.mark_success(proxy, latency_ms=latency_ms)
                if key is not None:
                    prov.keys.mark_success(key)
                # ``first = next(stream)`` already proved the model live -- heal now
                # ahead of the returned chain (success confirmed at first token).
                if catalog is not None:
                    catalog.record_model_success(model_id)
                # Active-stream bookkeeping: the wrapped generator inc's on
                # ``mark_success`` (here -- the stream is now live and will
                # consume egress bandwidth) and dec's on whatever closed the
                # generator: clean StopIteration, mid-flight GeneratorExit
                # when the client disconnect lands, or a transport exception
                # bubbled out of ``stream_guard``. Without this the picker
                # would have no view of "this lane is currently carrying a
                # stream" and the saturation cap above would never fire.
                if proxy is not None:
                    active_streams.inc(proxy.id)
                    src = chain((first,), stream)
                    src_gen = src  # type alias for the closure below
                    def _with_cleanup(_src=src_gen, _pid=proxy.id) -> Generator[bytes, None, None]:
                        try:
                            yield from _src
                        finally:
                            active_streams.dec(_pid)
                    return _with_cleanup(), prov, key, attempts
                # Pure-direct streams (no proxy in play): the upstream bytes
                # are next'd inline, no inc/dec bookkeeping is needed -- the
                # picker doesn't see this lane at all, so the saturation
                # cap and the load-band reading are both N/A.
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
            # Model-class failure during stream-open must reach the recycler,
            # not only the non-stream catch sites -- the chat-stream AllFailed
            # catch in app.py does not call ``record_model_failure`` (a cached
            # stream that does not connect leaves no outside caller to bump).
            _maybe_record_model_failure(model_id, failure.status_code, catalog)
            attempts.append({
                "provider": prov.id, "key": key.id if key is not None else None,
                "status": failure.status_code, "proxy": proxy.id if proxy else None,
            })
            last_error = failure
            if failure.status_code not in _RETRYABLE:
                raise AllFailedError(failure, attempts)
    raise AllFailedError(last_error, attempts)
