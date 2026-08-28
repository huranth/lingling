"""In-flight request counters per proxy.

Pool entries are tripped open by *past* request rates (``window_load``,
``consecutive_failures``) which is the right signal for "I just used
this lane -- it might be cooling". But for "this lane is *currently*
busy carrying a stream" the past is noise; only the live count
matters. Without it a 3-CLI concurrent stress run stacked two streams
on the same lane, the second session's stream hit the same IP's free-
tier quota mid-`firstchunk` (40s), burned the lane's meter, and the
stream_guard retry routed through the same stack a second time --
both tries died.

The counter is intentionally small and dependency-free so the picker
can read it under its own lock without paying a holding penalty. Two
R/W call sites (the executor's stream-open / stream-close and the
parking module's saturated-lane detection) plus any read site (the
picker's score and the dashboard's mid-cell pip).

Lifecycle contract:
    inc(pid) when an upstream stream is OPEN and bytes will flow.
    dec(pid) when that generator finalises -- clean completion,
        GeneratorExit (client disconnect), mid-flight exception, the
        `finally` on the wrapped generator must reach ``dec()`` for
        *every* exit. ``finally`` on the wrapping generator gives us
        this for free: Python's generator finalisation fires the
        ``finally`` block regardless of why the consumer drops the
        generator.
    ``active(pid)`` returns the live count for one lane; ``snapshot``
    returns the full dict for ``/api/proxies`` and the chip ladder.

Entry hygiene: counts only ever go up while a stream is mid-flight,
zero out as soon as it ends (drop rather than keep zombie zeroes
around -- a 10-minute stress run otherwise leaves hundreds of dead
``pid -> 0`` rows in the dict and each ``snapshot`` call walks them).
"""

from __future__ import annotations

import threading
from typing import Dict


class ActiveStreams:
    """Per-proxy live-stream counter. Single instance; module-level singleton."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Dict[str, int] = {}

    def inc(self, proxy_id: str, n: int = 1) -> int:
        """Bump the live count for ``proxy_id``; return the new total.

        ``dec()`` MUST follow for every ``inc()`` (the executor's wrapped
        generator's ``finally`` block is the enforcement point). A wavering
        counter poisons the picker's load-band filter -- the lane either
        piles on forever (dec never runs) or disappears from preferred
        picks forever (double-dec). The picker does not catch the bug;
        callers must.
        """
        if not proxy_id:
            return 0
        with self._lock:
            current = self._counts.get(proxy_id, 0)
            new = current + n
            self._counts[proxy_id] = new
            return new

    def dec(self, proxy_id: str, n: int = 1) -> int:
        """Decrement the count for ``proxy_id``; never below zero. Drop the
        entry once it hits zero so the dict stays bounded under long-running
        stress runs. Returns the new total (0 once dropped).
        """
        if not proxy_id:
            return 0
        with self._lock:
            current = self._counts.get(proxy_id, 0)
            new = current - n
            if new <= 0:
                self._counts.pop(proxy_id, None)
                return 0
            self._counts[proxy_id] = new
            return new

    def active(self, proxy_id: str) -> int:
        """Live count for one lane. Cheap (one dict lookup under lock)."""
        if not proxy_id:
            return 0
        with self._lock:
            return self._counts.get(proxy_id, 0)

    def snapshot(self) -> Dict[str, int]:
        """Point-in-time copy of every lane's live count for ``/api/proxies``."""
        with self._lock:
            return dict(self._counts)


# Module-level singleton. Importing this module from multiple call sites
# always sees the same in-memory counters -- a per-call instantiation
# would split the picker's "actively-streaming" view across instances
# and the busy-lane penalty would silently misfire. ``active_streams``
# is the single source of truth.
active_streams = ActiveStreams()
