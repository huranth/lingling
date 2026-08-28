"""Honest, minimal egress pool. No bullshit load windows.

OpenCode free tier is generous: ~30-100 req per model per IP before 429 (verified
live). The pool's job is to rotate on real 429, not fake "rate-limited after 2-3
req" chatter. Picks the fastest healthy proxy (EWMA latency, else round-robin).

Picking algorithm (in this order, refreshed by `_pick_locked`):

1. Drop anything in cooldown; if every proxy cools, fall through to the soonest.
2. Find the lowest `_avg_latency`; neighbours within `_LATENCY_BAND_MS` are
   "competitively fast" and picked between by load (so the dispatcher can hold
   a fast lane back when an even-faster one already exists).
3. Each candidate's effective load = `decayed_load(now)` plus
   `_ACTIVE_STREAMS_WEIGHT × active_streams.active(pid)` -- the live con-
   currency contribution from `providers.active_streams`. Past-window
   request count alone doesn't tell us "this lane is currently carrying a
   stream," and stacking 3 in-flight streams on the same fast lane was
   what burned the per-IP free-tier quota under the 3-CLI stress run.
4. Among ties at the lowest effective load, round-robin so fresh-boot
   (all-zeros) spreads instead of always taking the head of the list.
"""

from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core import config
from providers.active_streams import active_streams

_WINDOW_HALF_LIFE_S = 120.0
_MAX_SESSIONS = 2048
# Latency band (ms) inside which load decides instead of raw latency. See
# ``_pick_locked`` for why concurrent streams must spread across lanes.
_LATENCY_BAND_MS = 300.0
# Multiplier from "currently-carrying-an-open-stream" to "an equivalent
# decayed_load tick". A lane with 1 in-flight stream therefore reads as
# ~1.5x past-window-loaded as one with no streams. Sized > 1 so a
# single inflight event already nudges a slow-but-idle lane past a
# fast-but-busy one; < 2 so two inflight doesn't *completely* shadow
# one extra inflight (the difference between 1 and 2 streams still
# shows up). Tunable via env for stress runs where the load/stream
# ratio shifts.
_ACTIVE_STREAMS_WEIGHT = 1.5
# Picker algorithm. Defaults to P2C (Power-of-Two-Choices) since the
# research review confirmed Netflix's Zuul approach (multi-factor
# scoring + linear decay + choice-of-2) is exactly the right shape for
# a 10-lane cohort: O(log log N) ≈ 1.7 streams of max-load gap even
# under poison-pill bursts, computed in O(1) per pick. Setting
# ``LINGLING_LOAD_BALANCER_ALGO=rr`` falls back to the legacy
# latency-band + ties-broken-by-round-robin path, which is preferable
# only if the random sampler is somehow a regression (e.g. a future
# pseudorandom-checked-in-by-bug scenario). Tested in
# ``test_unit_proxy_pool_pick_uses_power_of_two_choices``.
_P2C_MIN_POOL = 4  # below this size, P2C doesn't sample two distinct lanes well
_LOAD_BALANCER_ALGO = os.getenv("LINGLING_LOAD_BALANCER_ALGO", "p2c").lower()


def _redact(url: str) -> str:
    """Strip credentials from a proxy URL for safe display."""
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1) if "://" in url else ("", url)
    host = rest.split("@", 1)[1]
    return f"{scheme}://{host}" if scheme else host


@dataclass
class Proxy:
    """One egress proxy. Honest stats, latency + load."""

    id: str
    url: str
    label: str = ""
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    total_requests: int = 0
    total_429: int = 0
    window_load: float = 0.0
    _avg_latency: Optional[float] = None
    last_used_ts: float = 0.0

    def in_cooldown(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) < self.cooldown_until

    def cooldown_remaining(self, now: Optional[float] = None) -> float:
        return max(0.0, self.cooldown_until - (now or time.time()))

    def decayed_load(self, now: float) -> float:
        if self.last_used_ts <= 0:
            return 0.0
        elapsed = now - self.last_used_ts
        if elapsed <= 0:
            return self.window_load
        return self.window_load * (0.5 ** (elapsed / _WINDOW_HALF_LIFE_S))

    def effective_load(self, now: float, active_weight: float = _ACTIVE_STREAMS_WEIGHT) -> float:
        """Picker score: decayed_load + active penalty.

        ``active_streams.active(self.id)`` reads the live stream count under
        its own lock (the only other place the pool reads this is ``status``
        below). The pool's own lock is not held here -- both the picker and
        the status endpoint call ``effective_load`` outside any lock, the
        only writers (the executor's inc/dec) go through ``active_streams``
        directly, and the integer read there is short enough that an
        off-by-one either at the call boundary is impossible (the
        transitional value is the right answer for "right now").
        """
        return self.decayed_load(now) + active_weight * active_streams.active(self.id)

    def status(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "id": self.id,
            "label": self.label or self.id,
            "url": _redact(self.url),
            "in_cooldown": self.in_cooldown(now),
            "cooldown_remaining_s": round(self.cooldown_remaining(now), 1),
            "consecutive_failures": self.consecutive_failures,
            "total_requests": self.total_requests,
            "total_429": self.total_429,
            "avg_latency_ms": round(self._avg_latency, 1) if self._avg_latency is not None else None,
            "window_load": round(self.decayed_load(now), 2),
            # Live stream count, read through ``active_streams`` (a single
            # dict lookup under its own lock). Surfaced so the dashboard's
            # chip can render "2 streams" alongside the decayed-load cell
            # and the operator can distinguish a lane that's just busy
            # from a cooling one.
            "active_streams": active_streams.active(self.id),
        }


class ProxyPool:
    """Round-robin egress proxy pool with exponential cooldown backoff.

    Mirrors :class:`providers.key_pool.KeyPool` so cooldown semantics are
    consistent across the key and proxy rotation axes.
    """

    def __init__(self, proxies: Optional[List[Proxy]] = None) -> None:
        self.proxies: List[Proxy] = proxies or []
        self._lock = threading.Lock()
        self._cursor = 0  # rotating index so pick() advances through the pool
        # session id -> proxy id, for pick_sticky. Insertion-ordered so the
        # oldest assignments are the ones dropped when it fills up.
        self._sessions: Dict[str, str] = {}
        # Monotonic counter for generated ids. Deriving them from len(proxies)
        # meant remove-then-add reused a live id: removing proxy-2 from three
        # proxies left ['proxy-1','proxy-3'], and the next add() was also
        # 'proxy-3'. get_by_id/remove return the first match, so the health
        # daemon could then heal or dump the wrong exit.
        self._next_id = len(self.proxies) + 1

    def _fresh_id(self) -> str:
        """A pool-unique generated id, immune to removals."""
        while True:
            candidate = f"proxy-{self._next_id}"
            self._next_id += 1
            if all(px.id != candidate for px in self.proxies):
                return candidate

    @classmethod
    def from_list(cls, items: Optional[List[Any]]) -> "ProxyPool":
        """Build a pool from a list of strings or dicts.

        Accepted dict keys for the URL: ``url``, ``proxy``, ``server``.
        Optional ``id`` and ``label``. Bare strings are treated as URLs.
        """
        proxies: List[Proxy] = []
        for i, item in enumerate(items or []):
            if isinstance(item, str):
                item = {"url": item}
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("proxy") or item.get("server")
            if not url:
                continue
            proxies.append(
                Proxy(
                    id=item.get("id") or f"proxy-{i + 1}",
                    url=url,
                    label=item.get("label", ""),
                )
            )
        return cls(proxies)

    # -- mutation ----------------------------------------------------------
    def add(self, url: str, label: str = "", proxy_id: str = "") -> Proxy:
        with self._lock:
            px = Proxy(
                id=proxy_id or self._fresh_id(),
                url=url,
                label=label,
            )
            self.proxies.append(px)
            return px

    def remove(self, proxy_id: str) -> bool:
        with self._lock:
            for i, px in enumerate(self.proxies):
                if px.id == proxy_id:
                    self.proxies.pop(i)
                    return True
            return False

    def set_url(self, proxy_id: str, url: str) -> bool:
        """Repoint a proxy at a new URL, under the lock.

        The health daemon calls this after a port migration. It used to
        assign ``px.url`` directly on the object returned by ``get_by_id``, which
        is the one field the executor reads while building an httpx client -- so
        the write raced a request and could send it through a stale port.
        """
        with self._lock:
            for px in self.proxies:
                if px.id == proxy_id:
                    px.url = url
                    return True
            return False

    # -- selection ---------------------------------------------------------
    def pick(self) -> Optional[Proxy]:
        """Pick the fastest healthy proxy, load as tie-breaker."""
        with self._lock:
            return self._pick_locked(time.time())

    def _pick_locked(self, now: float) -> Optional[Proxy]:
        if not self.proxies:
            return None
        available = [px for px in self.proxies if not px.in_cooldown(now)]
        if not available:
            return min(self.proxies, key=lambda p: p.cooldown_until)

        def _lat(px: Proxy) -> float:
            return px._avg_latency if px._avg_latency is not None else 0.0

        # Power-of-Two-Choices (Netflix Zuul's "Choice-of-2") is the canonical
        # path when the cohort is big enough for sampling to mean anything. We
        # sample from all available lanes (NOT a latency-banded subset) because
        # the band collapse -- one fast lane inside, three mid-latency lanes
        # outside -- was the bug that pinned every pick to the single fastest
        # exit under the live stress run with 6/10 lanes 429'd. With
        # ``_ACTIVE_STREAMS_WEIGHT x active_streams(pid)`` baked into
        # ``effective_load`` the score already penalises busy lanes; P2C's two-
        # sample random draw is what stops the picker deterministically picking
        # the head of the latency-sorted list every time.
        if _LOAD_BALANCER_ALGO == "p2c" and len(available) >= _P2C_MIN_POOL:
            a, b = random.sample(available, 2)
            return a if a.effective_load(now) <= b.effective_load(now) else b

        # Legacy path: latency-band shortlist, tie-broken by effective load +
        # round-robin. Used when ``LINGLING_LOAD_BALANCER_ALGO=rr`` pins the
        # deterministic path (the rotation / sticky-load tests exercise this).
        # Also retained as the fallback for small pools where two random
        # samples often hit the same lane.
        best_lat = min(_lat(px) for px in available)
        banded = [px for px in available if _lat(px) <= best_lat + _LATENCY_BAND_MS]
        if len(banded) > 1:
            min_load = min(px.effective_load(now) for px in banded)
            equal = [px for px in banded if px.effective_load(now) == min_load]
            if len(equal) > 1:
                self._cursor = (self._cursor + 1) % len(equal)
                return equal[self._cursor % len(equal)]
            return equal[0]
        return min(available, key=_lat)

    def pick_sticky(self, session_id: str) -> Optional[Proxy]:
        """Pick a proxy for a session id, reusing the previous choice.

        The first request for a session gets the least-loaded proxy via
        :meth:`pick`; that choice is remembered so later turns keep the same exit
        IP. If the remembered proxy is gone or in cooldown, a fresh :meth:`pick`
        replaces it.

        The earlier implementation chose by ``hash(session_id) % len(proxies)``.
        That was wrong twice over: it ignored load, so a session could be pinned
        to the busiest proxy in the pool, and ``hash(str)`` is salted per process,
        so the "deterministic" mapping changed on every restart. Assigning by load
        and remembering the result gives real affinity *and* real balance.

        An empty session id falls through to :meth:`pick`, so anonymous
        back-to-back requests rotate.
        """
        if not session_id:
            return self.pick()
        with self._lock:
            if not self.proxies:
                return None
            now = time.time()
            known = self._sessions.get(session_id)
            if known is not None:
                for px in self.proxies:
                    if px.id == known and not px.in_cooldown(now):
                        return px
            # Unknown session, or its proxy is gone/cooling: assign the least
            # loaded one now and remember it.
            chosen = self._pick_locked(now)
            if chosen is not None:
                self._remember(session_id, chosen.id)
            return chosen

    def _remember(self, session_id: str, proxy_id: str) -> None:
        """Record a session's proxy, bounding the map so it cannot grow forever.

        Called with the lock held. One entry per conversation is tiny, but a
        long-lived gateway sees unbounded distinct session ids, so the oldest
        assignments are dropped once the map is full -- a session that outlives
        its entry simply gets re-assigned by load.
        """
        if len(self._sessions) >= _MAX_SESSIONS:
            for stale in list(self._sessions)[: len(self._sessions) // 2]:
                del self._sessions[stale]
        self._sessions[session_id] = proxy_id

    def time_until_available(self) -> Optional[float]:
        """Seconds until some exit leaves cooldown, or None when the pool is empty.

        ``0.0`` means an exit is usable right now. A positive value is how long
        the soonest one still needs, which is what lets an exhausted request wait
        for capacity instead of failing (see :mod:`routing.parking`).
        """
        with self._lock:
            if not self.proxies:
                return None
            now = time.time()
            return min(px.cooldown_remaining(now) for px in self.proxies)

    # -- feedback ----------------------------------------------------------
    def mark_success(self, proxy: Proxy, latency_ms: Optional[float] = None) -> None:
        with self._lock:
            proxy.consecutive_failures = 0
            proxy.cooldown_until = 0.0
            proxy.total_requests += 1
            now = time.time()
            proxy.window_load = proxy.decayed_load(now) + 1.0
            proxy.last_used_ts = now
            if latency_ms is not None:
                alpha = 0.3
                if proxy._avg_latency is None:
                    proxy._avg_latency = latency_ms
                else:
                    proxy._avg_latency = alpha * latency_ms + (1 - alpha) * proxy._avg_latency

    def mark_failure(self, proxy: Proxy, status_code: int) -> float:
        """Honest cooldown: 429 is real rate-limit (long), others short."""
        with self._lock:
            proxy.total_requests += 1
            now = time.time()
            proxy.window_load = proxy.decayed_load(now) + 1.0
            proxy.last_used_ts = now
            if status_code == 429:
                proxy.total_429 += 1
                proxy.consecutive_failures += 1
                base_s = config.PROXY_COOLDOWN_BASE_MS / 1000.0
                max_s = config.PROXY_COOLDOWN_MAX_MS / 1000.0
                delay = min(max_s, base_s * (2 ** (proxy.consecutive_failures - 1)))
                proxy.cooldown_until = now + delay
                return delay
            if status_code in (500, 502, 503, 504):
                proxy.cooldown_until = now + 2.0
                return 2.0
            if status_code not in config.RECONCILED_FAILURE_STATUSES:
                return 0.0
            proxy.consecutive_failures += 1
            proxy.cooldown_until = now + 1.0
            return 1.0

    # -- lookup ------------------------------------------------------------
    def get_by_id(self, proxy_id: str) -> Optional[Proxy]:
        """Thread-safe lookup of a proxy by its id. Returns None if not found."""
        with self._lock:
            for px in self.proxies:
                if px.id == proxy_id:
                    return px
            return None

    def get_all_proxies(self) -> List[Proxy]:
        """Thread-safe snapshot of all proxies (returns a copy)."""
        with self._lock:
            return list(self.proxies)

    # -- introspection -----------------------------------------------------
    def status(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            available = sum(1 for p in self.proxies if not p.in_cooldown(now))
            return {
                "total": len(self.proxies),
                "available": available,
                "in_cooldown": len(self.proxies) - available,
                "proxies": [p.status() for p in self.proxies],
            }

    def __len__(self) -> int:
        return len(self.proxies)

