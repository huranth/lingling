"""In-flight stream count per egress, so the WARP/Tor health and formation
daemons don't re-roll an exit a request is still streaming through.

A slow hidden-reasoning model (e.g. ``muse-spark-1.2-contributor-free``) keeps a
stream open for a long time. The daemons re-roll exits on a 60s/300s cadence
based on probe state alone; ``warp/manager.py`` restarts the wireproxy process,
which sends a graceful FIN to the streaming socket. The provider's stream reader
(``providers/base.py``) has no ``[DONE]`` / ``finish_reason`` detection -- it just
iterates ``resp.iter_lines()`` until the network ends -- so that FIN reads as
``upstream closed before completing`` and the stream survives only via the one
mid-flight retry onto a fresh exit. That is the "recovered attempts=2" on every
MuseSpark response.

The streaming path calls :func:`inc` around the lifetime of each stream and
:func:`dec` when it ends (see ``OpenAICompatibleProvider.stream_chat``). The
healers call :func:`active` before re-rolling and defer a busy egress by one
cycle instead of killing it. A genuinely *dead* tunnel cannot carry a stream, so
its count is 0 and it is never deferred -- only a streaming (healthy-up) egress
is protected. The guard is gated by ``config.DEFER_REROLL_WHEN_BUSY``.

The registry is keyed by the pool proxy id (``warp-<index>`` / ``tor-<index>``),
which is the same id the streaming path receives as ``proxy_id`` and the healers
derive as ``f"warp-{inst.index}"`` / ``f"tor-{inst.index}"``, so the two sides
join cleanly.
"""
from __future__ import annotations

import threading
from typing import Dict

_lock = threading.Lock()
_counts: Dict[str, int] = {}


def inc(proxy_id: str) -> None:
    """Mark one more in-flight stream on ``proxy_id``. No-op for falsy ids
    (direct / non-proxied requests), which never ride a re-rollable egress."""
    if not proxy_id:
        return
    with _lock:
        _counts[proxy_id] = _counts.get(proxy_id, 0) + 1


def dec(proxy_id: str) -> None:
    """Mark one in-flight stream done. Idempotent: dec past zero is a no-op, so
    an unbalanced call (e.g. an exception before the matching inc completed)
    cannot drive the count negative -- it just drops the id."""
    if not proxy_id:
        return
    with _lock:
        v = _counts.get(proxy_id, 0)
        if v <= 1:
            _counts.pop(proxy_id, None)
        else:
            _counts[proxy_id] = v - 1


def active(proxy_id: str) -> int:
    """In-flight stream count on ``proxy_id`` (0 when none / unknown)."""
    if not proxy_id:
        return 0
    with _lock:
        return _counts.get(proxy_id, 0)


def snapshot() -> Dict[str, int]:
    """Copy of the whole map -- for dashboards / tests."""
    with _lock:
        return dict(_counts)


def reset() -> None:
    """Clear all counts. Tests use this to isolate cases."""
    with _lock:
        _counts.clear()
