"""Start-up + periodic reconcile of catalog burn-state against OpenCode Zen.

For every model that is currently burned (or blacklisted), the reconciler asks
OpenCode's live ``/models`` endpoint whether the model is still on the menu.
If it is, a tiny in-band probe is sent through OpenCode -- routed through the
same proxy pool the live executor uses -- to confirm Zen will actually serve
it. The proxy pool coverage is what allows the probe to disambiguate
"upstream is broken" (every proxy gets the same 5xx) from "this one egress
IP is rate-limited" (other proxies return 200). Outcomes:

* Model absent from Zen's live ``/models`` -- authoritative: keep burned.
* Model present and any probe returns 200 -- Zen serves it again -- un-burn,
  reset ``blacklist_hits``.
* Model present and every probe returned 400/404/422 (model-class) --
  Zen claims but refuses: keep burned, increment ``blacklist_hits``, and
  trip ``blacklisted`` once the count crosses ``config.BURN_BLACKLIST_HITS``.
* Model present and every probe returned 5xx (model-side upstream is
  broken, not the egress -- the same proxies served other models moments
  ago) -- treat as a non-transient model-broken signal: keep burned,
  increment ``blacklist_hits`` so a stuck 500 eventually trips
  ``blacklisted`` and stops soaking bandwidth on retries.
* Model present and probes returned a mix of transient (429) and other
  errors (a single hot IP got rate-limited mid-pool, others fine) -- the
  model is reachable; don't bump. We only treat "uniform across the pool"
  as evidence of model failure.
* Model present and all probes are transport (599) -- pool / SOCKS layer
  is sick; transient, do not bump.

Probes are keyless (OpenCode's free tier is keyless per
``OpenCodeProvider.requires_key``) and use a single-token ping so the
egress budget is microscopic. The reconciler runs once synchronously
inside ``lifespan`` so the very first user request sees a reconciled
catalog, then again every ``config.RECONCILE_INTERVAL_S``.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Dict, List, Optional

from core import config
from models.catalog import UnifiedCatalog
from providers.base import Provider, UpstreamError  # noqa: F401
from providers.proxy_pool import ProxyPool

_log = logging.getLogger("uvicorn.error")


# Same set the executor's recycler uses for "this model is the problem, not
# the egress"; see ``routing.executor._MODEL_CLASS_ERROR_STATUSES`` and
# ``RECONCILED_FAILURE_STATUSES``.
_MODEL_CLASS_FAILURE_STATUSES = frozenset({400, 404, 422})
# Transport / upstream-blip statuses: when observed alongside clean probes
# or alongside a uniform 5xx storm, don't bump blacklist_hits on these --
# they're either a single IP getting 429'd mid-pool (transient) or the
# pool itself is sick.
_TRANSIENT_STATUSES = frozenset({408, 429})
# Once the proxy pool is exhausted and every probe returned a 5xx-side
# status, the upstream is uniformly broken for this model. We surface this
# to the reconciler as a model-class signal so it bumps blacklist_hits.
_UNIFORM_UPSTREAM_BROKEN = frozenset({500, 502, 503, 504})


# Probe signal categories returned alongside the synthesized status code.
# They let ``_classify`` distinguish "model is unmistakably broken" from
# "egress/Cloudflare is having a bad day" without losing the HTTP code.
PROBE_OK = "ok"                       # reachable; un-burn + reset hits
PROBE_TRANSIENT = "transient"         # not enough evidence to bump
PROBE_MODEL_CLASS = "model_class"     # uniform 4xx: model itself refuses
PROBE_UNIFORM_BROKEN = "uniform_5xx"  # uniform 5xx: model upstream broken
ABSENT = "absent"
RECOVERED = "recovered"
KEPT_BURNING = "kept_burning"
BLACKLISTED = "blacklisted"
SKIPPED = "skipped"

_RECONCILE_BUCKETS: Dict[str, List[str]] = {
    ABSENT: [], RECOVERED: [], KEPT_BURNING: [], BLACKLISTED: [], SKIPPED: [],
}


def _fetch_ids_from_provider(provider: Provider) -> Optional[set]:
    """Return Zen's live id set, or ``None`` on /models failure (skip the tick)."""
    try:
        return set(provider.fetch_model_ids())
    except (UpstreamError, Exception) as exc:  # noqa: BLE001 -- boundary
        _log.warning("reconcile: /models fetch blew up (%s) -- skipping this tick", exc)
        return None


def _probe_one(
    provider: Provider, model_id: str, secret: str, timeout: float,
    proxy_url: Optional[str],
) -> int:
    """Single-token ping through ``proxy_url`` (or direct); returns HTTP status."""
    try:
        provider.chat_completions(
            [{"role": "user", "content": "ping"}],
            model_id, secret=secret, timeout=timeout,
            proxy_url=proxy_url, max_tokens=1,
        )
        return 200
    except UpstreamError as exc:
        return int(exc.status_code or 599)
    except Exception as exc:  # noqa: BLE001 -- boundary
        _log.info("reconcile: probe for %s hit transport error (%s)", model_id, exc)
        return 599


def _probe_via_provider(
    provider: Provider, model_id: str, secret: str, timeout: float,
    proxy_pool: Optional[ProxyPool] = None,
) -> tuple:
    """Mirror real-live traffic: probe through every proxy in the pool,
    then synthesize a verdict that the reconciler can react to.

    Returns ``(status, signal)``:
    * ``status``: ``200`` if any proxy reaches Zen successfully; the
      representative status (``404`` for uniform 4xx, ``500`` for uniform
      upstream-broken 5xx) when every probe agreed on that category; the
      worst observed status otherwise.
    * ``signal``: one of the ``PROBE_*`` constants. Tells ``_classify``
      whether the verdict can be trusted as model-broken (PROBE_MODEL_CLASS
      / PROBE_UNIFORM_BROKEN) or whether it should be treated as transient
      (PROBE_TRANSIENT). A bare direct probe always returns PROBE_TRANSIENT
      because there's no multi-egress evidence to escalate against.

    Why iterate the proxy pool at all? A single direct probe validates a
    different egress than real chat traffic uses, so a probe-time 5xx might
    actually be a one-off Cloudflare blip on our own IP and have nothing
    to do with the model. Iterating the pool tells us whether *the model*
    is broken (every proxy agrees on the same failure) versus whether
    *the egress* is the problem (one proxy fine, another rate-limited).
    """
    # Providers that opt out of proxies (or whose pools are empty) skip the
    # fan-out and probe directly -- the legacy single-attempt path.
    use_pool = (
        proxy_pool is not None
        and len(proxy_pool) > 0
        and getattr(provider, "needs_proxy", lambda: False)()
        and not getattr(provider, "prefer_direct", lambda _mid: False)(model_id)
    )
    if not use_pool:
        status = _probe_one(provider, model_id, secret, timeout, proxy_url=None)
        # Direct (no-pool) probe: 4xx is still authoritatively model-class
        # (Zen itself rejected the model id -- that's enough to bump).
        # 5xx/429/408/599 is transient (we have no multi-egress evidence).
        if status == 200:
            signal = PROBE_OK
        elif status in _MODEL_CLASS_FAILURE_STATUSES:
            signal = PROBE_MODEL_CLASS
        else:
            signal = PROBE_TRANSIENT
        return (status, signal)

    max_attempts = min(len(proxy_pool), config.PROXY_MAX_ATTEMPTS_PER_REQUEST)
    statuses: List[int] = []
    for _ in range(max_attempts):
        proxy = proxy_pool.pick()
        if proxy is None:
            break
        status = _probe_one(
            provider, model_id, secret, timeout, proxy_url=proxy.url,
        )
        statuses.append(status)
        # Mirror the dispatcher: a 200 heals, a model-class failure ends
        # the fan-out (same model on the rest of the pool would also fail),
        # other non-200 statuses cool the proxy and try the next egress.
        if status == 200:
            proxy_pool.mark_success(proxy)
            return (200, PROBE_OK)
        proxy_pool.mark_failure(proxy, status)
        if status in _MODEL_CLASS_FAILURE_STATUSES:
            # Model itself rejected by upstream; further proxies won't help.
            break

    if not statuses:
        return (599, PROBE_TRANSIENT)  # pool empty
    if any(s == 200 for s in statuses):
        return (200, PROBE_OK)  # some proxy succeeded; shouldn't hit

    # Synthesize: if every observed status falls in the same category, that's
    # a real signal. Mixed results → conservative transient verdict.
    if all(s in _MODEL_CLASS_FAILURE_STATUSES for s in statuses):
        return (404, PROBE_MODEL_CLASS)
    if all(s in _UNIFORM_UPSTREAM_BROKEN for s in statuses):
        # Model's upstream is broken across every egress we tried;
        # surfaces a uniform-broken signal so the recycler eventually
        # hard-blacklists it instead of looping forever on transient.
        return (500, PROBE_UNIFORM_BROKEN)
    # Mixed transient / partial 5xx → conservative transient
    return (max(statuses), PROBE_TRANSIENT)


class OpenCodeReconciler:
    """Walks the recycler's burn-state against Zen's live catalog."""

    def __init__(
        self, catalog: UnifiedCatalog, provider: Provider,
        proxy_pool: Optional[ProxyPool] = None,
        secret_getter: Callable[[], str] = lambda: "",
        fetch_ids: Optional[Callable[[], Optional[set]]] = None,
        probe: Optional[Callable[[str], tuple]] = None,
    ) -> None:
        self._catalog = catalog
        # The two seams below exist so hermetic tests swap /models identity
        # and the probe result without touching a real network or subclassing
        # ``Provider``. Production callers rely on the defaults.
        self._proxy_pool = proxy_pool
        self._fetch_ids = fetch_ids or (lambda: _fetch_ids_from_provider(provider))
        self._probe = probe or (
            lambda mid: _probe_via_provider(
                provider, mid, secret_getter(),
                config.BURN_PROBE_TIMEOUT_S, proxy_pool=proxy_pool,
            )
        )
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- public API -------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="catalog-reconcile", daemon=True,
        )
        self._thread.start()
        _log.info("reconcile: daemon up (interval=%.0fs)", config.RECONCILE_INTERVAL_S)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        _log.info("reconcile: daemon stopped")

    def run_once(self) -> Dict[str, List[str]]:
        """Walk every burned / blacklisted id, probe what Zen now serves.

        Returns ``{outcome: [model_id, ...]}``. One persisted write at the
        tail so per-model mutations don't churn fsync.
        """
        buckets: Dict[str, List[str]] = {k: [] for k in _RECONCILE_BUCKETS}
        live_ids = self._fetch_ids()
        if live_ids is None:
            return buckets

        cat = self._catalog
        with cat._lock:
            interesting_ids = [
                mid for mid, lm in cat._logical.items()
                if lm.burned or lm.blacklisted or lm.blacklist_hits
            ]

        for mid in interesting_ids:
            outcome = self._classify(mid, live_ids)
            buckets[outcome].append(mid)

        if buckets[RECOVERED] or buckets[BLACKLISTED] or buckets[KEPT_BURNING]:
            _log.info(
                "reconcile: tick recovered=%d kept_burning=%d blacklisted=%d "
                "absent=%d skipped=%d (live=%d)",
                len(buckets[RECOVERED]), len(buckets[KEPT_BURNING]),
                len(buckets[BLACKLISTED]), len(buckets[ABSENT]), len(buckets[SKIPPED]),
                len(live_ids),
            )
        cat._persist_state()
        return buckets

    # -- internals --------------------------------------------------------
    def _classify(self, model_id: str, live_ids: set) -> str:
        cat = self._catalog
        if model_id not in live_ids:
            return ABSENT

        try:
            verdict = self._probe(model_id)
            # Probe override may return a bare int (legacy seam) or a
            # 2-tuple ``(status, signal)``. Normalize: a bare int is
            # interpreted as a direct, no-pool probe -- 4xx is model-class
            # (legacy semantic), 5xx/429/599/408 is transient.
            if isinstance(verdict, tuple) and len(verdict) == 2:
                status, signal = verdict
            else:
                status = int(verdict)
                if status == 200:
                    signal = PROBE_OK
                elif status in _MODEL_CLASS_FAILURE_STATUSES:
                    signal = PROBE_MODEL_CLASS
                else:
                    signal = PROBE_TRANSIENT
        except Exception as exc:  # noqa: BLE001 -- defensive
            _log.info("reconcile: probe raised for %s (%s) -- transient", model_id, exc)
            status, signal = 599, PROBE_TRANSIENT

        if signal == PROBE_OK or status == 200:
            with cat._lock:
                lm = cat._logical.get(model_id)
                if lm is None:
                    return SKIPPED
                was_burned = lm.burned
                lm.burned = False
                lm.burned_at = 0.0
                lm.recover_after = 0.0
                lm.consecutive_failures = 0
                lm.blacklist_hits = 0
                if was_burned:
                    _log.info("reconcile: model %s is back -- un-burned", lm.id)
            return RECOVERED

        with cat._lock:
            lm = cat._logical.get(model_id)
            if lm is None:
                return SKIPPED
            if lm.blacklisted:
                # Don't re-probe a known-dead model; only ``clear_blacklist``
                # (operator) or restart restores service for it.
                return SKIPPED
            # The PROBE_MODEL_CLASS / PROBE_UNIFORM_BROKEN signals are only
            # ever returned by the fan-out probe AND only when every proxy
            # agreed on the same failure category. That consensus is the
            # multi-egress evidence that lets us rank 5xx alongside 4xx
            # as a model-broken signal (a single direct 5xx remains
            # transient under PROBE_TRANSIENT).
            if signal in (PROBE_MODEL_CLASS, PROBE_UNIFORM_BROKEN):
                new_hits = lm.blacklist_hits + 1
            elif status in _TRANSIENT_STATUSES or status == 599:
                new_hits = 0  # transient -- forget last broken verdict
            else:
                new_hits = 0
            # ``mark_blacklisted`` consults the threshold, flips ``blacklisted``,
            # and fsyncs the manifest. Pick the outcome from its return.
            return BLACKLISTED if cat.mark_blacklisted(model_id, new_hits, status) else KEPT_BURNING

    def _run_loop(self) -> None:
        # First sync tick already ran inside lifespan; only steady-state ticks
        # happen on the daemon thread.
        while not self._stop_event.is_set():
            if self._stop_event.wait(config.RECONCILE_INTERVAL_S):
                return
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 -- daemon boundary
                _log.warning("reconcile: tick crashed (%s)", exc)
