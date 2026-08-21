"""Bug-pinning tests for ``routing.pacing_memory`` -- the runtime-learned
"needs thinking patience" registry.

HERMETIC -- no network, no streams. ``pacing_memory`` is a tiny persisted set of
model ids observed to reason on the wire: from a chunk carrying reasoning
tokens (definitive), or from a stream that stalled before emitting any visible
content (the hidden-reasoning fingerprint). An id is timestamped, TTL-bounded,
and persisted across restarts so a future hidden-reasoning model self-adapts
after its first turn instead of stalling every time -- the way MuseSpark did
before the override was hard-coded into LONG_THINKING_MODELS.

The autouse ``_isolated_pacing_memory`` fixture points the registry at a
throwaway tmp file and resets it before each test, so these tests never touch
the live gateway's data dir.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("LINGLING_DATA_DIR", tempfile.mkdtemp(prefix="lingling-pacing-test-"))
os.environ.setdefault("LINGLING_REQUIRE_KEY", "0")
os.environ.setdefault("LINGLING_BOOTSTRAP_WARP", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from core import config  # noqa: E402
from routing import pacing_memory  # noqa: E402


def test_mark_and_is_reasoning():
    pacing_memory.reset_for_test()
    assert pacing_memory.is_reasoning("m1") is False
    pacing_memory.mark_reasoning("m1")
    assert pacing_memory.is_reasoning("m1") is True
    assert pacing_memory.is_reasoning("other") is False


def test_mark_falsy_id_is_noop():
    pacing_memory.reset_for_test()
    pacing_memory.mark_reasoning("")
    assert pacing_memory.snapshot() == []
    assert pacing_memory.is_reasoning("") is False


def test_mark_idempotent_no_churn(monkeypatch):
    """Re-marking a fresh id must not rewrite the file -- reasoning tokens arrive
    on every chunk of every turn, so the hot path must be a no-op once learned."""
    pacing_memory.reset_for_test()
    saves = []
    monkeypatch.setattr(pacing_memory, "_save_locked", lambda: saves.append(1))
    pacing_memory.mark_reasoning("m1")
    pacing_memory.mark_reasoning("m1")
    pacing_memory.mark_reasoning("m1")
    assert saves == [1], "re-marking a fresh id churned the file"


def test_ttl_zero_disables(monkeypatch):
    """``REASONING_LEARNED_TTL_DAYS == 0`` disables the extension: nothing is
    learned, nothing is returned -- the static override/catalog/body checks
    remain the only sources of patience."""
    monkeypatch.setattr(config, "REASONING_LEARNED_TTL_DAYS", 0)
    pacing_memory.reset_for_test()
    pacing_memory.mark_reasoning("m1")
    assert pacing_memory.is_reasoning("m1") is False
    assert pacing_memory.snapshot() == []


def test_stale_entry_expires_on_load(monkeypatch):
    """An entry older than the TTL is dropped when the registry next loads from
    disk, so a one-time transient stall does not pin a model patient forever."""
    monkeypatch.setattr(config, "REASONING_LEARNED_TTL_DAYS", 1)
    Path(config.REASONING_LEARNED_FILE).write_text(
        json.dumps({"stale": 0.0}), encoding="utf-8"
    )
    pacing_memory.reload_for_test()
    assert pacing_memory.is_reasoning("stale") is False
    # A fresh entry loaded from disk is recognised.
    Path(config.REASONING_LEARNED_FILE).write_text(
        json.dumps({"fresh": time.time()}), encoding="utf-8"
    )
    pacing_memory.reload_for_test()
    assert pacing_memory.is_reasoning("fresh") is True


def test_persist_roundtrip():
    """An id learned in one process is seen by another after a 'restart'."""
    pacing_memory.reset_for_test()
    pacing_memory.mark_reasoning("learned-model")
    # Simulate restart: drop memory and force a reload from the persisted file.
    pacing_memory.reload_for_test()
    assert pacing_memory.is_reasoning("learned-model") is True
    assert pacing_memory.snapshot() == ["learned-model"]


def test_snapshot_sorted_and_tidy():
    pacing_memory.reset_for_test()
    pacing_memory.mark_reasoning("b")
    pacing_memory.mark_reasoning("a")
    pacing_memory.mark_reasoning("c")
    assert pacing_memory.snapshot() == ["a", "b", "c"]
