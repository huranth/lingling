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
    mgr = _running(_manager(1))
    old = mgr.instances[0].process
    with mock.patch("warp.tor_egress.subprocess.Popen", return_value=FakeProc()), \
         mock.patch("warp.tor_egress.job", create=True):
        assert mgr.restart_instance(mgr.instances[0], log=lambda *a, **k: None) is True
    assert old._alive is False                 # killed ...
    assert mgr.instances[0].process is not old  # ... and replaced


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
