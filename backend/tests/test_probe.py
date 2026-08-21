"""Tests for the startup probe and healers (warp.probe module).

HERMETIC tests -- no network, no real WARP identities.  Every test uses fake
proxy pools, fake warp managers, and mocked httpx responses.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

os.environ.setdefault("LINGLING_DATA_DIR", tempfile.mkdtemp(prefix="lingling-probe-test-"))
os.environ.setdefault("LINGLING_REQUIRE_KEY", "0")
os.environ.setdefault("LINGLING_BOOTSTRAP_WARP", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.proxy_pool import ProxyPool  # noqa: E402
from warp import probe  # noqa: E402


class FakeWarpInstance:
    def __init__(self, index, port=51001):
        self.index = index
        self.port = port
        self.private_key = "fake-key"
        self.proxy_url = f"socks5://127.0.0.1:{port}"
        self.process = None
        self.regenerated = False

    def __repr__(self):
        return f"<FakeWarpInstance #{self.index}>"


class FakeWarpManager:
    def __init__(self, count=3):
        self.instances = [FakeWarpInstance(i + 1, 51001 + i) for i in range(count)]
        self.count = count
        self.regenerated_indices = []
        self.rerolls = []          # (index, attempt) per re_roll_tunnel call

    def regenerate_instance(self, inst):
        inst.regenerated = True
        self.regenerated_indices.append(inst.index)
        return True                    # mirrors WarpManager: True on full success, False on fail

    def re_roll_tunnel(self, inst, attempt=0, endpoint=None, log=None):
        self.rerolls.append((inst.index, attempt))
        return endpoint or "162.159.192.1"


def _make_pool(n=3):
    pool = ProxyPool()
    for i in range(n):
        pool.add(f"socks5://127.0.0.1:{51001 + i}", proxy_id=f"warp-{i + 1}")
    return pool


def test_probe_result_defaults():
    r = probe.ProbeResult(proxy_id="warp-1")
    assert r.status == "pending"
    d = r.to_dict()
    assert d["proxy_id"] == "warp-1"


def test_probe_summary_to_dict():
    s = probe.ProbeSummary(total=2, healthy=1, dead=1)
    d = s.to_dict()
    assert d["total"] == 2
    assert d["healthy"] == 1
    assert d["healed"] == 0


# ---------------------------------------------------------------------------
# _probe_single tests (mocked httpx)
# ---------------------------------------------------------------------------

def test_probe_single_200_ok():
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    with mock.patch("warp.probe.socks5_connect_check", return_value=""), \
         mock.patch("warp.probe.httpx.Client") as MockClient:
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.MagicMock(return_value=ctx)
        ctx.__exit__ = mock.MagicMock(return_value=False)
        ctx.post.return_value = mock_resp
        MockClient.return_value = ctx
        result = probe._probe_single(
            "socks5://127.0.0.1:51001", "warp-1",
            "deepseek-v4-flash-free", "https://opencode.ai/zen/v1", 15,
        )
    assert result.status == "ok"
    assert result.latency_ms >= 0


def test_probe_single_429():
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 429
    mock_resp.json.return_value = {"error": {"message": "Rate limit exceeded"}}
    with mock.patch("warp.probe.socks5_connect_check", return_value=""), \
         mock.patch("warp.probe.httpx.Client") as MockClient:
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.MagicMock(return_value=ctx)
        ctx.__exit__ = mock.MagicMock(return_value=False)
        ctx.post.return_value = mock_resp
        MockClient.return_value = ctx
        result = probe._probe_single(
            "socks5://127.0.0.1:51001", "warp-1",
            "deepseek-v4-flash-free", "https://opencode.ai/zen/v1", 15,
        )
    assert result.status == "rate_limited"
    assert "Rate limit" in result.error


def test_probe_single_connection_error():
    with mock.patch("warp.probe.socks5_connect_check", return_value=""), \
         mock.patch("warp.probe.httpx.Client") as MockClient:
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.MagicMock(return_value=ctx)
        ctx.__exit__ = mock.MagicMock(return_value=False)
        ctx.post.side_effect = Exception("connection refused")
        MockClient.return_value = ctx
        result = probe._probe_single(
            "socks5://127.0.0.1:51001", "warp-1",
            "deepseek-v4-flash-free", "https://opencode.ai/zen/v1", 15,
        )
    assert result.status == "dead"
    assert "connection refused" in result.error


def test_probe_single_timeout():
    import httpx as _httpx
    with mock.patch("warp.probe.socks5_connect_check", return_value=""), \
         mock.patch("warp.probe.httpx.Client") as MockClient:
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.MagicMock(return_value=ctx)
        ctx.__exit__ = mock.MagicMock(return_value=False)
        ctx.post.side_effect = _httpx.TimeoutException("timed out")
        MockClient.return_value = ctx
        result = probe._probe_single(
            "socks5://127.0.0.1:51001", "warp-1",
            "deepseek-v4-flash-free", "https://opencode.ai/zen/v1", 15,
        )
    assert result.status == "dead"
    assert "timeout" in result.error


def test_probe_single_500():
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 500
    with mock.patch("warp.probe.socks5_connect_check", return_value=""), \
         mock.patch("warp.probe.httpx.Client") as MockClient:
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.MagicMock(return_value=ctx)
        ctx.__exit__ = mock.MagicMock(return_value=False)
        ctx.post.return_value = mock_resp
        MockClient.return_value = ctx
        result = probe._probe_single(
            "socks5://127.0.0.1:51001", "warp-1",
            "deepseek-v4-flash-free", "https://opencode.ai/zen/v1", 15,
        )
    assert result.status == "dead"
    assert "HTTP 500" in result.error


def test_probe_single_401_is_probe_error_not_dead():
    """A 401 (OpenCode rejected the probe model/auth) reached the upstream, so
    the tunnel is up and not rate-limited -- it is NOT a dead tunnel. This
    regressed live: every freshly registered identity got ``probe_error``'d as
    ``dead`` and heal_expired regenerated the whole pool on the next probe
    because ``deepseek-v4-flash-free`` had been gated behind a key, burning
    Cloudflare registrations for a problem the identity can't fix."""
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 401
    with mock.patch("warp.probe.socks5_connect_check", return_value=""), \
         mock.patch("warp.probe.httpx.Client") as MockClient:
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.MagicMock(return_value=ctx)
        ctx.__exit__ = mock.MagicMock(return_value=False)
        ctx.post.return_value = mock_resp
        MockClient.return_value = ctx
        result = probe._probe_single(
            "socks5://127.0.0.1:51001", "warp-1",
            "deepseek-v4-flash-free", "https://opencode.ai/zen/v1", 15,
        )
    assert result.status == "probe_error"
    assert "HTTP 401" in result.error


def test_probe_error_lanes_are_not_healed_as_dead():
    """A probe model rejection (probe_error) must not be churned by ``heal_expired``
    or ``heal_rate_limited``: the tunnel reached OpenCode, so neither regenerating
    the identity nor re-rolling the exit can fix an upstream model gate."""
    pool = _make_pool(2)
    wm = FakeWarpManager(count=2)
    summary = probe.ProbeSummary(
        total=2, healthy=1, probe_error=1, results=[
            probe.ProbeResult(proxy_id="warp-1", status="ok"),
            probe.ProbeResult(proxy_id="warp-2", status="probe_error",
                              error="HTTP 401", exit_ip="104.28.231.142"),
        ])
    healed_expired = probe.heal_expired(pool, summary, wm, log=lambda *a, **k: None)
    healed_rate = probe.heal_rate_limited(pool, summary, wm, log=lambda *a, **k: None)
    assert healed_expired == 0 and healed_rate == 0
    assert wm.regenerated_indices == []
    # The probe_error lane stays in the pool so real traffic still routes it.
    assert pool.get_by_id("warp-1") is not None
    assert pool.get_by_id("warp-2") is not None
    # And neither healer rewrote the verdict.
    assert summary.results[1].status == "probe_error"


# ---------------------------------------------------------------------------
# SOCKS5 liveness pre-check -- httpcore's handshake reads carry no timeout,
# so a silent tunnel used to park the probing thread forever. These tests# reproduce that exact wedge with a local server that accepts and never
# answers.
# ---------------------------------------------------------------------------

class _SilentSocksServer:
    """Accepts TCP connections and never speaks -- a wedged wireproxy/tor."""

    def __init__(self):
        import socket as _socket
        self._socket = _socket
        srv = _socket.socket()
        srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        self._srv = srv
        self.port = srv.getsockname()[1]
        self.url = f"socks5://127.0.0.1:{self.port}"
        self._conns = []
        import threading as _threading
        self._stop = _threading.Event()

        def serve():
            srv.settimeout(0.2)
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                    self._conns.append(conn)  # hold open, never reply
                except _socket.timeout:
                    continue
                except OSError:
                    break

        _threading.Thread(target=serve, daemon=True).start()

    def close(self):
        self._stop.set()
        for c in self._conns:
            try:
                c.close()
            except OSError:
                pass
        self._srv.close()


def test_socks5_check_times_out_on_silent_proxy():
    srv = _SilentSocksServer()
    try:
        started = time.time()
        reason = probe.socks5_connect_check(srv.url, "opencode.ai", 443, timeout=0.5)
        assert reason == "timed out"
        assert time.time() - started < 5
    finally:
        srv.close()


def test_socks5_check_refused_and_non_socks():
    assert probe.socks5_connect_check("socks5://127.0.0.1:1", "opencode.ai", 443, 0.5)
    assert probe.socks5_connect_check("http://127.0.0.1:8080", "opencode.ai") == \
        "not a socks5:// proxy"


def test_probe_single_dead_lane_fails_fast_without_httpx():
    srv = _SilentSocksServer()
    try:
        with mock.patch.object(probe.config, "PROBE_SOCKS_TIMEOUT", 0.5), \
             mock.patch("warp.probe.httpx.Client") as MockClient:
            started = time.time()
            result = probe._probe_single(
                srv.url, "warp-9", "m", "https://opencode.ai/zen/v1", 15,
            )
            assert MockClient.call_count == 0  # never reached httpx
        assert result.status == "dead"
        assert "socks5 handshake: timed out" in result.error
        assert time.time() - started < 5
    finally:
        srv.close()


def test_fetch_exit_ip_gives_up_fast_on_silent_tunnel():
    srv = _SilentSocksServer()
    try:
        started = time.time()
        assert probe._fetch_exit_ip(srv.url, timeout=0.5) == ""
        assert time.time() - started < 5
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# probe_all: parallel lanes + watchdog
# ---------------------------------------------------------------------------

def test_probe_all_runs_lanes_in_parallel():
    pool = _make_pool(6)

    def slow_probe(url, pid, model, base, timeout):
        time.sleep(0.25)
        return probe.ProbeResult(proxy_id=pid, status="ok", probed_at=time.time())

    with mock.patch.object(probe.config, "PROBE_CONCURRENCY", 6), \
         mock.patch("warp.probe._probe_single", side_effect=slow_probe):
        started = time.time()
        summary = probe.probe_all(pool, log=lambda *a, **k: None)
    assert summary.healthy == 6
    # Sequential would need 6 x 0.25s = 1.5s; parallel one wave of 0.25s.
    assert time.time() - started < 1.2


def test_probe_all_watchdog_cuts_off_wedged_lane():
    pool = _make_pool(2)
    import threading as _threading
    release = _threading.Event()

    def stuck_probe(url, pid, model, base, timeout):
        release.wait(10)  # simulate a lane parked in an un-timed handshake
        return probe.ProbeResult(proxy_id=pid, status="ok")

    with mock.patch.object(probe.config, "PROBE_CONCURRENCY", 2), \
         mock.patch.object(probe.config, "PROBE_SOCKS_TIMEOUT", 0.05), \
         mock.patch.object(probe.config, "PROBE_TRACE_TIMEOUT", 0.05), \
         mock.patch.object(probe.config, "PROBE_CAP_SLACK", 0.35), \
         mock.patch("warp.probe._probe_single", side_effect=stuck_probe):
        try:
            started = time.time()
            summary = probe.probe_all(pool, timeout=0.1, log=lambda *a, **k: None)
            assert summary.dead == 2
            assert summary.healthy == 0
            assert time.time() - started < 5
            assert all("no answer in" in r.error for r in summary.results)
        finally:
            release.set()  # free the parked workers so the executor can rest


# ---------------------------------------------------------------------------
# probe_all tests
# ---------------------------------------------------------------------------

def test_probe_all_empty_pool():
    pool = ProxyPool()
    summary = probe.probe_all(pool, log=lambda *a, **k: None)
    assert summary.total == 0


def test_probe_all_mixed_results():
    pool = _make_pool(3)

    def fake_probe(url, pid, model, base, timeout):
        if "51001" in url:
            return probe.ProbeResult(proxy_id=pid, status="ok", latency_ms=100, probed_at=time.time())
        elif "51002" in url:
            return probe.ProbeResult(proxy_id=pid, status="rate_limited", probed_at=time.time())
        else:
            return probe.ProbeResult(proxy_id=pid, status="dead", error="timeout", probed_at=time.time())

    with mock.patch("warp.probe._probe_single", side_effect=fake_probe):
        summary = probe.probe_all(pool, log=lambda *a, **k: None)
    assert summary.total == 3
    assert summary.healthy == 1
    assert summary.rate_limited == 1
    assert summary.dead == 1
    assert probe.latest_summary()["total"] == 3


# ---------------------------------------------------------------------------
# healer tests
# ---------------------------------------------------------------------------

def test_heal_expired_removes_dead_and_regenerates():
    pool = _make_pool(2)
    wm = FakeWarpManager(count=2)
    summary = probe.ProbeSummary(total=2, healthy=1, dead=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="ok"),
        probe.ProbeResult(proxy_id="warp-2", status="dead", error="timeout"),
    ])
    healed = probe.heal_expired(pool, summary, wm, log=lambda *a, **k: None)
    assert healed == 1
    assert 2 in wm.regenerated_indices
    assert pool.get_by_id("warp-1") is not None
    assert pool.get_by_id("warp-2") is None


def test_heal_expired_marks_summary_healed():
    """A regenerated identity flips its probe result to ``healed`` right away.

    The dashboard reads the stored summary, not the logs; without this the
    slot would keep its stale ``dead`` verdict (red chip) until the next
    probe pass even though the lane was already regenerated.
    """
    pool = _make_pool(2)
    wm = FakeWarpManager(count=2)
    summary = probe.ProbeSummary(total=2, healthy=1, dead=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="ok"),
        probe.ProbeResult(proxy_id="warp-2", status="dead", error="timeout",
                          exit_ip="104.28.231.142"),
    ])
    healed = probe.heal_expired(pool, summary, wm, log=lambda *a, **k: None)
    assert healed == 1
    r = summary.results[1]
    assert r.status == "healed"
    assert r.exit_ip == "" and r.error == "" and r.latency_ms == 0.0
    # The aggregate counters move with the verdicts.
    assert summary.healthy == 1 and summary.dead == 0


def test_heal_rate_limited_updates_summary_to_healed():
    """A successful re-roll rewrites the probe result to the transient
    ``healed`` status (not ``ok``) with the fresh exit IP and latency.

    The stored summary is what the dashboard renders; the per-slot chip has
    a warn-orange ``healed`` pill (tooltip "exit refreshed — next probe
    verifies it") so an operator can pick a lane freshly rolled off a
    burned exit out of the rack — bypassing it (writing ``ok``) was what
    made the heal look invisible, a graduated lane snapping from red to
    green with no tag. ``healed`` is counted in its own :class:`ProbeSummary`
    counter (NOT ``healthy``): this re-roll was canary-verified once, but the
    periodic probe is the standing confirmation, and the next pass rewrites
    the summary so the lane flips to ``ok`` on its own. Totals reconcile —
    ``healthy + rate_limited + dead + healed == total`` — so the exit-probe
    panel still adds up.
    """
    pool = _make_pool(1)
    wm = FakeWarpManager(count=1)
    summary = probe.ProbeSummary(total=1, rate_limited=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="rate_limited",
                          exit_ip="104.28.231.142"),
    ])
    exits = iter(["104.28.231.145"])
    checks = iter([probe.ProbeResult(proxy_id="warp-1", status="ok", latency_ms=777)])
    with mock.patch("warp.probe._fetch_exit_ip", side_effect=lambda url: next(exits)), \
         mock.patch("warp.probe.probe_proxy", side_effect=lambda url, pid, model=None: next(checks)), \
         mock.patch("warp.probe.time.sleep"):
        healed = probe.heal_rate_limited(pool, summary, wm, log=lambda *a, **k: None)
    assert healed == 1
    r = summary.results[0]
    assert r.status == "healed"
    assert r.exit_ip == "104.28.231.145"
    assert r.latency_ms == 777
    assert summary.rate_limited == 0 and summary.healthy == 0 and summary.healed == 1


def test_heal_rate_limited_failed_roll_keeps_summary_intact():
    """A slot that cannot escape the burned exits keeps its verdict -- the
    chip must keep showing rate-limited, not flip to something optimistic."""
    pool = _make_pool(1)
    wm = FakeWarpManager(count=1)
    summary = probe.ProbeSummary(total=1, rate_limited=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="rate_limited",
                          exit_ip="104.28.231.142"),
    ])
    with mock.patch("warp.probe._fetch_exit_ip",
                    return_value="104.28.231.142"), \
         mock.patch("warp.probe.time.sleep"):
        healed = probe.heal_rate_limited(pool, summary, wm, log=lambda *a, **k: None)
    assert healed == 0
    r = summary.results[0]
    assert r.status == "rate_limited" and r.exit_ip == "104.28.231.142"
    assert summary.rate_limited == 1 and summary.healthy == 0


def test_heal_rate_limited_rerolls_until_clean_exit():
    """A burned slot re-establishes its tunnel until its exit IP is unburned.

    The heal must keep the proxy in the pool (the port never changes, only the
    tunnel behind it does) and must not spend a Cloudflare registration.
    """
    pool = _make_pool(3)
    wm = FakeWarpManager(count=3)
    summary = probe.ProbeSummary(total=3, rate_limited=2, results=[
        probe.ProbeResult(proxy_id="warp-1", status="ok", exit_ip="104.28.231.146"),
        probe.ProbeResult(proxy_id="warp-2", status="rate_limited", exit_ip="104.28.231.142"),
        probe.ProbeResult(proxy_id="warp-3", status="rate_limited", exit_ip="104.28.231.142"),
    ])
    exits = iter(["104.28.231.142",   # warp-2 roll 1: lands back on the burned IP
                  "104.28.231.145",   # warp-2 roll 2: fresh IP
                  "104.28.231.145"])  # warp-3 roll 1: shares warp-2's clean exit
    checks = iter([probe.ProbeResult(proxy_id="warp-2", status="ok", latency_ms=900),
                   probe.ProbeResult(proxy_id="warp-3", status="ok", latency_ms=800)])
    with mock.patch("warp.probe._fetch_exit_ip", side_effect=lambda url: next(exits)), \
         mock.patch("warp.probe.probe_proxy", side_effect=lambda url, pid, model=None: next(checks)), \
         mock.patch("warp.probe.time.sleep"):
        healed = probe.heal_rate_limited(pool, summary, wm, log=lambda *a, **k: None)
    assert healed == 2
    # warp-2 needed two rolls (attempt 0 burned, attempt 1 clean); warp-3 got
    # the next roll slot.
    assert (2, 0) in wm.rerolls and (2, 1) in wm.rerolls and (3, 0) in wm.rerolls
    # Nothing was regenerated, nothing was removed: slots stay in the pool.
    assert wm.regenerated_indices == []
    assert pool.get_by_id("warp-2") is not None
    assert pool.get_by_id("warp-3") is not None


def test_heal_rate_limited_gives_up_after_max_rolls():
    """A slot that cannot escape the burned IPs stays in the pool, unhealed.

    The next periodic pass retries it; the pool's own cooldown keeps live
    traffic away meanwhile.
    """
    from core import config
    pool = _make_pool(1)
    wm = FakeWarpManager(count=1)
    summary = probe.ProbeSummary(total=1, rate_limited=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="rate_limited", exit_ip="104.28.231.142"),
    ])
    with mock.patch("warp.probe._fetch_exit_ip",
                    return_value="104.28.231.142"), \
         mock.patch("warp.probe.time.sleep"):
        healed = probe.heal_rate_limited(pool, summary, wm, log=lambda *a, **k: None)
    assert healed == 0
    assert len(wm.rerolls) == config.WARP_REROLL_MAX_ATTEMPTS
    assert pool.get_by_id("warp-1") is not None


def test_heal_rate_limited_parks_unhealed_exit_out_of_pick():
    """A rate-limited exit the heal could not move off a burned IP is parked
    out of ``ProxyPool.pick`` until the next probe pass -- so a live request
    is not routed through a known-burned address only to rediscover its 429
    at request time. ``mark_failure`` is deliberately NOT the gate: probe
    verdicts are the old observation, not a fresh request failure, and would
    inflate the burn counters that drive ``_dump_burned_identities``.
    """
    pool = _make_pool(1)
    wm = FakeWarpManager(count=1)
    summary = probe.ProbeSummary(total=1, rate_limited=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="rate_limited",
                          exit_ip="104.28.231.142"),
    ])
    px = pool.get_by_id("warp-1")
    assert px is not None and not px.in_cooldown()
    # Every roll lands back on the burned IP -> "could not escape".
    with mock.patch("warp.probe._fetch_exit_ip",
                    return_value="104.28.231.142"), \
         mock.patch("warp.probe.time.sleep"):
        healed = probe.heal_rate_limited(pool, summary, wm, log=lambda *a, **k: None)
    assert healed == 0
    # Still in the pool (no regeneration) but now cooling: parked, not gone.
    still = pool.get_by_id("warp-1")
    assert still is not None
    assert still.in_cooldown()
    # The gate came from extend_cooldown (no failure bump): probe verdict,
    # not a real 429, so the burn counters stay untouched.
    assert still.consecutive_failures == 0 and still.total_429 == 0


def test_heal_rate_limited_verifies_with_converged_canary_not_the_pin():
    """The post-roll verify probe must use the canary ``probe_all`` proved
    serving (``summary.model``), not the configured ``PROBE_MODEL`` pin -- an
    OpenCode pull behind a gate made the old pin 400 "Model is unavailable"
    on every fresh exit, which read ``probe_error`` and left the slot
    "for the health cycle" indefinitely. The fix is exactly this canary
    thread-down from :func:`probe_all` to :func:`probe_proxy`.
    """
    pool = _make_pool(1)
    wm = FakeWarpManager(count=1)
    summary = probe.ProbeSummary(
        total=1, rate_limited=1, model="mimo-v2.5-free", results=[
            probe.ProbeResult(proxy_id="warp-1", status="rate_limited",
                              exit_ip="104.28.231.142"),
        ],
    )
    calls = []
    sentinel = probe.ProbeResult(proxy_id="warp-1", status="ok", latency_ms=42)
    with mock.patch("warp.probe._fetch_exit_ip",
                    return_value="104.28.231.145"), \
         mock.patch("warp.probe.probe_proxy",
                    side_effect=lambda url, pid, model=None:
                        (calls.append(model), sentinel)[1]), \
         mock.patch("warp.probe.time.sleep"):
        healed = probe.heal_rate_limited(pool, summary, wm, log=lambda *a, **k: None)
    assert healed == 1
    # The verify probe ran with the converged canary, not the stale pin.
    assert calls and calls[-1] == "mimo-v2.5-free"


def test_heal_rate_limited_newly_burned_exit_joins_the_burned_set():
    """An exit that turns out limited on first contact is not rolled onto twice."""
    pool = _make_pool(1)
    wm = FakeWarpManager(count=1)
    summary = probe.ProbeSummary(total=1, rate_limited=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="rate_limited", exit_ip="104.28.231.142"),
    ])
    exits = iter(["104.28.231.145", "104.28.231.145", "104.28.231.146"])
    checks = iter([
        probe.ProbeResult(proxy_id="warp-1", status="rate_limited", exit_ip="104.28.231.145"),
        probe.ProbeResult(proxy_id="warp-1", status="ok"),
    ])
    with mock.patch("warp.probe._fetch_exit_ip", side_effect=lambda url: next(exits)), \
         mock.patch("warp.probe.probe_proxy", side_effect=lambda url, pid, model=None: next(checks)), \
         mock.patch("warp.probe.time.sleep"):
        healed = probe.heal_rate_limited(pool, summary, wm, log=lambda *a, **k: None)
    assert healed == 1
    # .145 was probed exactly once: after its 429 it joined the burned set, so
    # the next roll that landed there re-rolled without re-probing it.
    assert wm.rerolls == [(1, 0), (1, 1), (1, 2)]


def test_heal_rate_limited_dead_tunnel_stops_early():
    """A tunnel that will not come back up is left for the dead-tunnel healer."""
    pool = _make_pool(1)
    wm = FakeWarpManager(count=1)
    wm.re_roll_tunnel = lambda inst, attempt=0, endpoint=None, log=None: None
    summary = probe.ProbeSummary(total=1, rate_limited=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="rate_limited", exit_ip="104.28.231.142"),
    ])
    healed = probe.heal_rate_limited(pool, summary, wm, log=lambda *a, **k: None)
    assert healed == 0
    assert pool.get_by_id("warp-1") is not None


def test_heal_rate_limited_removes_orphaned_pool_entry():
    """A warp- proxy id with no instance behind it is dropped from the pool."""
    pool = _make_pool(1)
    wm = FakeWarpManager(count=1)
    wm.instances = []   # the instance is gone
    summary = probe.ProbeSummary(total=1, rate_limited=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="rate_limited", exit_ip="104.28.231.142"),
    ])
    healed = probe.heal_rate_limited(pool, summary, wm, log=lambda *a, **k: None)
    assert healed == 0
    assert pool.get_by_id("warp-1") is None


# ---------------------------------------------------------------------------
# egress map + diversity spread
# ---------------------------------------------------------------------------


def test_egress_map_aimed_order_prefers_free_exit_edges():
    from warp import egress_map

    egress_map.observe("1.1.1.1", "edge-a")   # free
    egress_map.observe("1.1.1.2", "edge-b")   # will be marked burned
    egress_map.observe("1.1.1.3", "edge-c")   # occupied
    order = egress_map.aimed_order(burned={"1.1.1.2"}, occupied={"1.1.1.3"})
    assert order[0] == "edge-a", order        # free lane first
    assert "edge-b" not in order[:1]
    # Everything configured still appears exactly once.
    from core import config
    assert sorted(order) == sorted(set(order))
    assert set(config.WARP_ENDPOINTS) <= set(order)


def test_egress_map_knowledge_expires():
    import time as _time
    from warp import egress_map

    egress_map.observe("2.2.2.2", "edge-x")
    # Age the entry past the TTL by rewriting the timestamp.
    egress_map._load()["2.2.2.2"]["edge-x"] = _time.time() - (egress_map.ENTRY_TTL_S + 10)
    assert "2.2.2.2" not in egress_map.known_exits()
    assert egress_map.edges_for("2.2.2.2") == []


def test_spread_moves_duplicates_onto_free_exits():
    from warp import egress_map

    egress_map.observe("9.9.9.9", "edge-free")
    pool = _make_pool(3)
    wm = FakeWarpManager(count=3)
    summary = probe.ProbeSummary(total=3, healthy=3, results=[
        # two slots share one exit, a third sits elsewhere; 9.9.9.9 is free
        probe.ProbeResult(proxy_id="warp-1", status="ok", exit_ip="3.3.3.3"),
        probe.ProbeResult(proxy_id="warp-2", status="ok", exit_ip="3.3.3.3"),
        probe.ProbeResult(proxy_id="warp-3", status="ok", exit_ip="4.4.4.4"),
    ])
    exits = iter(["9.9.9.9"])
    checks = iter([probe.ProbeResult(proxy_id="warp-2", status="ok")])
    aimed = []
    rolled = []
    wm.re_roll_tunnel = lambda inst, attempt=0, endpoint=None, log=None: \
        (aimed.append(endpoint), rolled.append(inst.index),
         endpoint or "edge-free")[2]
    with mock.patch("warp.probe._fetch_exit_ip", side_effect=lambda url: next(exits)), \
         mock.patch("warp.probe.probe_proxy", side_effect=lambda url, pid, model=None: next(checks)), \
         mock.patch("warp.probe.time.sleep"):
        moved = probe.spread_distinct_exits(pool, summary, wm, log=lambda *a, **k: None)
    assert moved == 1
    assert aimed == ["edge-free"]     # the roll was aimed at the known edge
    assert rolled == [2]              # the duplicate moved, not the singleton


def test_spread_updates_summary_with_new_exit():
    """A moved slot's probe result follows it to its new exit lane.

    The dashboard groups slots by the exit IP in the probe result; a spread
    that verified a new exit must rewrite it or the rack keeps showing the
    slot on its old lane (and the exit-lane count stays one short).
    """
    from warp import egress_map

    egress_map.observe("9.9.9.9", "edge-free")
    pool = _make_pool(3)
    wm = FakeWarpManager(count=3)
    summary = probe.ProbeSummary(total=3, healthy=3, results=[
        probe.ProbeResult(proxy_id="warp-1", status="ok", exit_ip="3.3.3.3"),
        probe.ProbeResult(proxy_id="warp-2", status="ok", exit_ip="3.3.3.3"),
        probe.ProbeResult(proxy_id="warp-3", status="ok", exit_ip="4.4.4.4"),
    ])
    exits = iter(["9.9.9.9"])
    checks = iter([probe.ProbeResult(proxy_id="warp-2", status="ok", latency_ms=555)])
    with mock.patch("warp.probe._fetch_exit_ip", side_effect=lambda url: next(exits)), \
         mock.patch("warp.probe.probe_proxy", side_effect=lambda url, pid, model=None: next(checks)), \
         mock.patch("warp.probe.time.sleep"):
        moved = probe.spread_distinct_exits(pool, summary, wm, log=lambda *a, **k: None)
    assert moved == 1
    by_id = {r.proxy_id: r for r in summary.results}
    assert by_id["warp-2"].status == "ok"
    assert by_id["warp-2"].exit_ip == "9.9.9.9"
    assert by_id["warp-2"].latency_ms == 555
    assert summary.healthy == 3       # ok -> ok is a no-op for the counters


def test_spread_no_free_targets_is_a_noop():
    from warp import egress_map

    egress_map.observe("3.3.3.3", "edge-a")
    pool = _make_pool(2)
    wm = FakeWarpManager(count=2)
    summary = probe.ProbeSummary(total=2, healthy=2, results=[
        probe.ProbeResult(proxy_id="warp-1", status="ok", exit_ip="3.3.3.3"),
        probe.ProbeResult(proxy_id="warp-2", status="ok", exit_ip="3.3.3.3"),
    ])
    with mock.patch.object(wm, "re_roll_tunnel") as rr:
        moved = probe.spread_distinct_exits(pool, summary, wm, log=lambda *a, **k: None)
    assert moved == 0
    rr.assert_not_called()            # every known exit is occupied


def test_heal_reduces_rolls_when_all_known_exits_are_burned():
    from warp import egress_map

    # The map knows exactly one exit, and it is the burned one.
    egress_map.observe("5.5.5.5", "edge-a")
    pool = _make_pool(1)
    wm = FakeWarpManager(count=1)
    summary = probe.ProbeSummary(total=1, rate_limited=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="rate_limited", exit_ip="5.5.5.5"),
    ])
    with mock.patch("warp.probe._fetch_exit_ip", return_value="5.5.5.5"), \
         mock.patch("warp.probe.time.sleep"):
        healed = probe.heal_rate_limited(pool, summary, wm, log=lambda *a, **k: None)
    assert healed == 0
    # Discovery budget only: 2 rolls, not the full 6.
    assert len(wm.rerolls) == 2


def test_heal_expired_skips_nonexistent():
    pool = _make_pool(1)
    wm = FakeWarpManager(count=3)
    summary = probe.ProbeSummary(total=1, results=[
        probe.ProbeResult(proxy_id="warp-99", status="dead"),
    ])
    healed = probe.heal_expired(pool, summary, wm, log=lambda *a, **k: None)
    assert healed == 0


def test_heal_logs_reroll_exception():
    """A crashing re-roll must not take the whole healer down."""
    pool = _make_pool(1)
    wm = FakeWarpManager(count=1)
    wm.re_roll_tunnel = mock.MagicMock(side_effect=RuntimeError("wireproxy vanished"))
    summary = probe.ProbeSummary(total=1, rate_limited=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="rate_limited", exit_ip="104.28.231.142"),
    ])
    logs = []
    healed = probe.heal_rate_limited(pool, summary, wm, log=lambda *a, **k: logs.append(str(a)))
    assert healed == 0
    assert pool.get_by_id("warp-1") is not None


# ---------------------------------------------------------------------------
# helper tests
# ---------------------------------------------------------------------------

def test_instance_index_valid():
    assert probe._instance_index("warp-5") == 5
    assert probe._instance_index("warp-10") == 10


def test_instance_index_invalid():
    assert probe._instance_index("proxy-1") is None
    assert probe._instance_index("warp-") is None
    assert probe._instance_index("warp-abc") is None


def test_find_instance_found():
    wm = FakeWarpManager(count=3)
    inst = probe._find_instance(wm, 2)
    assert inst is not None
    assert inst.index == 2


def test_find_instance_not_found():
    wm = FakeWarpManager(count=3)
    assert probe._find_instance(wm, 99) is None


# ---------------------------------------------------------------------------
# Full integration: probe + heal
# ---------------------------------------------------------------------------

def test_probe_and_heal_full_cycle():
    pool = _make_pool(3)
    wm = FakeWarpManager(count=3)

    def fake_probe(url, pid, model, base, timeout):
        if "51001" in url:
            return probe.ProbeResult(proxy_id=pid, status="ok", latency_ms=50,
                                     probed_at=time.time(), exit_ip="104.28.231.146")
        elif "51002" in url:
            return probe.ProbeResult(proxy_id=pid, status="rate_limited",
                                    probed_at=time.time(), exit_ip="104.28.231.142")
        else:
            return probe.ProbeResult(proxy_id=pid, status="dead", error="connect error",
                                     probed_at=time.time(), exit_ip="")

    with mock.patch("warp.probe._probe_single", side_effect=fake_probe):
        summary = probe.probe_all(pool, log=lambda *a, **k: None)

    assert summary.healthy == 1
    assert summary.rate_limited == 1
    assert summary.dead == 1

    # The rate-limited slot re-rolls onto a fresh exit and confirms with a
    # real request; the dead one regenerates its identity (that heal is for
    # broken tunnels, which is what regeneration actually fixes).
    exits = iter(["104.28.231.145"])
    checks = iter([probe.ProbeResult(proxy_id="warp-2", status="ok", latency_ms=850)])
    with mock.patch("warp.probe._fetch_exit_ip", side_effect=lambda url: next(exits)), \
         mock.patch("warp.probe.probe_proxy", side_effect=lambda url, pid, model=None: next(checks)), \
         mock.patch("warp.probe.time.sleep"):
        rl_healed = probe.heal_rate_limited(pool, summary, wm, log=lambda *a, **k: None)
    expired_healed = probe.heal_expired(pool, summary, wm, log=lambda *a, **k: None)

    assert expired_healed == 1
    assert rl_healed == 1
    assert wm.regenerated_indices == [3]
    assert wm.rerolls == [(2, 0)]
    assert pool.get_by_id("warp-1") is not None
    assert pool.get_by_id("warp-2") is not None
    assert pool.get_by_id("warp-3") is None


def test_probe_config_defaults():
    from core import config
    assert config.PROBE_ON_STARTUP is True
    assert config.PROBE_MODEL == "deepseek-v4-flash-free"
    assert config.PROBE_TIMEOUT > 0
    assert config.PROBE_INTERVAL_S > 0


# ---------------------------------------------------------------------------
# Health daemon periodic probe (rate-limit healer running mid-session)
# ---------------------------------------------------------------------------

def _make_daemon(pool=None, wm=None, probe_interval=300):
    from warp.health import WarpHealthDaemon
    pool = _make_pool(3) if pool is None else pool
    wm = wm or FakeWarpManager(count=3)
    return WarpHealthDaemon(
        wm, pool, check_interval=1, probe_interval=probe_interval,
        log=lambda *a, **k: None,
    )


def test_daemon_periodic_probe_runs_both_healers_and_resyncs():
    pool = _make_pool(3)
    wm = FakeWarpManager(count=3)
    daemon = _make_daemon(pool, wm)

    summary = probe.ProbeSummary(total=3, rate_limited=2, results=[
        probe.ProbeResult(proxy_id="warp-1", status="ok", exit_ip="104.28.231.146"),
        probe.ProbeResult(proxy_id="warp-2", status="rate_limited", exit_ip="104.28.231.142"),
        probe.ProbeResult(proxy_id="warp-3", status="rate_limited", exit_ip="104.28.231.142"),
    ])

    with mock.patch("warp.probe.probe_all", return_value=summary), \
         mock.patch("warp.probe.heal_rate_limited", return_value=2) as h_rl, \
         mock.patch("warp.probe.heal_expired", return_value=0) as h_exp, \
         mock.patch.object(daemon, "_check_and_heal") as cycle:
        daemon._probe_and_heal_burned_exits()

    h_rl.assert_called_once()
    h_exp.assert_called_once()
    # The pool re-sync cycle ran so freshly re-rolled exits are live immediately.
    cycle.assert_called_once()


def test_daemon_periodic_probe_skips_empty_pool():
    daemon = _make_daemon(pool=ProxyPool())
    with mock.patch("warp.probe.probe_all") as pa:
        daemon._probe_and_heal_burned_exits()
    pa.assert_not_called()


def test_daemon_probe_no_heal_skips_verify_and_resync():
    pool = _make_pool(1)
    daemon = _make_daemon(pool)
    summary = probe.ProbeSummary(total=1, healthy=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="ok"),
    ])
    with mock.patch("warp.probe.probe_all", return_value=summary), \
         mock.patch("warp.probe.heal_rate_limited", return_value=0), \
         mock.patch("warp.probe.heal_expired", return_value=0), \
         mock.patch.object(daemon, "_check_and_heal") as cycle:
        daemon._probe_and_heal_burned_exits()
    cycle.assert_not_called()


def test_daemon_loop_fires_probe_when_due():
    daemon = _make_daemon(probe_interval=0.05)
    daemon._warmup = False
    daemon._next_probe_at = 0.0  # due now
    fired = []

    def fake_cycle():
        fired.append("cycle")
        if len(fired) >= 2:
            daemon._stop.set()

    with mock.patch.object(daemon, "_check_and_heal", side_effect=fake_cycle), \
         mock.patch.object(
             daemon, "_probe_and_heal_burned_exits",
             side_effect=lambda: fired.append("probe"),
         ):
        daemon._run_loop()

    assert "probe" in fired
    # After firing, the next probe is scheduled an interval out.
    assert daemon._next_probe_at > 0


def test_daemon_loop_skips_probe_during_warmup():
    daemon = _make_daemon(probe_interval=0.05)
    daemon._warmup = True
    fired = []

    def fake_cycle():
        fired.append("cycle")
        if len(fired) >= 2:
            daemon._stop.set()

    with mock.patch.object(daemon, "_check_and_heal", side_effect=fake_cycle), \
         mock.patch.object(
             daemon, "_probe_and_heal_burned_exits",
             side_effect=lambda: fired.append("probe"),
         ):
        daemon._run_loop()

    assert "probe" not in fired


# ---------------------------------------------------------------------------
# Exit-lane formation
# ---------------------------------------------------------------------------

def test_formation_spreads_duplicates_onto_free_exits():
    """All slots on one exit + a known free exit -> formation moves a duplicate."""
    from warp import egress_map, formation

    egress_map.observe("7.7.7.7", "edge-a")     # known, currently unused
    pool = _make_pool(2)
    wm = FakeWarpManager(count=2)
    # Both slots currently egress via 8.8.8.8 (the default pile-up).
    exits = iter(["7.7.7.7"])
    with mock.patch("warp.formation._fetch_exit_ip",
                    side_effect=lambda url: next(exits, "8.8.8.8")), \
         mock.patch("warp.formation.time.sleep"):
        result = formation.form_distinct_exits(pool, wm, log=lambda *a, **k: None)
    # The mover landed on 7.7.7.7; occupancy split 1/1.
    assert result["distinct"] == 2, result
    assert sorted(result["lanes"]) == ["7.7.7.7", "8.8.8.8"]
    assert all(len(ids) == 1 for ids in result["lanes"].values())
    # Both slots stayed in the pool the whole time.
    assert pool.get_by_id("warp-1") is not None and pool.get_by_id("warp-2") is not None


def test_formation_is_a_noop_when_already_distinct():
    from warp import egress_map, formation

    pool = _make_pool(2)
    wm = FakeWarpManager(count=2)
    wm.re_roll_tunnel = mock.MagicMock()
    with mock.patch("warp.formation._fetch_exit_ip",
                    side_effect=lambda url: "8.8.8.8" if "51001" in url else "7.7.7.7"):
        result = formation.form_distinct_exits(pool, wm, log=lambda *a, **k: None)
    assert result["distinct"] == 2
    wm.re_roll_tunnel.assert_not_called()   # no duplicates -> no churn


def test_formation_explores_when_map_is_short():
    """Duplicates with no free known exit still explore unmapped edges."""
    from warp import egress_map, formation

    egress_map.observe("8.8.8.8", "edge-known")   # the pile-up exit
    pool = _make_pool(2)
    wm = FakeWarpManager(count=2)
    aimed = []
    calls = []
    def fake_roll(inst, attempt=0, endpoint=None, log=None):
        aimed.append(endpoint)
        return endpoint or "edge-x"
    wm.re_roll_tunnel = fake_roll
    def fake_trace(url):
        calls.append(url)
        # Measurement (first len(pool) calls): both slots on the pile-up.
        # The roll on warp-2 then discovers a brand-new exit.
        if len(calls) <= 2:
            return "8.8.8.8"
        return "7.7.7.1" if "51002" in url else "8.8.8.8"
    with mock.patch("warp.formation._fetch_exit_ip", side_effect=fake_trace), \
         mock.patch("warp.formation.time.sleep"):
        result = formation.form_distinct_exits(pool, wm, log=lambda *a, **k: None)
    assert result["distinct"] == 2, result
    assert aimed and aimed[0] not in ("edge-known", None)  # explored an unmapped edge
    # Discovery fed the map for next time.
    assert "7.7.7.1" in egress_map.known_exits()


def test_formation_stops_when_rolls_change_nothing():
    """A stuck dice (always the same exit) must not loop forever."""
    from core import config
    from warp import egress_map, formation

    egress_map.observe("8.8.8.8", "edge-a")
    pool = _make_pool(3)
    wm = FakeWarpManager(count=3)
    rolls = []
    wm.re_roll_tunnel = lambda inst, attempt=0, endpoint=None, log=None: \
        (rolls.append(1), "edge-a")[1]
    with mock.patch("warp.formation._fetch_exit_ip", return_value="8.8.8.8"), \
         mock.patch("warp.formation.time.sleep"):
        result = formation.form_distinct_exits(pool, wm, log=lambda *a, **k: None)
    assert result["distinct"] == 1
    # Bounded work: at most rounds x duplicates rolls, not an infinite loop.
    assert len(rolls) <= config.WARP_FORMATION_MAX_ROUNDS * 3


def test_formation_never_targets_a_burned_exit():
    """A rate-limited exit in the latest probe is not a lane target.

    Formation is quota-free and cannot see burns itself; without this it
    aimed duplicate slots straight onto the burned IP, handing the healer
    fresh work every pass (observed live as formation/healer thrash).
    """
    from warp import egress_map, formation, probe as probe_mod

    egress_map.observe("6.6.6.6", "edge-burned")   # known but burned
    pool = _make_pool(2)
    wm = FakeWarpManager(count=2)
    # Latest real-model probe: the burned exit is rate-limited.
    probe_mod._store(probe_mod.ProbeSummary(total=1, rate_limited=1, results=[
        probe_mod.ProbeResult(proxy_id="warp-1", status="rate_limited",
                              exit_ip="6.6.6.6"),
    ]))
    landed = []
    def fake_trace(url):
        # Both slots measure on 8.8.8.8; every roll lands on the burned
        # exit to prove formation keeps re-rolling past it.
        calls.append(url)
        return "8.8.8.8" if len(calls) <= 2 else "6.6.6.6"
    calls = []
    wm.re_roll_tunnel = lambda inst, attempt=0, endpoint=None, log=None: \
        (landed.append("6.6.6.6"), endpoint or "edge-burned")[1]
    with mock.patch("warp.formation._fetch_exit_ip", side_effect=fake_trace), \
         mock.patch("warp.formation.time.sleep"):
        result = formation.form_distinct_exits(pool, wm, log=lambda *a, **k: None)
    # The burned exit never became a lane, no matter that rolls landed on it.
    assert "6.6.6.6" not in result["lanes"], result["lanes"]


# ---------------------------------------------------------------------------
# resolve_probe_model -- the probe must verify lanes with a model OpenCode
# actually serves. A hardcoded pin rots the moment OpenCode pulls it behind a
# key, and then every lane reads probe_error and the pool looks dead while the
# egress is fine. These pin the resolution rule.
# ---------------------------------------------------------------------------

class _FreeModel:
    """Stand-in for a LogicalModel -- resolve_probe_model only reads .id and
    .reasoning off it (getattr-with-defaults), so this is all it needs."""
    def __init__(self, id, reasoning=False):
        self.id = id
        self.reasoning = reasoning


def test_resolve_probe_model_prefers_free_non_reasoning(monkeypatch):
    """Prefer a free, non-reasoning model from the live catalog: it answers the
    5-token probe immediately and is the least likely to be gated or to burn the
    probe's timeout thinking. The pin is the fallback, not the first choice."""
    monkeypatch.setattr(probe.config, "PROBE_MODEL", "deepseek-v4-flash-free")
    free = [
        _FreeModel("musespark-1.2-contributor-free", reasoning=True),
        _FreeModel("ling-3.0-flash-free", reasoning=False),
    ]
    assert probe.resolve_probe_model(free) == "ling-3.0-flash-free"


def test_resolve_probe_model_skips_reasoning_models(monkeypatch):
    """A reasoning model thinks before its first token -- sometimes silently --
    which stretches the probe and risks a watchdog/timeout on a lane that is
    fine. Reasoning models are skipped over when a non-reasoning free model
    exists, even when they appear first in the catalog order."""
    monkeypatch.setattr(probe.config, "PROBE_MODEL", "deepseek-v4-flash-free")
    free = [
        _FreeModel("o3-deep-reasoning-free", reasoning=True),
        _FreeModel("musespark-1.2-contributor-free", reasoning=True),
        _FreeModel("ling-3.0-flash-free", reasoning=False),
    ]
    assert probe.resolve_probe_model(free) == "ling-3.0-flash-free"


def test_resolve_probe_model_uses_reasoning_when_no_non_reasoning(monkeypatch):
    """OpenCode's free tier is currently all reasoning models. When every free
    model is reasoning the resolver must NOT fall back to the hardcoded pin --
    that pin may be gated, and using it makes every lane read probe_error (the
    trap convergence exists to escape). It uses a reasoning catalog model (one
    OpenCode is actually serving) instead, in catalog order, so convergence can
    still advance across models."""
    monkeypatch.setattr(probe.config, "PROBE_MODEL", "deepseek-v4-flash-free")
    free = [
        _FreeModel("big-pickle", reasoning=True),
        _FreeModel("nemotron-3-ultra-free", reasoning=True),
    ]
    assert probe.resolve_probe_model(free) == "big-pickle"
    assert probe.resolve_probe_model(
        free, exclude=frozenset({"big-pickle"})
    ) == "nemotron-3-ultra-free"


def test_resolve_probe_model_falls_back_to_pin_only_when_no_free_models(monkeypatch):
    """The configured pin is the last resort, used only when the catalog
    advertises no free model to probe with -- and as the explicit escape hatch
    when an operator wants a specific model probed."""
    monkeypatch.setattr(probe.config, "PROBE_MODEL", "deepseek-v4-flash-free")
    assert probe.resolve_probe_model(None) == "deepseek-v4-flash-free"
    assert probe.resolve_probe_model([]) == "deepseek-v4-flash-free"


def test_probe_timeout_extended_for_reasoning_probe_model(monkeypatch):
    """A reasoning probe model (OpenCode's free tier is all-reasoning) thinks
    before its first token; cutting it off at the tight PROBE_TIMEOUT reads as a
    dead lane and churns the healers (regenerating identities) for lanes that
    were merely thinking. _probe_timeout_for extends a reasoning model's per-lane
    budget when the extended timeout is larger, while a non-reasoning model keeps
    the tight base so a genuinely stuck lane is still caught fast."""
    monkeypatch.setattr(probe.config, "PROBE_TIMEOUT", 15.0)
    monkeypatch.setattr(probe.config, "PROBE_REASONING_TIMEOUT", 45.0)
    monkeypatch.setattr(probe.config, "LONG_THINKING_MODELS", frozenset())

    class _M:
        def __init__(self, reasoning):
            self.reasoning = reasoning

    class _Cat:
        def by_id(self, mid):
            return _M(True) if mid == "nemotron-3-ultra-free" else _M(False)

    cat = _Cat()
    # Non-reasoning -> tight base.
    assert probe._probe_timeout_for("x-free", cat, 15.0) == 15.0
    # Reasoning via the live catalog -> extended.
    assert probe._probe_timeout_for("nemotron-3-ultra-free", cat, 15.0) == 45.0
    # Reasoning via the LONG_THINKING_MODELS override (catalog says otherwise).
    monkeypatch.setattr(
        probe.config, "LONG_THINKING_MODELS",
        frozenset({"musespark-1.2-contributor-free"}),
    )
    assert probe._probe_timeout_for(
        "musespark-1.2-contributor-free", cat, 15.0,
    ) == 45.0
    # Override honored even without a catalog.
    assert probe._probe_timeout_for(
        "musespark-1.2-contributor-free", None, 15.0,
    ) == 45.0
    assert probe._probe_timeout_for("unknown", None, 15.0) == 15.0
    # Extension disabled (0, or <= base) -> base even for reasoning.
    monkeypatch.setattr(probe.config, "LONG_THINKING_MODELS", frozenset())
    monkeypatch.setattr(probe.config, "PROBE_REASONING_TIMEOUT", 0.0)
    assert probe._probe_timeout_for("nemotron-3-ultra-free", cat, 15.0) == 15.0
    monkeypatch.setattr(probe.config, "PROBE_REASONING_TIMEOUT", 10.0)  # <= base
    assert probe._probe_timeout_for("nemotron-3-ultra-free", cat, 15.0) == 15.0


def test_resolve_probe_model_excludes_ids(monkeypatch):
    """Convergence borrows another free model for the pass without retiring the
    rejected one, so resolve_probe_model must skip ids already tried. With a mix
    of non-reasoning and reasoning free models, a non-reasoning one is preferred;
    only when it (and the next non-reasoning one) are excluded does the resolver
    fall to a reasoning catalog model -- not the pin. The pin is the last resort,
    when every free model is excluded."""
    monkeypatch.setattr(probe.config, "PROBE_MODEL", "deepseek-v4-flash-free")
    free = [
        _FreeModel("deepseek-v4-flash-free", reasoning=False),
        _FreeModel("ling-3.0-flash-free", reasoning=False),
        _FreeModel("mimo-v2.5-free", reasoning=True),
    ]
    # Non-reasoning is preferred.
    assert probe.resolve_probe_model(free) == "deepseek-v4-flash-free"
    # First non-reasoning excluded -> the next non-reasoning one.
    assert probe.resolve_probe_model(
        free, exclude=frozenset({"deepseek-v4-flash-free"})
    ) == "ling-3.0-flash-free"
    # Both non-reasoning excluded -> fall to the reasoning free model, not pin.
    assert probe.resolve_probe_model(
        free, exclude=frozenset({"deepseek-v4-flash-free", "ling-3.0-flash-free"})
    ) == "mimo-v2.5-free"
    # Everything excluded -> the configured pin.
    assert probe.resolve_probe_model(
        free, exclude=frozenset(
            {"deepseek-v4-flash-free", "ling-3.0-flash-free", "mimo-v2.5-free"}
        )
    ) == "deepseek-v4-flash-free"
    # A reasoning model is skipped when a non-reasoning (non-excluded) one exists.
    assert probe.resolve_probe_model(
        free, exclude=frozenset({"ling-3.0-flash-free"}),
    ) == "deepseek-v4-flash-free"


# ---------------------------------------------------------------------------
# probe_all log line + total -- pins the log-vs-summary contradiction and the
# snapshot-vs-live-pool contradiction the dashboard showed.
# ---------------------------------------------------------------------------

def test_probe_all_logs_probe_error_lane_as_probe_error_not_dead():
    """The per-lane log line used to lump every non-ok/non-429 verdict into
    "dead", so a probe model rejection (probe_error) logged as
    ``probe: warp-2 -- dead (HTTP 400)`` while the summary line counted it as
    ``1 probe-error`` -- a direct contradiction an operator sees in the logs.
    The per-lane line now labels a probe_error as ``probe-error`` to match the
    summary, instead of mislabelling it as a dead tunnel (which would also
    trigger the wrong healer: a dead tunnel regenerates, a probe_error is left
    alone)."""
    pool = _make_pool(2)

    def fake_probe(url, pid, model, base, timeout):
        if "51001" in url:
            return probe.ProbeResult(proxy_id=pid, status="ok", latency_ms=10,
                                     probed_at=time.time())
        return probe.ProbeResult(proxy_id=pid, status="probe_error",
                                 error="HTTP 400", probed_at=time.time())

    lines = []

    def log(fmt, *args, **_kw):
        lines.append(fmt % args if args else fmt)

    with mock.patch("warp.probe._probe_single", side_effect=fake_probe), \
         mock.patch("warp.probe.config.WARP_VERBOSE", True):
        probe.probe_all(pool, log=log)

    per_lane = [ln for ln in lines if ln.startswith("probe: warp-2")]
    assert per_lane, lines
    assert "-- probe-error (HTTP 400)" in per_lane[0], per_lane[0]
    assert "-- dead" not in per_lane[0], per_lane[0]
    # The summary still aggregates it under probe-error.
    assert any("1 told-us-no" in ln for ln in lines), lines


def test_probe_all_counts_non_warp_lanes_in_total():
    """The startup probe ran in the WARP bootstrap thread while Tor lanes were
    still joining in a concurrent thread, so the cached snapshot said
    probe.total=10 while the live pool gauge showed 13/13 -- a dashboard
    contradiction. probe_all must count every lane the pool holds, including
    the non-warp Tor/SOCKS lanes, so a snapshot taken after they join reflects
    the real pool size (the Tor-join refresh in _bootstrap_tor relies on this)."""
    pool = _make_pool(2)                                  # warp-1, warp-2
    pool.add("socks5://127.0.0.1:51010", proxy_id="tor-1")
    pool.add("socks5://127.0.0.1:51011", proxy_id="tor-2")

    def fake_probe(url, pid, model, base, timeout):
        return probe.ProbeResult(proxy_id=pid, status="ok", latency_ms=5,
                                 probed_at=time.time())

    with mock.patch("warp.probe._probe_single", side_effect=fake_probe):
        summary = probe.probe_all(pool, log=lambda *a, **k: None)
    assert summary.total == 4, summary.total
    assert summary.healthy == 4


# ---------------------------------------------------------------------------
# probe-model convergence -- OpenCode keeps advertising free models it is
# temporarily not serving (under heavy load), so a probe pinned to one 400-s on
# every lane and reports the whole pool as probe_error while the egress is
# fine. probe_all now borrows another free model for the pass instead of
# leaving every lane read "probe?" -- it does NOT retire the rejected model,
# because a "-free" model that 400s while still listed is transient overload,
# not removal (OpenCode pulls a genuinely-dropped model from the free section
# outright; genuine removal is detected per-request by _flag_model_retired).
# ---------------------------------------------------------------------------

class _FakeCatalog:
    """Minimal stand-in for UnifiedCatalog used by resolve_probe_model +
    probe_all convergence. free() mirrors the real one's "exclude
    unavailable" rule, so resolve_probe_model naturally skips retired ids."""
    def __init__(self, models):
        self._all = list(models)
        self._unavail = set()
        self.marked = []          # ids handed to mark_unavailable, in order

    def free(self):
        return [m for m in self._all if m.id not in self._unavail]

    def by_id(self, model_id):
        # _probe_timeout_for reads .reasoning off the returned model.
        for m in self._all:
            if m.id == model_id:
                return m
        return None

    def mark_unavailable(self, model_id):
        self.marked.append(model_id)
        self._unavail.add(model_id)

    def is_unavailable(self, model_id):
        return model_id in self._unavail


def _probe_url_warp_index(url):
    """Pick the warp-N index out of a socks5://127.0.0.1:510NN url, so a fake
    _probe_single can give different lanes different verdicts."""
    return int(url.rsplit(":", 1)[-1]) - 51000


def test_probe_single_4xx_captures_the_upstream_reason():
    """A 400 used to be logged as a bare "HTTP 400", forcing a manual
    reproduction to learn *why* OpenCode rejected the probe. The body is now
    parsed so the per-lane log reads "probe-error (HTTP 400: This model is
    unavailable for free)" -- which immediately says the egress is fine and the
    model is the problem, not the tunnel."""
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 400
    mock_resp.json.return_value = {"error": {"message": "This model is unavailable for free"}}
    with mock.patch("warp.probe.socks5_connect_check", return_value=""), \
         mock.patch("warp.probe.httpx.Client") as MockClient:
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.MagicMock(return_value=ctx)
        ctx.__exit__ = mock.MagicMock(return_value=False)
        ctx.post.return_value = mock_resp
        MockClient.return_value = ctx
        result = probe._probe_single(
            "socks5://127.0.0.1:51001", "warp-1",
            "deepseek-v4-flash-free", "https://opencode.ai/zen/v1", 15,
        )
    assert result.status == "probe_error"
    assert "HTTP 400" in result.error
    assert "This model is unavailable for free" in result.error


def test_probe_all_borrows_another_model_when_every_lane_rejects_it():
    """When the probe model is rejected on *every* lane (all probe_error,
    nothing served/dead), the model -- not the pool -- is the problem:
    probe_all borrows the next free non-reasoning model for this pass so lane
    health is still reported. The rejected model is NOT retired -- a "-free"
    model that 400s while still listed is transient overload, not removal
    (OpenCode pulls a genuinely-dropped model from the free section outright),
    so it must stay routable; retiring it would hide it from routing/dispatcher
    for the full TTL just because the upstream was busy."""
    pool = _make_pool(2)
    cat = _FakeCatalog([
        _FreeModel("deepseek-v4-flash-free", reasoning=False),
        _FreeModel("ling-3.0-flash-free", reasoning=False),
    ])

    def fake_probe(url, pid, model, base, timeout):
        if model == "deepseek-v4-flash-free":
            return probe.ProbeResult(proxy_id=pid, status="probe_error",
                                     error="HTTP 400: unavailable for free",
                                     probed_at=time.time())
        return probe.ProbeResult(proxy_id=pid, status="ok", latency_ms=5,
                                 probed_at=time.time())

    with mock.patch("warp.probe._probe_single", side_effect=fake_probe):
        summary = probe.probe_all(pool, catalog=cat, log=lambda *a, **k: None)

    # NOT retired -- still routable and still offered by the catalog.
    assert cat.marked == []
    assert not cat.is_unavailable("deepseek-v4-flash-free")
    assert "deepseek-v4-flash-free" in [m.id for m in cat.free()]
    # The borrowed model verified both lanes.
    assert summary.healthy == 2
    assert summary.probe_error == 0
    assert summary.total == 2


def test_probe_all_stops_when_no_other_model_is_available_to_borrow(monkeypatch):
    """When the only free model is rejected on every lane there is nothing
    left to borrow for the pass. probe_all must stop rather than looping on the
    same rejected model -- max_model_attempts bounds it, but the tried-set exits
    as soon as resolve falls back to a model already tried (here the pin is the
    rejected model itself). The rejected model stays routable (not retired), and
    the pass reports probe_error (the probe could not verify, not that egress
    is down -- the lane is dead only if the tunnel is)."""
    monkeypatch.setattr(probe.config, "PROBE_MODEL", "deepseek-v4-flash-free")
    pool = _make_pool(2)
    cat = _FakeCatalog([_FreeModel("deepseek-v4-flash-free", reasoning=False)])

    def fake_probe(url, pid, model, base, timeout):
        return probe.ProbeResult(proxy_id=pid, status="probe_error",
                                 error="HTTP 400: unavailable for free",
                                 probed_at=time.time())

    sweeps = []
    real = probe._sweep_pool

    def counting_sweep(pool_, model, base, timeout_, log):
        sweeps.append(model)
        return real(pool_, model, base, timeout_, log)

    with mock.patch("warp.probe._probe_single", side_effect=fake_probe), \
         mock.patch("warp.probe._sweep_pool", side_effect=counting_sweep):
        summary = probe.probe_all(pool, catalog=cat, log=lambda *a, **k: None)

    assert cat.marked == []                       # not retired
    assert len(sweeps) == 1                      # did not loop on the rejected model
    assert summary.probe_error == 2
    assert summary.healthy == 0


def test_probe_all_borrows_a_reasoning_model_when_all_free_models_are_reasoning():
    """OpenCode's free tier is currently entirely reasoning models, so the
    first-resolve pass in resolve_probe_model finds nothing and it must use a
    reasoning catalog model (not the gated pin) -- otherwise convergence could
    never borrow a *different* model and every lane would read probe_error while
    the egress is fine. Pinned with two reasoning models: the first is gated on
    every lane, the second serves, so convergence borrows the second and the
    lanes verify healthy."""
    pool = _make_pool(2)
    cat = _FakeCatalog([
        _FreeModel("deepseek-v4-flash-free", reasoning=True),
        _FreeModel("nemotron-3-ultra-free", reasoning=True),
    ])

    def fake_probe(url, pid, model, base, timeout):
        if model == "deepseek-v4-flash-free":
            return probe.ProbeResult(proxy_id=pid, status="probe_error",
                                     error="HTTP 400: Model is unavailable",
                                     probed_at=time.time())
        return probe.ProbeResult(proxy_id=pid, status="ok", latency_ms=5,
                                 probed_at=time.time())

    swept = []
    real = probe._sweep_pool

    def counting_sweep(pool_, model, base, timeout_, log):
        swept.append(model)
        return real(pool_, model, base, timeout_, log)

    with mock.patch("warp.probe._probe_single", side_effect=fake_probe), \
         mock.patch("warp.probe._sweep_pool", side_effect=counting_sweep):
        summary = probe.probe_all(pool, catalog=cat, log=lambda *a, **k: None)

    assert swept == ["deepseek-v4-flash-free", "nemotron-3-ultra-free"]
    assert cat.marked == []                       # not retired -- stays routable
    assert summary.healthy == 2                    # the borrowed model verified lanes
    assert summary.probe_error == 0
    assert summary.total == 2


def test_probe_all_does_not_retire_when_any_lane_is_served():
    """A mixed pass (one healthy, one probe_error) is a genuinely mixed pool,
    not a dead model: a model swap would only mask the real lane's health. The
    model is NOT retired and there is no second sweep."""
    pool = _make_pool(2)
    cat = _FakeCatalog([_FreeModel("deepseek-v4-flash-free", reasoning=False)])

    def fake_probe(url, pid, model, base, timeout):
        idx = _probe_url_warp_index(url)
        if idx == 1:                         # warp-1: served -> ok
            return probe.ProbeResult(proxy_id=pid, status="ok", latency_ms=5,
                                     probed_at=time.time())
        return probe.ProbeResult(proxy_id=pid, status="probe_error",
                                 error="HTTP 400: unavailable for free",
                                 probed_at=time.time())

    with mock.patch("warp.probe._probe_single", side_effect=fake_probe):
        summary = probe.probe_all(pool, catalog=cat, log=lambda *a, **k: None)

    assert cat.marked == []                  # not retired
    assert summary.healthy == 1
    assert summary.probe_error == 1


def test_probe_all_without_catalog_sweeps_once_and_does_not_retry():
    """No catalog => no convergence path (the legacy behaviour). An all-probe_error
    pass is returned as-is rather than looping, since there is no other free
    model to borrow and nothing to converge against."""
    pool = _make_pool(2)

    def fake_probe(url, pid, model, base, timeout):
        return probe.ProbeResult(proxy_id=pid, status="probe_error",
                                 error="HTTP 400", probed_at=time.time())

    sweeps = []
    real = probe._sweep_pool

    def counting_sweep(pool_, model, base, timeout_, log):
        sweeps.append(model)
        return real(pool_, model, base, timeout_, log)

    with mock.patch("warp.probe._probe_single", side_effect=fake_probe), \
         mock.patch("warp.probe._sweep_pool", side_effect=counting_sweep):
        summary = probe.probe_all(pool, log=lambda *a, **k: None)

    assert len(sweeps) == 1                  # exactly one pass, no retry
    assert summary.probe_error == 2
    assert summary.healthy == 0


def test_probe_all_skips_a_model_retired_after_resolve():
    """A model may be unavailable at sweep time even though the caller resolved
    it -- seeded by the operator, or genuinely removed by a concurrent request
    (see _flag_model_retired). probe_all must re-resolve instead of sweeping a
    known-unavailable model, which would 400 the whole pool a second time.
    Pinned with an already-unavailable model so the guard fires on the very
    first iteration."""
    pool = _make_pool(2)
    cat = _FakeCatalog([
        _FreeModel("deepseek-v4-flash-free", reasoning=False),
        _FreeModel("ling-3.0-flash-free", reasoning=False),
    ])
    cat.mark_unavailable("deepseek-v4-flash-free")   # unavailable: seeded / genuinely removed

    swept = []
    real = probe._sweep_pool

    def counting_sweep(pool_, model, base, timeout_, log):
        swept.append(model)
        return real(pool_, model, base, timeout_, log)

    def fake_probe(url, pid, model, base, timeout):
        # ling is served; deepseek would 400 but must never reach the sweep.
        assert model == "ling-3.0-flash-free", model
        return probe.ProbeResult(proxy_id=pid, status="ok", latency_ms=5,
                                 probed_at=time.time())

    with mock.patch("warp.probe._probe_single", side_effect=fake_probe), \
         mock.patch("warp.probe._sweep_pool", side_effect=counting_sweep):
        summary = probe.probe_all(
            pool, model="deepseek-v4-flash-free", catalog=cat,
            log=lambda *a, **k: None,
        )

    assert swept == ["ling-3.0-flash-free"]          # deepseek never swept
    assert summary.healthy == 2
    assert summary.probe_error == 0


# ---------------------------------------------------------------------------
# Active-in-flight-stream guard: healers defer a busy egress
# ---------------------------------------------------------------------------


def _active_busy_for(*targets):
    """Patch the healers' view of the in-flight registry: report 1 (busy) for
    the named proxy ids and 0 (idle) for everything else. With no args every
    egress reads idle, exercising the control path."""
    busy = set(targets)
    return mock.patch(
        "warp.probe.active_streams.active",
        side_effect=lambda pid: 1 if pid in busy else 0,
    )


# ---------------------------------------------------------------------------
# heal_rate_limited: never re-roll a streaming (busy) burned exit
# ---------------------------------------------------------------------------

def test_heal_rate_limited_defers_busy_egress():
    """A burned exit with an open stream is left for the next pass. The tight
    re-roll loop never starts, so no wireproxy is torn down under the request."""
    pool = _make_pool(1)
    wm = FakeWarpManager(count=1)
    summary = probe.ProbeSummary(total=1, rate_limited=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="rate_limited",
                          exit_ip="104.28.231.142"),
    ])
    with _active_busy_for("warp-1"):
        healed = probe.heal_rate_limited(pool, summary, wm,
                                         log=lambda *a, **k: None)
    assert healed == 0
    assert wm.rerolls == []                      # the re-roll loop never entered
    assert wm.regenerated_indices == []
    assert pool.get_by_id("warp-1") is not None  # not removed
    r = summary.results[0]
    assert r.status == "rate_limited" and r.exit_ip == "104.28.231.142"
    assert summary.rate_limited == 1 and summary.healthy == 0


def test_heal_rate_limited_still_heals_when_idle():
    """Control: an idle burned exit escapes. The guard only blocks a streaming
    egress; a quiet one must still re-roll off its burned IP."""
    pool = _make_pool(1)
    wm = FakeWarpManager(count=1)
    summary = probe.ProbeSummary(total=1, rate_limited=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="rate_limited",
                          exit_ip="104.28.231.142"),
    ])
    exits = iter(["104.28.231.145"])
    checks = iter([probe.ProbeResult(proxy_id="warp-1", status="ok", latency_ms=10)])
    with _active_busy_for(), \
         mock.patch("warp.probe._fetch_exit_ip", side_effect=lambda url: next(exits)), \
         mock.patch("warp.probe.probe_proxy", side_effect=lambda url, pid, model=None: next(checks)), \
         mock.patch("warp.probe.time.sleep"):
        healed = probe.heal_rate_limited(pool, summary, wm,
                                         log=lambda *a, **k: None)
    assert healed == 1
    assert wm.rerolls and (1, 0) in wm.rerolls
    assert summary.results[0].status == "healed"


# ---------------------------------------------------------------------------
# heal_expired: never remove/regenerate a lane a stream is riding
# ---------------------------------------------------------------------------

def test_heal_expired_defers_busy_dead_slot():
    """The guard runs before remove+regenerate. A busy lane is left in the pool
    and keeps its verdict until the stream drains -- it is never killed under a
    request."""
    pool = _make_pool(2)
    wm = FakeWarpManager(count=2)
    summary = probe.ProbeSummary(total=2, healthy=1, dead=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="ok"),
        probe.ProbeResult(proxy_id="warp-2", status="dead", error="timeout"),
    ])
    with _active_busy_for("warp-2"):
        healed = probe.heal_expired(pool, summary, wm, log=lambda *a, **k: None)
    assert healed == 0
    assert wm.regenerated_indices == []
    assert pool.get_by_id("warp-2") is not None   # not removed under the stream
    assert summary.dead == 1 and summary.healthy == 1


def test_heal_expired_still_regenerates_when_idle():
    """Control: an idle dead tunnel is removed and regenerated."""
    pool = _make_pool(2)
    wm = FakeWarpManager(count=2)
    summary = probe.ProbeSummary(total=2, healthy=1, dead=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="ok"),
        probe.ProbeResult(proxy_id="warp-2", status="dead", error="timeout"),
    ])
    with _active_busy_for():                       # all idle
        healed = probe.heal_expired(pool, summary, wm, log=lambda *a, **k: None)
    assert healed == 1
    assert 2 in wm.regenerated_indices
    assert pool.get_by_id("warp-2") is None


# ---------------------------------------------------------------------------
# spread_distinct_exits: leave a busy duplicate on its current exit
# ---------------------------------------------------------------------------

def test_spread_defers_busy_duplicate():
    """The duplicate spread would move is streaming -- it stays put, no roll is
    attempted, and the summary keeps its healthy count (no spurious churn)."""
    from warp import egress_map

    egress_map.observe("9.9.9.9", "edge-free")
    pool = _make_pool(3)
    wm = FakeWarpManager(count=3)
    summary = probe.ProbeSummary(total=3, healthy=3, results=[
        probe.ProbeResult(proxy_id="warp-1", status="ok", exit_ip="3.3.3.3"),
        probe.ProbeResult(proxy_id="warp-2", status="ok", exit_ip="3.3.3.3"),
        probe.ProbeResult(proxy_id="warp-3", status="ok", exit_ip="4.4.4.4"),
    ])
    with _active_busy_for("warp-2"), \
         mock.patch("warp.probe.time.sleep"):
        moved = probe.spread_distinct_exits(pool, summary, wm,
                                            log=lambda *a, **k: None)
    assert moved == 0
    assert wm.rerolls == []                        # the busy duplicate was not rolled
    assert summary.healthy == 3


# ---------------------------------------------------------------------------
# rotate_burned_tor_lanes: never restart a Tor lane a stream is riding
# ---------------------------------------------------------------------------

class _FakeTorInstance:
    def __init__(self, index):
        self.index = index


class _FakeTorManager:
    def __init__(self, count=1):
        self.instances = [_FakeTorInstance(i + 1) for i in range(count)]
        self.restarts = []

    def restart_instance(self, inst, log=None):
        self.restarts.append(inst.index)
        return True


def _tor_pool(count=1):
    pool = ProxyPool()
    for i in range(count):
        pool.add(f"socks5://127.0.0.1:{9050 + i}", proxy_id=f"tor-{i + 1}")
    return pool


def test_rotate_burned_tor_lanes_defers_busy():
    """A Tor lane with an open stream is not restarted -- its route (and the
    live request) is preserved. The verdict stays until it can rotate."""
    pool = _tor_pool(1)
    tm = _FakeTorManager(1)
    summary = probe.ProbeSummary(total=1, rate_limited=1, results=[
        probe.ProbeResult(proxy_id="tor-1", status="rate_limited",
                          exit_ip="185.220.101.1"),
    ])
    with _active_busy_for("tor-1"):
        rotated = probe.rotate_burned_tor_lanes(pool, tm, summary,
                                                 log=lambda *a, **k: None)
    assert rotated == 0
    assert tm.restarts == []
    assert summary.results[0].status == "rate_limited"  # not healed under stream
    assert summary.rate_limited == 1


def test_rotate_burned_tor_lanes_heals_when_idle():
    """Control: an idle burned Tor lane rotates to a fresh exit."""
    pool = _tor_pool(1)
    tm = _FakeTorManager(1)
    summary = probe.ProbeSummary(total=1, rate_limited=1, results=[
        probe.ProbeResult(proxy_id="tor-1", status="rate_limited",
                          exit_ip="185.220.101.1"),
    ])
    with _active_busy_for():                        # all idle
        rotated = probe.rotate_burned_tor_lanes(pool, tm, summary,
                                                log=lambda *a, **k: None)
    assert rotated == 1
    assert tm.restarts == [1]
    assert summary.results[0].status == "healed"


def test_heal_expired_leaves_dead_when_regeneration_fails():
    """A regeneration that returns False (port-find / registration / restart-after-regen
    failure in WarpManager.regenerate_instance) must NOT chip the lane ``healed``.
    The identity never came back, so the dashboard must keep the dead verdict
    instead of an optimistic heal pill that hides a still-down lane until a real
    request rediscovered it at request time.
    """
    pool = _make_pool(1)
    wm = FakeWarpManager(count=1)
    wm.regenerate_instance = mock.MagicMock(return_value=False)
    summary = probe.ProbeSummary(total=1, dead=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="dead", error="timeout"),
    ])
    healed = probe.heal_expired(pool, summary, wm, log=lambda *a, **k: None)
    assert healed == 0
    assert wm.regenerate_instance.called
    assert summary.dead == 1 and summary.healed == 0
    assert summary.results[0].status == "dead"


def test_rotate_burned_tor_lanes_parks_lane_when_restart_fails():
    """A Tor restart that returns False leaves the lane on its burned exit. The
    rotator must park it via ``extend_cooldown`` so ``pick()`` skips it until the
    next probe pass; without that gate a live request lands on the still-burned
    exit and 429s before request-time mark_failure sees it. The verdict stays
    ``rate_limited`` (no optimistic ``healed`` pill: the lane did not recover).
    """
    pool = _tor_pool(1)
    tm = _FakeTorManager(1)
    tm.restart_instance = mock.MagicMock(return_value=False)
    summary = probe.ProbeSummary(total=1, rate_limited=1, results=[
        probe.ProbeResult(proxy_id="tor-1", status="rate_limited",
                          exit_ip="1.2.3.4"),
    ])
    rotated = probe.rotate_burned_tor_lanes(pool, tm, summary,
                                            log=lambda *a, **k: None)
    assert rotated == 0
    px = pool.get_by_id("tor-1")
    assert px.in_cooldown(time.time())              # parked out of pick()
    assert px.consecutive_failures == 0              # probe verdict did not bump
    assert summary.rate_limited == 1 and summary.healed == 0
    assert summary.results[0].status == "rate_limited"


def test_spread_parks_burned_lane_when_verify_says_rate_limited():
    """spread rolls a duplicate onto a free target exit, but the verify probe
    finds the new exit rate-limited. The moved lane must be parked via
    ``extend_cooldown`` (out of ``pick()`` until the next probe pass), the new
    exit added to ``burned`` so no other slot is aimed there too, and the
    stored summary rewritten so the dashboard chips the new exit as
    rate-limited instead of green. The verify verdict is a probe observation,
    not a fresh 429 -- no failure-tally bump.
    """
    from warp import egress_map

    egress_map.observe("9.9.9.9", "edge-free")
    pool = _make_pool(3)
    wm = FakeWarpManager(count=3)
    summary = probe.ProbeSummary(total=3, healthy=3, results=[
        probe.ProbeResult(proxy_id="warp-1", status="ok", exit_ip="3.3.3.3"),
        probe.ProbeResult(proxy_id="warp-2", status="ok", exit_ip="3.3.3.3"),
        probe.ProbeResult(proxy_id="warp-3", status="ok", exit_ip="4.4.4.4"),
    ])
    exits = iter(["9.9.9.9"])
    checks = iter([probe.ProbeResult(proxy_id="warp-2", status="rate_limited",
                                     error="HTTP 429", latency_ms=12.0)])
    with mock.patch("warp.probe._fetch_exit_ip", side_effect=lambda url: next(exits)), \
         mock.patch("warp.probe.probe_proxy",
                    side_effect=lambda url, pid, model=None: next(checks)), \
         mock.patch("warp.probe.time.sleep"):
        moved = probe.spread_distinct_exits(pool, summary, wm,
                                            log=lambda *a, **k: None)
    assert moved == 0
    by_id = {r.proxy_id: r for r in summary.results}
    r = by_id["warp-2"]
    assert r.status == "rate_limited"               # chip flipped to the new exit
    assert r.exit_ip == "9.9.9.9"
    assert r.error == "HTTP 429"
    assert r.latency_ms == 12.0
    assert summary.rate_limited == 1 and summary.healthy == 2
    px = pool.get_by_id("warp-2")
    assert px.in_cooldown(time.time())              # parked out of pick()
    assert px.consecutive_failures == 0             # no probe-bump to the tally
