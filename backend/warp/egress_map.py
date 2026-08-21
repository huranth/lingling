"""Learned map of WARP endpoints -> exit IPs, persisted across runs.

Measured live: the exit IP a tunnel gets is decided when the tunnel comes up,
sampled from the local Cloudflare PoP's small pool (~5 addresses here), and the
endpoint edge influences which pool is drawn from. Re-rolling blind wastes
rolls re-discovering the same few mappings; remembering which edge reached
which exit turns re-rolls from dice into aimed assignment.

The map is pure observation: every (endpoint, exit_ip) pair the healer or the
diversity pass sees is recorded. Cloudflare re-shuffles pools over time, so
entries age out and stale claims must always be re-verified by the caller with
a real request before a slot trusts them.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import List, Optional, Set

from core import config

# Entries older than this stop being trusted for aimed rolls (the PoP's
# edge->pool assignment does drift over hours).
ENTRY_TTL_S = 12 * 3600

_lock = threading.Lock()
_cache: Optional[dict] = None


def _path() -> Path:
    return Path(config.DATA_DIR) / "warp" / "egress_map.json"


def _load() -> dict:
    global _cache  # noqa: PLW0603
    if _cache is not None:
        return _cache
    try:
        _cache = json.loads(_path().read_text())
    except (OSError, json.JSONDecodeError):
        _cache = {}
    return _cache


def _save(data: dict) -> None:
    global _cache  # noqa: PLW0603
    _cache = data
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)
    except OSError:
        # The map is an optimization; losing it only costs extra rolls.
        pass


def observe(exit_ip: str, endpoint: str) -> None:
    """Record that ``endpoint`` handed out ``exit_ip`` on a recent roll."""
    if not exit_ip or not endpoint:
        return
    with _lock:
        data = _load()
        entry = data.setdefault(exit_ip, {})
        entry[endpoint] = time.time()
        _save(data)


def edges_for(exit_ip: str) -> List[str]:
    """Endpoints known to reach ``exit_ip``, most recently observed first."""
    with _lock:
        entry = _load().get(exit_ip, {})
        now = time.time()
        fresh = {ep: ts for ep, ts in entry.items() if now - ts <= ENTRY_TTL_S}
        return [ep for ep, _ in sorted(fresh.items(), key=lambda kv: -kv[1])]


def known_exits() -> Set[str]:
    """All exit IPs with at least one fresh endpoint observation."""
    with _lock:
        now = time.time()
        return {
            ip for ip, entry in _load().items()
            if any(now - ts <= ENTRY_TTL_S for ts in entry.values())
        }


def aimed_order(burned: set, occupied: set) -> List[str]:
    """Endpoint attempt order for a re-roll, best target first.

    Prefers edges known to reach an exit that is neither burned nor occupied
    (a genuinely free lane), then edges to unburned-but-shared exits, then any
    candidate edge not yet mapped (exploration -- the map only knows what it
    has seen), then the rest.
    """
    free, shared = [], []
    for ip in known_exits():
        if ip in burned:
            continue
        (free if ip not in occupied else shared).extend(edges_for(ip))
    seen = set(free) | set(shared)
    explore = [e for e in config.WARP_ENDPOINTS if e not in seen]
    rest = [e for e in config.WARP_ENDPOINTS if e not in free + shared + explore]
    return free + shared + explore + rest


def reset_for_tests() -> None:
    """Drop the in-memory cache (tests only)."""
    global _cache  # noqa: PLW0603
    with _lock:
        _cache = None
