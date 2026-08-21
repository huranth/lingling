"""Models learned at runtime to need "thinking patience".

A hidden-reasoning model thinks with server-side tokens it never streams, so the
wire stays silent through the whole thinking pause. Three static signals can
grant it the longer idle/read budgets already:

* an operator override (``config.LONG_THINKING_MODELS``),
* the catalog flag (``LogicalModel.reasoning``), or
* the request body asking for reasoning (``reasoning_effort`` / ``thinking``).

But a model whose reasoning is *both* hidden (no streamed reasoning tokens, so
the client has nothing to ask for) *and* unadvertised (the listing says
``reasoning: false``) is invisible to all three -- it stalls on every turn until
an operator edits the override list by hand. That is the thing this module
removes: the patience it needs is learned from how it behaves on the wire and
remembered, so *a future model of the same kind self-adapts after its first turn*
instead of stalling forever.

Two observations feed the registry, both written from
:func:`routing.stream_guard.guarded_stream` on the chat path:

* a streamed chunk that carries ``reasoning_content`` / ``reasoning`` /
  ``thinking`` is definitive -- the model reasons, full stop;
* a stream that trips the idle watchdog (:class:`routing.stream_idle.StreamStalled`)
  before emitting any visible content is the signature of a model that thinks
  silently before its first token -- the watchdog saw bytes stop for the whole
  budget without the model ever speaking, which a reasoning-but-displaying model
  never does. Gating on ``text_chars == 0`` keeps a model that genuinely stalls
  mid-content (it already spoke, so it is not hidden-thinking) from being
  mis-learned, which would otherwise loosen its first-token failover for nothing.

Entries are timestamped and persisted (mirrors ``retired_models.json``), so they
survive a restart and are shared across the chat/responses/messages entrypoints
(all three read this registry through ``app._stream_pacing``). A TTL ages an
entry out: a one-time transient stall does not pin a model patient forever.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Dict, List

from core import config

_lock = threading.Lock()
_learned: Dict[str, float] = {}  # model_id -> learn timestamp
_loaded = False


def _ttl() -> float:
    return max(0, config.REASONING_LEARNED_TTL_DAYS) * 86400.0


def _load_locked() -> None:
    global _learned, _loaded
    ttl = _ttl()
    fresh: Dict[str, float] = {}
    if ttl:  # ttl == 0 disables the extension; keep nothing.
        try:
            raw = json.loads(
                Path(config.REASONING_LEARNED_FILE).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            raw = {}
        now = time.time()
        if isinstance(raw, dict):
            for mid, ts in raw.items():
                if not isinstance(ts, (int, float)):
                    continue
                if (now - float(ts)) < ttl:
                    fresh[str(mid)] = float(ts)
    _learned = fresh
    _loaded = True


def _ensure_loaded() -> None:
    if _loaded:
        return
    with _lock:
        if not _loaded:
            _load_locked()


def _save_locked() -> None:
    try:
        path = Path(config.REASONING_LEARNED_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_learned), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def mark_reasoning(model_id: str) -> None:
    """Record that ``model_id`` was observed to reason.

    Idempotent on the hot path: an id already known fresh is a no-op (no disk
    write), so observing reasoning tokens on every chunk of every turn does not
    churn the file. A falsy id is ignored -- it carries no model to learn.
    """
    if not model_id:
        return
    _ensure_loaded()
    ttl = _ttl()
    if not ttl:  # extension disabled -> nothing to learn
        return
    with _lock:
        now = time.time()
        prev = _learned.get(model_id)
        if prev is not None and (now - prev) < ttl:
            return  # already fresh; avoid the write
        _learned[model_id] = now
        _save_locked()


def is_reasoning(model_id: str) -> bool:
    """True when ``model_id`` is remembered as reasoning and still fresh."""
    if not model_id:
        return False
    _ensure_loaded()
    ttl = _ttl()
    if not ttl:
        return False
    with _lock:
        ts = _learned.get(model_id)
    return ts is not None and (time.time() - ts) < ttl


def snapshot() -> List[str]:
    """Current learned ids, for a dashboard / log line."""
    _ensure_loaded()
    ttl = _ttl()
    if not ttl:
        return []
    now = time.time()
    with _lock:
        return sorted(m for m, ts in _learned.items() if (now - ts) < ttl)


def reset_for_test() -> None:
    """Drop the in-memory registry (tests). Does not touch the file on disk."""
    global _loaded
    with _lock:
        _learned.clear()
        _loaded = True


def reload_for_test() -> None:
    """Force the next access to re-read the file (tests that edit the file)."""
    global _loaded
    with _lock:
        _loaded = False
