"""Tests for the Tor egress lanes (warp.tor_egress) — hermetic, no network.

Every spawn is a mocked Popen; downloads are skipped by pre-planting a fake
tor.exe. What is under test is the lane lifecycle logic: seeding, cloning,
rotation, pool sync, and the healer/daemon integration around them.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock
import shutil

os.environ.setdefault("LINGLING_DATA_DIR", tempfile.mkdtemp(prefix="lingling-tor-test-"))
os.environ.setdefault("LINGLING_REQUIRE_KEY", "0")
os.environ.setdefault("LINGLING_BOOTSTRAP_WARP", "0")
os.environ.setdefault("LINGLING_TOR_ENABLED", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.proxy_pool import ProxyPool  # noqa: E402
from warp import tor_egress  # noqa: E402


class FakeProc:
    def __init__(self):
        self.pid = 4242
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def kill(self):
        self._alive = False

    def wait(self, timeout=None):
        return 0


def _manager(count=3, tools=True):
    root = Path(tempfile.mkdtemp(prefix="tor-mgr-"))
    mgr = tor_egress.TorEgressManager(root, count)
    if tools:
        mgr._tor_path().parent.mkdir(parents=True, exist_ok=True)
        mgr._tor_path().write_bytes(b"MZ fake")
    return mgr


def _running(mgr):
    for inst in mgr.instances:
        inst.process = FakeProc()
    return mgr


def test_torrc_pins_port_and_dir_and_no_country():
    mgr = _manager(1)
    rc = mgr._torrc(mgr.instances[0])
    assert "SocksPort 127.0.0.1:52001" in rc
    # A localhost, cookie-authed control port for fast NEWNYM rotation.
    assert "ControlPort 127.0.0.1:53001" in rc
    assert "CookieAuthentication 1" in rc
    assert "DataDirectory" in rc
    # Country pins stalled first circuits in live testing; they must stay out.
    assert "ExitNodes" not in rc


def test_ensure_tools_skips_when_binary_present():
    mgr = _manager(1, tools=True)
    with mock.patch("urllib.request.urlopen") as up:
        assert mgr.ensure_tools(log=lambda *a, **k: None) is True
    up.assert_not_called()


def test_start_all_bootstraps_stops_clones_then_spawns():
    """Fresh machine: seed instance is STOPPED before its cache is cloned.

    tor.exe memory-maps its cache files; cloning from a running instance
    fails on Windows (WinError 1224, seen live) and used to abort the whole
    bootstrap before lanes reached the pool.
    """
    mgr = _manager(3)
    spawned = []
    stopped = []
    def fake_popen(cmd, **kwargs):
        spawned.append(cmd)
        return FakeProc()
    def cache_grows(inst):
        # Fresh machine: nothing is cached until the first instance boots.
        return bool(spawned) and inst.index == 1
    with mock.patch("warp.tor_egress.subprocess.Popen", side_effect=fake_popen), \
         mock.patch("warp.tor_egress.time.sleep"), \
         mock.patch("warp.tor_egress.job", create=True), \
         mock.patch.object(mgr, "_cache_ready", side_effect=cache_grows), \
         mock.patch.object(mgr, "_stop",
                           side_effect=lambda inst: (
                               stopped.append(inst.index),
                               setattr(inst, "process", None),
                           )):
        result = mgr.start_all(log=lambda *a, **k: None)
    assert result["started"] == 3
    assert stopped == [1]                    # seed stopped before cloning
    assert len(spawned) == 4                 # seed + all three (seed respawned)
    assert all(i.process is not None for i in mgr.instances)


def test_warm_machine_skips_seeding_entirely():
    """Every instance dir already caches: no bootstrap, no copying, just spawn."""
    mgr = _manager(2)
    for inst in mgr.instances:
        inst.data_dir.mkdir(parents=True, exist_ok=True)
        (inst.data_dir / "cached-microdesc-consensus").write_text("consensus")
    with mock.patch("warp.tor_egress.subprocess.Popen", return_value=FakeProc()), \
         mock.patch("warp.tor_egress.job", create=True), \
         mock.patch.object(mgr, "_clone_cache") as clone:
        result = mgr.start_all(log=lambda *a, **k: None)
    assert result["started"] == 2
    clone.assert_not_called()


def test_clone_cache_skips_locked_files_without_failing():
    """A locked (memory-mapped) source file is skipped, not fatal."""
    mgr = _manager(2)
    src = mgr.instances[0].data_dir
    dst = mgr.instances[1].data_dir
    src.mkdir(parents=True, exist_ok=True)
    (src / "cached-microdescs").write_text("descs")
    real_copy = shutil.copy2
    def flaky(src_p, dst_p, *a, **k):
        if str(src_p).endswith("cached-microdescs"):
            raise OSError(1224, "user-mapped section open")
        return real_copy(src_p, dst_p, *a, **k)
    logs = []
    with mock.patch("warp.tor_egress.shutil.copy2", side_effect=flaky):
        mgr._clone_cache(src, dst, log=lambda *a, **k: logs.append(str(a)))
    # The locked file was skipped with a log line; no exception escaped.
    assert any("skipped" in l for l in logs)


def test_restart_rotates_exit_by_respawning():
    """When the control port is unavailable (no cookie / a lane started by a
    build whose torrc predates ControlPort / a dead process), the NEWNYM fast
    path falls back to kill+relaunch: the old process is killed and a new one
    takes its place. Hermetic -- no socket is opened because no cookie exists.
    """
    mgr = _running(_manager(1))
    old = mgr.instances[0].process
    with mock.patch("warp.tor_egress.subprocess.Popen", return_value=FakeProc()), \
         mock.patch("warp.tor_egress.job", create=True):
        assert mgr.restart_instance(mgr.instances[0], log=lambda *a, **k: None) is True
    assert old._alive is False                 # killed ...
    assert mgr.instances[0].process is not old  # ... and replaced


def test_send_newnym_returns_false_without_a_cookie():
    """No control_auth_cookie -> NEWNYM cannot authenticate, so restart must
    fall back to kill+relaunch instead of holding the heal lock on a socket
    timeout. Returns False before any socket is opened (hermetic, no network)."""
    mgr = _running(_manager(1))
    inst = mgr.instances[0]
    assert inst.control_port == inst.port + 1000   # 53001, one block above SOCKS
    # process is up, control port configured, but no cookie was ever written.
    assert mgr._send_newnym(inst, log=lambda *a, **k: None) is False


def test_newnym_rotates_exit_without_respawning():
    """SIGNAL NEWNYM rotates the exit IP without killing the lane: the SOCKS
    port and process stay up, so a burned Tor lane heals in under a second
    instead of the ~5-10s kill+relaunch serialized under the heal lock."""
    mgr = _running(_manager(1))
    inst = mgr.instances[0]
    old = inst.process
    with mock.patch.object(mgr, "_send_newnym", return_value=True) as nm, \
         mock.patch.object(mgr, "_kill_and_respawn") as respawn:
        assert mgr.restart_instance(inst, log=lambda *a, **k: None) is True
    nm.assert_called_once()
    respawn.assert_not_called()
    assert inst.process is old and old._alive is True


def test_sync_to_pool_adds_running_lanes_once():
    mgr = _running(_manager(3))
    pool = ProxyPool()
    assert mgr.sync_to_pool(pool, log=lambda *a, **k: None) == 3
    assert pool.get_by_id("tor-1") is not None
    assert mgr.sync_to_pool(pool, log=lambda *a, **k: None) == 0  # idempotent


def test_healers_leave_tor_lanes_alone():
    """A 429/dead Tor lane must not be deleted by the WARP healers.

    The daemon rotates Tor lanes through their own manager; the probe-module
    healers only understand WARP identities and used to remove unknown ids
    outright -- silently deleting every extra lane on the first 429.
    """
    from tests.test_probe import FakeWarpManager, _make_pool
    from warp import probe

    pool = _make_pool(1)
    pool.add("socks5://127.0.0.1:52001", proxy_id="tor-1", label="Tor lane")
    wm = FakeWarpManager(count=1)
    summary = probe.ProbeSummary(total=1, rate_limited=1, dead=1, results=[
        probe.ProbeResult(proxy_id="tor-1", status="rate_limited", exit_ip="1.2.3.4"),
        probe.ProbeResult(proxy_id="tor-1", status="dead", exit_ip=""),
    ])
    rl = probe.heal_rate_limited(pool, summary, wm, log=lambda *a, **k: None)
    exp = probe.heal_expired(pool, summary, wm, log=lambda *a, **k: None)
    assert rl == 0 and exp == 0
    assert pool.get_by_id("tor-1") is not None, "a Tor lane was deleted by a WARP healer"


def test_daemon_rotates_burned_tor_lanes():
    from warp import probe
    from warp.health import WarpHealthDaemon

    class FakeTor:
        def __init__(self):
            self.instances = [type("I", (), {"index": 1})()]
            self.restarts = []

        def restart_instance(self, inst, log=None):
            self.restarts.append(inst.index)
            return True

    tor = FakeTor()
    daemon = WarpHealthDaemon(
        type("W", (), {"instances": []})(), ProxyPool(),
        tor_manager=tor, log=lambda *a, **k: None,
    )
    daemon.pool.add("socks5://127.0.0.1:52001", proxy_id="tor-1", label="Tor")
    daemon.pool.mark_failure(daemon.pool.get_by_id("tor-1"), 429)
    summary = probe.ProbeSummary(total=1, rate_limited=1, results=[
        probe.ProbeResult(proxy_id="tor-1", status="rate_limited", exit_ip="1.2.3.4"),
    ])
    assert daemon._rotate_burned_tor_lanes(summary) == 1
    assert tor.restarts == [1]
    # The rotation reset the lane's burn counters (fresh exit, fresh slate).
    after = daemon.pool.get_by_id("tor-1")
    assert after.consecutive_failures == 0 and after.total_429 == 0


def _fake_tor(indexes):
    """Minimal TorEgressManager stand-in: records which lanes got restarted."""
    class FakeTorManager:
        def __init__(self):
            self.instances = [type("I", (), {"index": i})() for i in indexes]
            self.restarts = []

        def restart_instance(self, inst, log=None):
            self.restarts.append(inst.index)
            return True

    return FakeTorManager()


def test_rotate_burned_tor_lanes_function_restarts_burned_only():
    """The module-level rotator restarts lanes marked rate_limited/dead and
    leaves healthy ones alone -- the same contract the daemon method had."""
    from providers.proxy_pool import ProxyPool
    from warp import probe

    tor = _fake_tor([1, 2, 3])
    pool = ProxyPool()
    for i in (1, 2, 3):
        pool.add(f"socks5://127.0.0.1:{52000 + i}", proxy_id=f"tor-{i}", label=f"Tor #{i}")
    # Burn two of them so we can confirm the third is left untouched.
    pool.mark_failure(pool.get_by_id("tor-1"), 429)
    pool.mark_failure(pool.get_by_id("tor-3"), 429)

    summary = probe.ProbeSummary(total=3, rate_limited=1, dead=1, results=[
        probe.ProbeResult(proxy_id="tor-1", status="rate_limited"),
        probe.ProbeResult(proxy_id="tor-2", status="ok"),
        probe.ProbeResult(proxy_id="tor-3", status="dead"),
    ])
    rotated = probe.rotate_burned_tor_lanes(
        pool, tor, summary, log=lambda *a, **k: None,
    )
    assert rotated == 2
    assert tor.restarts == [1, 3]                       # healthy tor-2 left alone
    for pid in ("tor-1", "tor-3"):
        after = pool.get_by_id(pid)
        assert after.consecutive_failures == 0
        assert after.total_429 == 0
    # The stored summary must reflect the heals immediately -- the dashboard
    # reads it, and without this the lanes keep their stale rate-limited/dead
    # verdicts (red chips) until the next probe pass verifies the fresh exits.
    assert summary.rate_limited == 0 and summary.dead == 0
    by_id = {r.proxy_id: r for r in summary.results}
    assert by_id["tor-1"].status == "healed" and by_id["tor-1"].exit_ip == ""
    assert by_id["tor-3"].status == "healed"
    assert by_id["tor-2"].status == "ok"               # untouched


def test_rotate_burned_tor_lanes_noop_when_tor_disabled():
    """Callers (startup, daemon, dashboard) must be able to invoke the
    rotator unconditionally -- it returns 0 silently when Tor is off."""
    from providers.proxy_pool import ProxyPool
    from warp import probe

    pool = ProxyPool()
    summary = probe.ProbeSummary(total=0, results=[])

    # None tor_manager (the path the daemon takes when LINGLING_TOR_ENABLED=0).
    assert probe.rotate_burned_tor_lanes(pool, None, summary, log=lambda *a, **k: None) == 0

    # A manager with zero instances (the path startup takes with TOR_COUNT=0).
    class Empty:
        instances = []

    assert probe.rotate_burned_tor_lanes(pool, Empty(), summary, log=lambda *a, **k: None) == 0


def test_rotate_burned_tor_lanes_never_touches_warp_or_unknown_ids():
    """WARP lanes and user proxies are not the Tor rotator's job; the WARP
    healers own them, and an unknown id must not cause a restart."""
    from providers.proxy_pool import ProxyPool
    from warp import probe

    tor = _fake_tor([1])
    pool = ProxyPool()
    pool.add("socks5://127.0.0.1:51001", proxy_id="warp-1", label="WARP #1")
    pool.add("socks5://127.0.0.1:53099", proxy_id="custom-proxy", label="user")
    pool.mark_failure(pool.get_by_id("warp-1"), 429)

    summary = probe.ProbeSummary(total=2, rate_limited=1, dead=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="rate_limited"),
        probe.ProbeResult(proxy_id="custom-proxy", status="dead"),
    ])
    assert probe.rotate_burned_tor_lanes(pool, tor, summary, log=lambda *a, **k: None) == 0
    assert tor.restarts == []
    # The burned WARP lane's counters are intact -- the Tor healer did nothing.
    assert pool.get_by_id("warp-1").total_429 == 1


def test_bootstrap_warp_rotates_burned_tor_lanes():
    """Startup must run the Tor rotator alongside the WARP healers.

    Previously ``_bootstrap_warp`` called only ``heal_expired`` +
    ``heal_rate_limited`` + ``spread_distinct_exits`` -- all three skip
    non-WARP lanes by design ("the daemon rotates them through their own
    manager instead"). That left a Tor lane the startup probe already saw
    429'd sitting unhealed until the daemon's first periodic probe fired
    ``PROBE_INTERVAL_S`` seconds later. This pins the wiring so it cannot
    regress: startup invokes ``rotate_burned_tor_lanes`` directly with the
    startup probe summary, and with the same pool+tor_manager the WARP
    healers saw.
    """
    import app
    from warp import probe

    summary = probe.ProbeSummary(total=2, rate_limited=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="ok"),
        probe.ProbeResult(proxy_id="tor-3", status="rate_limited",
                          exit_ip="45.66.35.27"),
    ])

    fake_tor = mock.MagicMock(spec=[])
    fake_pool = mock.MagicMock()
    fake_pool.__len__.return_value = 1
    fake_pool.status.return_value = {"total": 2, "available": 2, "healthy": 2}
    fake_warp = mock.MagicMock()
    fake_warp.count = 10
    fake_warp.status.return_value = {
        "identities_registered": 10, "proxies_running": 0,
    }

    with mock.patch.object(app, "warp_manager", fake_warp), \
         mock.patch.object(app, "proxy_pool", fake_pool), \
         mock.patch.object(app, "tor_manager", fake_tor), \
         mock.patch("app._start_warp_at_startup"), \
         mock.patch("warp.probe.probe_all", return_value=summary), \
         mock.patch("warp.probe.heal_expired", return_value=0), \
         mock.patch("warp.probe.heal_rate_limited", return_value=0), \
         mock.patch("warp.probe.rotate_burned_tor_lanes", return_value=0) as h_tor, \
         mock.patch("warp.probe.spread_distinct_exits", return_value=0), \
         mock.patch.object(app.config, "PROBE_ON_STARTUP", True), \
         mock.patch.object(app.config, "WARP_FORM_ON_STARTUP", False), \
         mock.patch.object(app.usage_store, "log"), \
         mock.patch.object(app.warp_health_daemon, "start"):
        app._bootstrap_warp()

    h_tor.assert_called_once()
    args, _kwargs = h_tor.call_args
    assert args[0] is fake_pool                       # proxy_pool, ...
    assert args[1] is fake_tor                         # tor_manager, ...
    assert args[2] is summary                          # summary
