"""Hermetic unit tests for the in-flight stream registry.

The healers/formation read this map to decide whether an egress has a request
streaming through it; the streaming path bumps it for the lifetime of each
stream. These tests pin the accounting so a miscount can never let a killer
daemon through (over-count -> one wasted heal-cycle, benign) or drop a count
so a live stream gets killed (under-count -> catastrophic, what we prevent).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("LINGLING_DATA_DIR", tempfile.mkdtemp(prefix="lingling-as-test-"))
os.environ.setdefault("LINGLING_REQUIRE_KEY", "0")
os.environ.setdefault("LINGLING_BOOTSTRAP_WARP", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers import active_streams  # noqa: E402


def test_inc_then_dec_drops_to_zero_and_leaves_registry():
    active_streams.reset()
    active_streams.inc("warp-1")
    assert active_streams.active("warp-1") == 1
    active_streams.dec("warp-1")
    assert active_streams.active("warp-1") == 0
    # A dec that brings the count to zero removes the id entirely.
    assert "warp-1" not in active_streams.snapshot()


def test_inc_twice_then_dec_once_keeps_one():
    active_streams.reset()
    active_streams.inc("warp-2")
    active_streams.inc("warp-2")
    active_streams.dec("warp-2")
    assert active_streams.active("warp-2") == 1


def test_dec_past_zero_is_idempotent_and_never_negative():
    """An unbalanced dec (exception before inc settled) must not drive the
    count negative -- that would let a later heal fire under a stream."""
    active_streams.reset()
    active_streams.dec("warp-3")
    assert active_streams.active("warp-3") == 0
    active_streams.dec("warp-3")
    assert active_streams.active("warp-3") == 0
    assert "warp-3" not in active_streams.snapshot()


def test_falsy_id_is_a_noop():
    """Direct (non-proxied) requests pass no proxy_id; they never ride a
    re-rollable egress, so they must not perturb any count."""
    active_streams.reset()
    active_streams.inc(None)
    active_streams.inc("")
    assert active_streams.active(None) == 0
    assert active_streams.active("") == 0
    active_streams.dec(None)
    active_streams.dec("")
    assert active_streams.snapshot() == {}


def test_counts_are_isolated_per_proxy_id():
    active_streams.reset()
    active_streams.inc("warp-1")
    active_streams.inc("warp-1")
    active_streams.inc("tor-1")
    snap = active_streams.snapshot()
    assert snap == {"warp-1": 2, "tor-1": 1}
    active_streams.dec("warp-1")
    assert active_streams.active("warp-1") == 1
    assert active_streams.active("tor-1") == 1


def test_snapshot_is_a_copy():
    """Mutating the returned snapshot must not corrupt the live registry
    (tests/dashboards hold onto it)."""
    active_streams.reset()
    active_streams.inc("warp-4")
    snap = active_streams.snapshot()
    snap["warp-4"] = 999
    assert active_streams.active("warp-4") == 1


def test_reset_clears_everything():
    active_streams.reset()
    active_streams.inc("warp-1")
    active_streams.inc("tor-7")
    active_streams.reset()
    assert active_streams.snapshot() == {}
    assert active_streams.active("warp-1") == 0
