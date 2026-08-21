"""Bug-pinning tests for ``routing.stream_idle.pacing_for``.

HERMETIC -- no network, no streams. ``pacing_for`` is a pure function of
``config`` that picks the idle-watchdog budget and the httpx read timeout for one
outbound stream based on whether the model hides its reasoning tokens.

A hidden-reasoning model (MuseSpark 1.2 Contributor free) stays silent on the
wire while it thinks: it streams no reasoning tokens and sends no keepalives.
The default watchdog (``STREAM_IDLE_TIMEOUT``) and the httpx read timeout
(``STREAM_FIRST_TOKEN_TIMEOUT`` passed in as the per-read timeout) both fired on
that silence, and ``stream_guard`` retried the model to death while it was merely
thinking. The fix was *patience, not stripping reasoning*: reasoning streams get a
longer "thinking patience" and a read timeout that sits above the watchdog.

These tests pin the matrix so a later "simplify the timeouts" edit cannot
silently reintroduce the MuseSpark break without turning one of these red.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("LINGLING_DATA_DIR", tempfile.mkdtemp(prefix="lingling-pacing-test-"))
os.environ.setdefault("LINGLING_REQUIRE_KEY", "0")
os.environ.setdefault("LINGLING_BOOTSTRAP_WARP", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from core import config  # noqa: E402
from routing import stream_idle  # noqa: E402


def _tune(monkeypatch, first, idle, thinking):
    monkeypatch.setattr(config, "STREAM_FIRST_TOKEN_TIMEOUT", first)
    monkeypatch.setattr(config, "STREAM_IDLE_TIMEOUT", idle)
    monkeypatch.setattr(config, "STREAM_THINKING_TIMEOUT", thinking)


def test_pacing_non_reasoning_uses_tight_defaults(monkeypatch):
    """A normal model never legitimately goes quiet, so it keeps the first-token
    budget as its read timeout and the idle watchdog as its silence budget --
    unchanged from the pre-thinking-patience behaviour. Anything looser here
    would slow first-token failover for every ordinary request."""
    _tune(monkeypatch, first=45.0, idle=90.0, thinking=300.0)
    idle_budget, read_to = stream_idle.pacing_for(reasoning=False)
    assert (idle_budget, read_to) == (90.0, 45.0)


def test_pacing_reasoning_gets_thinking_patience_with_read_above_watchdog(monkeypatch):
    """A hidden-reasoning model is silent while it thinks, so the watchdog
    budget extends to ``STREAM_THINKING_TIMEOUT`` and the httpx read timeout
    sits one first-token budget ABOVE it. The watchdog must fire before httpx:
    its ``StreamStalled`` is informative and carries the one ``stream_guard``
    retry, whereas a bare httpx ``ReadTimeout`` reads as a dead tunnel and
    breaks the turn without that recovery."""
    _tune(monkeypatch, first=45.0, idle=90.0, thinking=300.0)
    idle_budget, read_to = stream_idle.pacing_for(reasoning=True)
    assert idle_budget == 300.0
    assert read_to == 300.0 + 45.0
    assert read_to > idle_budget


def test_pacing_thinking_timeout_zero_disables_extension(monkeypatch):
    """``STREAM_THINKING_TIMEOUT <= 0`` is the disable sentinel for the
    *extension*, not for the watchdog itself: a reasoning model then falls back
    to the same tight budgets as a non-reasoning one. Pin this so the
    MuseSpark break returns only when an operator explicitly opts out of the
    patience (and the default is not silently treated as 'off')."""
    _tune(monkeypatch, first=45.0, idle=90.0, thinking=0.0)
    idle_budget, read_to = stream_idle.pacing_for(reasoning=True)
    assert (idle_budget, read_to) == (90.0, 45.0)
