"""Load-balanced egress proxy pool with exponential cooldown.

OpenCode rate-limits its free tier by connecting IP, not by key, so requests are
routed through a pool of egress proxies and a proxy is cooled on
rate-limit/auth/server errors -- exactly like the key pool cools a burned key.

Selection is *proactive* load balancing:

* :meth:`pick` returns the least-recently-loaded available proxy (by a decaying
  ``window_load``), spreading traffic evenly from the first request.
* :meth:`pick_sticky` pins a conversation to one exit IP. Off by default
  (``LINGLING_PROXY_STICKY=0``): it defeats the per-IP purpose, and affinity,
  when needed, is assigned by load and remembered rather than by hashing the
  session id (``hash(str)`` is randomised per process, so it wasn't even stable).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core import config

# Half-life (seconds) for the rolling load window. A request counts for ~half
# its weight after this long. Two minutes = "load over the last few minutes"
# rather than "load since process start" -- an idle proxy recovers fast.
_WINDOW_HALF_LIFE_S = 120.0

# Cap on remembered session -> proxy assignments. Only consulted when sticky
# sessions are switched on; bounded because session ids are unbounded.
_MAX_SESSIONS = 2048


def _redact(url: str) -> str:
    """Strip credentials from a proxy URL for safe display."""
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1) if "://" in url else ("", url)
    host = rest.split("@", 1)[1]
    return f"{scheme}://{host}" if scheme else host


@dataclass
class Proxy:
    """A single egress proxy URL. Never exposed with credentials via the API."""

    id: str
    url: str
    label: str = ""
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    total_requests: int = 0          # lifetime (stats only)
    total_429: int = 0               # lifetime 429s (stats only)
    # Rolling load window -- the effective weight of every request decays by
    # half every _WINDOW_HALF_LIFE_S. This is what "least loaded" selects on.
    window_load: float = 0.0
    last_used_ts: float = 0.0

    def in_cooldown(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) < self.cooldown_until

    def cooldown_remaining(self, now: Optional[float] = None) -> float:
        return max(0.0, self.cooldown_until - (now or time.time()))

    def decayed_load(self, now: float) -> float:
        """window_load decayed to *now* based on time since last_used_ts."""
        if self.last_used_ts <= 0:
            return 0.0
        elapsed = now - self.last_used_ts
        if elapsed <= 0:
            return self.window_load
        return self.window_load * (0.5 ** (elapsed / _WINDOW_HALF_LIFE_S))

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
            "window_load": round(self.decayed_load(now), 2),
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
        # 'proxy-3'. get_by_id/remove return the first match, so the WARP health
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
        removed = False
        with self._lock:
            for i, px in enumerate(self.proxies):
                if px.id == proxy_id:
                    self.proxies.pop(i)
                    removed = True
                    break
        if removed:
            # The proxy is gone; its pooled connections are pointlessly warm and
            # could hold a dead tunnel. Drop them so no later request reuses one.
            self._invalidate_connection_pool(proxy_id)
        return removed

    def set_url(self, proxy_id: str, url: str) -> bool:
        """Repoint a proxy at a new URL, under the lock.

        The WARP health daemon calls this after a port migration. It used to
        assign ``px.url`` directly on the object returned by ``get_by_id``, which
        is the one field the executor reads while building an httpx client -- so
        the write raced a request and could send it through a stale port.
        """
        with self._lock:
            for px in self.proxies:
                if px.id == proxy_id:
                    px.url = url
                    break
            else:
                return False
        # A port change means the old tunnel is gone; pooled clients still
        # pointed at the old port must not be reused.
        self._invalidate_connection_pool(proxy_id)
        return True

    @staticmethod
    def _invalidate_connection_pool(proxy_id: str) -> None:
        """Best-effort drop of a proxy's pooled httpx connections."""
        try:
            from providers.connection_pool import get_connection_pool
            get_connection_pool().invalidate(proxy_id)
        except Exception:  # noqa: BLE001
            pass

    # -- selection ---------------------------------------------------------
    def pick(self) -> Optional[Proxy]:
        """Pick the **least-loaded available** proxy.

        Among all proxies not in cooldown, returns the one with the smallest
        decayed ``window_load`` (the one that has done the least work in the last
        few minutes). Ties are broken by the rotating cursor so equal-load
        proxies still alternate. Returns ``None`` only when the pool is empty.

        If *every* proxy is in cooldown, the one closest to becoming available is
        returned so callers can decide whether to wait or fail fast.
        """
        with self._lock:
            return self._pick_locked(time.time())

    def _pick_locked(self, now: float) -> Optional[Proxy]:
        """The body of :meth:`pick`, for callers that already hold the lock.

        Single pass, but preserves the round-robin tie-break: when several
        proxies sit at the same (minimum) load -- the common case when the pool
        is idle or every exit just cooled down -- the cursor advances so
        concurrent requests spread across the tied exits instead of all piling
        onto the first one. Without that, a burst of simultaneous requests
        would burn one WARP IP's quota while the rest sat idle.
        """
        if not self.proxies:
            return None

        best: List[Proxy] = []
        best_load = float('inf')
        soonest_cooling: Optional[Proxy] = None
        min_cooldown = float('inf')

        for px in self.proxies:
            if not px.in_cooldown(now):
                load = px.decayed_load(now)
                if load < best_load:
                    best_load = load
                    best = [px]
                elif load == best_load:
                    best.append(px)
            else:
                remaining = px.cooldown_remaining(now)
                if remaining < min_cooldown:
                    min_cooldown = remaining
                    soonest_cooling = px

        if best:
            if len(best) > 1:
                self._cursor = (self._cursor + 1) % len(best)
                return best[self._cursor % len(best)]
            return best[0]

        # All in cooldown: return soonest
        return soonest_cooling

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
    def mark_success(self, proxy: Proxy) -> None:
        with self._lock:
            proxy.consecutive_failures = 0
            proxy.cooldown_until = 0.0
            proxy.total_requests += 1
            now = time.time()
            proxy.window_load = proxy.decayed_load(now) + 1.0
            proxy.last_used_ts = now

    def mark_failure(self, proxy: Proxy, status_code: int) -> float:
        """Apply cooldown backoff for rate-limit / auth / server failures."""
        with self._lock:
            proxy.total_requests += 1
            now = time.time()
            proxy.window_load = proxy.decayed_load(now) + 1.0
            proxy.last_used_ts = now
            if status_code == 429:
                proxy.total_429 += 1
            # 404 is deliberately excluded: the executor treats it as a hard,
            # non-retryable failure, so cooling this exit would bench a healthy IP
            # for a problem no other IP would fix.
            if status_code not in (401, 403, 429, 500, 502, 503, 504):
                return 0.0
            proxy.consecutive_failures += 1
            base_s = config.PROXY_COOLDOWN_BASE_MS / 1000.0
            max_s = config.PROXY_COOLDOWN_MAX_MS / 1000.0
            delay = min(max_s, base_s * (2 ** (proxy.consecutive_failures - 1)))
            proxy.cooldown_until = now + delay
            return delay

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

