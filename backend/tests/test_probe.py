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


# ---------------------------------------------------------------------------
# _probe_single tests (mocked httpx)
# ---------------------------------------------------------------------------

def test_probe_single_200_ok():
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    with mock.patch("warp.probe.httpx.Client") as MockClient:
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
    with mock.patch("warp.probe.httpx.Client") as MockClient:
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
    with mock.patch("warp.probe.httpx.Client") as MockClient:
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
    with mock.patch("warp.probe.httpx.Client") as MockClient:
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
    with mock.patch("warp.probe.httpx.Client") as MockClient:
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
         mock.patch("warp.probe.probe_proxy", side_effect=lambda url, pid: next(checks)), \
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
         mock.patch("warp.probe.probe_proxy", side_effect=lambda url, pid: next(checks)), \
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
         mock.patch("warp.probe.probe_proxy", side_effect=lambda url, pid: next(checks)), \
         mock.patch("warp.probe.time.sleep"):
        moved = probe.spread_distinct_exits(pool, summary, wm, log=lambda *a, **k: None)
    assert moved == 1
    assert aimed == ["edge-free"]     # the roll was aimed at the known edge
    assert rolled == [2]              # the duplicate moved, not the singleton


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
         mock.patch("warp.probe.probe_proxy", side_effect=lambda url, pid: next(checks)), \
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
