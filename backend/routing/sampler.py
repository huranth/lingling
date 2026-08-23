"""Post-heal multi-model sampler.

After the startup heal (and, when enough time has passed, after the daemon's
heal cycle) the pool is freshly verified against a *canary* free model: the
heal's ``probe_all`` returned a ``ProbeSummary`` whose lanes are either ``ok``
(proven alive and OpenCode-serving) or not. This module sweeps each configured
"good" model across the canary-**green** exits only and attributes each exit's
outcome, so the request path can tell two failure modes apart:

* **OpenCode-side** -- the model itself refuses (a model-side 4xx) on every
  canary-green exit while those same exits just served the canary fine.
  Retrying other exits cannot help, so the request path *fails fast* (one exit,
  then the existing per-model fallback fires) instead of churning the whole
  pool retrying IPs that cannot fix a model-side refusal.
* **per-IP burns** -- the model 429s or times out on some green exits but
  serves others. Requests for that model are routed onto its green subset so a
  burned exit for one model does not cost a user attempt.

The canary summary is *reused*, not re-run: probing a free non-reasoning model
across the pool is exactly what the heal already did, so the sampler accepts
that summary as the "which exits are alive" baseline and does not pay for it
again. A canary with zero green exits means OpenCode itself (or the whole pool)
is down -- the sampler cannot attribute anything, so it records nothing and the
request path behaves exactly as before (no cooked flags, no churn).

State is in-memory and short-lived: a result ages out after ``SAMPLER_TTL_S``
and a fresh pass overwrites it on the next heal cycle, so a model that recovers
is retried without a restart and a stale "cooked" verdict never pins a model
forever. The request path treats a model with no fresh sampler data as ``None``
-- normal pool selection -- so this is strictly additive and reverts fully when
``LINGLING_SAMPLER_ENABLED=0``.
"""

from __future__ import annotations

import threading
import time
from concurrent import futures
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional

from core import config

# Late import keeps this module importable in isolation (tests construct it
# directly and monkeypatch the probe primitive). ``warp.probe`` is light (no
# routing dependency), but importing it lazily also lets a test swap
# ``_probe_single`` before the first sweep.
_probe_single = None  # type: ignore[assignment]


def _warp_probe():
    """Resolve and memoize ``warp.probe`` (and the single-probe primitive)."""
    global _probe_single
    if _probe_single is None:
        from warp import probe as warp_probe
        _probe_single = warp_probe._probe_single
        return warp_probe
    # Already initialised: re-import to fetch the module for the lock.
    from warp import probe as warp_probe
    return warp_probe


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ModelSample:
    """One model's sampled outcome across the canary-green exits."""

    model: str
    # "ok" (every green exit served it) | "partial" (some served it, route to
    # those) | "cooked" (0 ok, model refused on >=1 green exit) | "rate_limited"
    # (0 ok, every green exit 429'd -- no usable exit, fail fast) | "unknown"
    # (0 ok but the failures were dead/mixed, so we cannot attribute).
    status: str = "unknown"
    ok_exits: List[str] = field(default_factory=list)
    rate_limited_exits: List[str] = field(default_factory=list)
    error_exits: List[str] = field(default_factory=list)      # probe_error (model-side)
    dead_exits: List[str] = field(default_factory=list)
    sampled_exits: int = 0
    canary_ok_exits: int = 0
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "status": self.status,
            "ok_exits": list(self.ok_exits),
            "rate_limited_exits": list(self.rate_limited_exits),
            "error_exits": list(self.error_exits),
            "dead_exits": list(self.dead_exits),
            "sampled_exits": self.sampled_exits,
            "canary_ok_exits": self.canary_ok_exits,
            "duration_ms": round(self.duration_ms, 1),
        }


@dataclass
class SamplerResult:
    """The whole sampler pass: one ``ModelSample`` per configured model."""

    models: List[ModelSample] = field(default_factory=list)
    canary_ok_exits: List[str] = field(default_factory=list)
    sampled_at: float = 0.0
    duration_ms: float = 0.0
    enabled: bool = True
    # "" (ran) | "disabled" | "no_canary_green" | "pool_empty" | "no_models"
    skipped_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "skipped_reason": self.skipped_reason,
            "sampled_at": self.sampled_at,
            "duration_ms": round(self.duration_ms, 1),
            "canary_ok_exits": list(self.canary_ok_exits),
            "models": [m.to_dict() for m in self.models],
        }


# ---------------------------------------------------------------------------
# Module-level state -- latest sampler result for the API and request path
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_latest: Optional[SamplerResult] = None
_last_sample_at: float = 0.0


def should_run(now: Optional[float] = None) -> bool:
    """True if the sampler is enabled and enough time has passed for a pass.

    Piggyback callers (the health daemon) gate on this so the sampler never runs
    more often than ``SAMPLER_INTERVAL_S`` even though every heal cycle offers
    the chance. The startup call ignores this (it wants a first pass now).
    """
    if not config.SAMPLER_ENABLED:
        return False
    n = now if now is not None else time.time()
    return (n - _last_sample_at) >= config.SAMPLER_INTERVAL_S


def _parse_models(override: Optional[List[str]]) -> List[str]:
    raw = list(override) if override is not None else list(config.SAMPLER_MODELS)
    out: List[str] = []
    for m in raw:
        m = (m or "").strip()
        if m and m not in out:
            out.append(m)
    return out


def _canary_ok_ids(canary_summary: Any) -> List[str]:
    """Proxy ids the canary probe considers alive (the green pool).

    A lane the heal just refreshed -- a WARP tunnel re-rolled off a burned
    exit (``_reroll_until_clean``) or a Tor lane whose route a restart just
    re-picked (``rotate_burned_tor_lanes``) -- sits in the transient ``healed``
    state, not ``ok``: the dashboard shows an orange "healed" pill so an
    operator can see a lane was freshly graduated rather than always-healthy.
    But the sampler's job is to re-probe and attribute per-(model, exit), so
    excluding ``healed`` lanes left a freshly-rotated Tor exit -- the one exit
    that bypasses WARP's per-IP burn -- out of the sweep, and ``ok_exits()``
    never learned it serves a model the request path then read as cooked.
    Sweeping ``healed`` lanes re-verifies them on the spot: one that came back
    dead is recounted dead, one that serves the model is attributed ok, and the
    dashboard's graduated "healed" UX is untouched (the probe pass that follows
    still graduates survivors to ``ok``).
    """
    if canary_summary is None:
        return []
    out: List[str] = []
    for r in getattr(canary_summary, "results", []) or []:
        if getattr(r, "status", "") in ("ok", "healed"):
            pid = getattr(r, "proxy_id", "")
            if pid and pid not in out:
                out.append(pid)
    return out


def _sampler_timeout(model: str, catalog: Any) -> float:
    """Per-probe timeout for a model. Reasoning/long-thinking models think
    before the first token even on a tiny completion, so they get the longer
    reasoning probe budget; fast models use the standard probe timeout."""
    if model in config.LONG_THINKING_MODELS:
        return float(config.PROBE_REASONING_TIMEOUT)
    lm = catalog.by_id(model) if catalog is not None else None
    if lm is not None and bool(getattr(lm, "reasoning", False)):
        return float(config.PROBE_REASONING_TIMEOUT)
    return float(config.PROBE_TIMEOUT)


def _sampler_probe_budget(model: str, catalog: Any) -> int:
    """Per-probe max_tokens for a model. Reasoning / long-thinking models reject
    a tiny token budget (muse-spark returns HTTP 400 at max_tokens=5 -- its
    reasoning cannot fit), which the sampler misreads as a model-side refusal and
    false-cooks: every green exit probe-errors, so a healthy model gets pinned
    cooked. Give them a real probe budget; fast models stay cheap (a liveness
    ping, not a generation). Mirrors _sampler_timeout's reasoning detection so
    the timeout and the budget agree per model."""
    if model in config.LONG_THINKING_MODELS:
        return int(config.PROBE_REASONING_MAX_TOKENS)
    lm = catalog.by_id(model) if catalog is not None else None
    if lm is not None and bool(getattr(lm, "reasoning", False)):
        return int(config.PROBE_REASONING_MAX_TOKENS)
    return int(config.PROBE_MAX_TOKENS)


def _sweep_model(
    model: str,
    green: List[Any],
    base_url: str,
    timeout: float,
    log: Any,
    max_tokens: int = 5,
) -> Dict[str, str]:
    """One model across the green exits, in parallel under a per-lane cap.

    Returns ``{proxy_id: status}`` for the green exits that were probed. Uses
    the same raw ``_probe_single`` the heal probe uses (same OpenCode UA, same
    SOCKS5 liveness pre-check that bounds httpx's un-timed handshake), so a
    verdict here is directly comparable to the canary's verdict per exit.
    """
    _warp_probe()
    probe_single = _probe_single  # memoized by _warp_probe
    workers = max(1, min(len(green), max(1, config.PROBE_CONCURRENCY)))
    cap = (
        timeout + config.PROBE_SOCKS_TIMEOUT
        + config.PROBE_TRACE_TIMEOUT + config.PROBE_CAP_SLACK
    )
    out: Dict[str, str] = {}
    ex = futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ll-sampler")
    try:
        pending = [
            (px, ex.submit(probe_single, px.url, px.id, model, base_url, timeout, max_tokens))
            for px in green
        ]
        for px, fut in pending:
            try:
                r = fut.result(timeout=cap)
                out[px.id] = getattr(r, "status", "dead")
            except futures.TimeoutError:
                out[px.id] = "dead"
                log("sampler: %s -- %s capped at %.0fs", model, px.id, cap)
            except Exception as exc:  # noqa: BLE001
                out[px.id] = "dead"
                log("sampler: %s -- %s worker error: %s", model, px.id, exc)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return out


def _classify(model: str, statuses: Dict[str, str], canary_ok: List[str]) -> ModelSample:
    """Turn per-exit statuses into a ModelSample with an attributed verdict."""
    started = time.time()
    ok, rl, err, dead = [], [], [], []
    for pid in sorted(set(canary_ok)):
        st = statuses.get(pid)
        if st == "ok":
            ok.append(pid)
        elif st == "rate_limited":
            rl.append(pid)
        elif st == "probe_error":
            err.append(pid)
        else:
            dead.append(pid)  # dead / missing / unknown

    sampled = sum(1 for pid in canary_ok if pid in statuses)

    if ok:
        status = "ok" if not (rl or err or dead) else "partial"
    elif sampled == 0:
        # Nothing was actually probed (green exits left the pool). Cannot
        # attribute -- leave the request path untouched.
        status = "unknown"
    elif err:
        # A model-side refusal (4xx other than 429) on a canary-green exit is
        # definitive: the exit is alive and serving the canary, it is *this
        # model* OpenCode refused. Every green exit refusing = OpenCode-side.
        status = "cooked"
    elif rl and not dead:
        # No ok and no model-side refusal, but every sampled green exit 429'd:
        # every usable IP is burned for this model right now. No useful exit ->
        # fail fast (the request would 429 on the first pick anyway).
        status = "rate_limited"
    else:
        # All dead, or rl+dead mixed without a model-side refusal: can't tell a
        # model outage from a flaky-under-load pool, so stay neutral.
        status = "unknown"

    return ModelSample(
        model=model,
        status=status,
        ok_exits=ok,
        rate_limited_exits=rl,
        error_exits=err,
        dead_exits=dead,
        sampled_exits=sampled,
        canary_ok_exits=len(canary_ok),
        duration_ms=(time.time() - started) * 1000.0,
    )


def sample_models(
    proxy_pool: Any,
    catalog: Any,
    canary_summary: Any,
    log: Any = lambda *a, **k: None,
    *,
    models: Optional[List[str]] = None,
    base_url: Optional[str] = None,
    timeout: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> Optional[SamplerResult]:
    """Sweep each configured model across the canary-green exits.

    ``canary_summary`` is the ``ProbeSummary`` the heal just produced (shared
    with this module so the sampler does not re-probe the canary). Returns the
    stored ``SamplerResult`` (also kept module-level for the request path and
    ``/api/sampler``); ``None`` only when the sampler is disabled.

    The whole pass is serialized under ``warp.probe._probe_lock`` so it never
    interleaves with a concurrent ``probe_all`` (the startup WARP probe, the
    Tor-join snapshot, the dashboard Probe button). A sampler pass is itself a
    burst of OpenCode traffic; holding the lock keeps the two from doubling up
    on the same exits.
    """
    if not config.SAMPLER_ENABLED:
        _store(SamplerResult(enabled=False, sampled_at=time.time(), skipped_reason="disabled"))
        return None

    started = time.time()
    wprobe = _warp_probe()
    target_models = _parse_models(models)
    if not target_models:
        res = SamplerResult(
            enabled=True, sampled_at=started, skipped_reason="no_models",
        )
        _store(res, started)
        log("sampler: no models configured — nothing to sample, skipping")
        return res

    canary_ok = _canary_ok_ids(canary_summary)
    if not canary_ok:
        res = SamplerResult(
            enabled=True, sampled_at=started, skipped_reason="no_canary_green",
        )
        _store(res, started)
        log("sampler: canary had zero green exits — skipping. opencode-side outage, nothing to attribute.")
        return res

    # Resolve the green proxies' live URLs from the pool (a canary-green id may
    # have left the pool between the heal and now; such an id is simply dropped).
    pool_snapshot = proxy_pool.get_all_proxies() if proxy_pool is not None else []
    green = [p for p in pool_snapshot if p.id in set(canary_ok)]
    if not green:
        res = SamplerResult(
            enabled=True, canary_ok_exits=sorted(canary_ok),
            sampled_at=started, skipped_reason="pool_empty",
        )
        _store(res, started)
        log("sampler: canary-green exits already left the pool — skipping")
        return res

    base = (base_url or config.OPENCODE_BASE_URL).rstrip("/")

    # Serialize with probe_all: a sampler pass is a burst of OpenCode traffic
    # and must not interleave with a concurrent heal probe.
    model_samples: List[ModelSample] = []
    with wprobe._probe_lock:
        for model in target_models:
            if catalog is not None and catalog.by_id(model) is None:
                # Not in the live catalog: probing it would 400 on every exit
                # and read as a false "cooked". Record it as unknown (neutral
                # to the request path) so the dashboard still lists it.
                model_samples.append(ModelSample(
                    model=model, status="unknown", canary_ok_exits=len(canary_ok),
                ))
                log("sampler: %-34s isn't in the catalog — skipped", model)
                continue
            to = float(timeout) if timeout is not None else _sampler_timeout(model, catalog)
            budget = int(max_tokens) if max_tokens is not None else _sampler_probe_budget(model, catalog)
            statuses = _sweep_model(model, green, base, to, log, max_tokens=budget)
            ms = _classify(model, statuses, canary_ok)
            model_samples.append(ms)
            log(
                "sampler: %-34s %s -- %d ok, %d rl, %d err, %d dead (of %d green, %.0fms)",
                model, ms.status, len(ms.ok_exits), len(ms.rate_limited_exits),
                len(ms.error_exits), len(ms.dead_exits), len(green), ms.duration_ms,
            )

    res = SamplerResult(
        models=model_samples,
        canary_ok_exits=sorted(canary_ok),
        sampled_at=started,
        duration_ms=(time.time() - started) * 1000.0,
        enabled=True,
    )
    _store(res, started)
    log(
        "sampler: done — %d models across %d green exits, %.0fms. the verdicts are in.",
        len(model_samples), len(green), res.duration_ms,
    )
    return res


# ---------------------------------------------------------------------------
# Request-path accessors + dashboard
# ---------------------------------------------------------------------------


def _current(now: Optional[float] = None) -> Optional[SamplerResult]:
    """The latest result if it is enabled and within its TTL, else None."""
    with _lock:
        res = _latest
    if res is None or not res.enabled:
        return None
    n = now if now is not None else time.time()
    if config.SAMPLER_TTL_S > 0 and (n - res.sampled_at) > config.SAMPLER_TTL_S:
        return None
    return res


def ok_exits(model_id: str) -> Optional[FrozenSet[str]]:
    """Exits proven ok for ``model_id``, consulted by the executor.

    * ``None`` -- no fresh sampler data (disabled, model not sampled, status
      ``unknown``, or aged out): the caller uses normal pool selection.
    * empty frozenset -- the model is cooked / every green exit burned: the
      caller should *fail fast* (cap the per-egress attempt budget) instead of
      churning the pool, since retrying other exits cannot fix a model-side
      refusal. It still picks normally (one shot) in case the verdict is stale.
    * non-empty frozenset -- route the request onto these exits.
    """
    res = _current()
    if res is None:
        return None
    for ms in res.models:
        if ms.model == model_id:
            if ms.status == "unknown":
                return None
            return frozenset(ms.ok_exits)
    return None  # model not in the sampler list -> unchanged


def is_cooked(model_id: str) -> bool:
    """True if the sampler most recently proved this model has zero usable exits."""
    ok = ok_exits(model_id)
    return ok is not None and not ok


def cooked_models(now: Optional[float] = None) -> FrozenSet[str]:
    """Ids the sampler most recently proved have no usable exit right now.

    A model is in this set when the sampler ran and found zero ok exits on the
    canary-green pool AND the failures were a model-side refusal (``cooked``) or
    every green exit 429-ing (``rate_limited``) -- both mean forwarding to it
    cannot succeed, so a request whose primary model just failed should *not*
    fall back to *another* such model. ``unknown`` (missing, or all-dead where
    we cannot attribute) is excluded -- the dispatcher may still try it and let
    the executor's normal failover decide.
    """
    res = _current(now)
    if res is None:
        return frozenset()
    return frozenset(
        ms.model for ms in res.models if ms.status in ("cooked", "rate_limited")
    )


def latest() -> Optional[Dict[str, Any]]:
    """The latest sampler result for ``/api/sampler`` (or None)."""
    with _lock:
        return _latest.to_dict() if _latest is not None else None


def _store(result: SamplerResult, started: float = 0.0) -> None:
    global _latest, _last_sample_at  # noqa: PLW0603
    with _lock:
        _latest = result
        _last_sample_at = started or result.sampled_at


def reset_for_test() -> None:
    """Clear module state so each unit test owns a fresh registry."""
    global _latest, _last_sample_at  # noqa: PLW0603
    global _probe_single
    with _lock:
        _latest = None
        _last_sample_at = 0.0
        _probe_single = None
