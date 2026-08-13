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


class FakeWarpManager:
    def __init__(self, count=3):
        self.instances = [FakeWarpInstance(i + 1, 51001 + i) for i in range(count)]
        self.count = count
        self.regenerated_indices = []

    def regenerate_instance(self, inst):
        inst.regenerated = True
        self.regenerated_indices.append(inst.index)


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


def test_heal_rate_limited_removes_and_regenerates():
    pool = _make_pool(3)
    wm = FakeWarpManager(count=3)
    summary = probe.ProbeSummary(total=3, rate_limited=2, results=[
        probe.ProbeResult(proxy_id="warp-1", status="ok"),
        probe.ProbeResult(proxy_id="warp-2", status="rate_limited"),
        probe.ProbeResult(proxy_id="warp-3", status="rate_limited"),
    ])
    healed = probe.heal_rate_limited(pool, summary, wm, log=lambda *a, **k: None)
    assert healed == 2
    assert set(wm.regenerated_indices) == {2, 3}
    assert pool.get_by_id("warp-1") is not None
    assert pool.get_by_id("warp-2") is None
    assert pool.get_by_id("warp-3") is None


def test_heal_expired_skips_nonexistent():
    pool = _make_pool(1)
    wm = FakeWarpManager(count=3)
    summary = probe.ProbeSummary(total=1, results=[
        probe.ProbeResult(proxy_id="warp-99", status="dead"),
    ])
    healed = probe.heal_expired(pool, summary, wm, log=lambda *a, **k: None)
    assert healed == 0


def test_heal_logs_regeneration_failure():
    pool = _make_pool(1)
    wm = FakeWarpManager(count=3)
    wm.regenerate_instance = mock.MagicMock(side_effect=RuntimeError("wgcf crashed"))
    summary = probe.ProbeSummary(total=1, results=[
        probe.ProbeResult(proxy_id="warp-1", status="rate_limited"),
    ])
    logs = []
    healed = probe.heal_rate_limited(pool, summary, wm, log=lambda *a, **k: logs.append(str(a)))
    assert healed == 0
    assert any("failed" in l for l in logs)


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
            return probe.ProbeResult(proxy_id=pid, status="ok", latency_ms=50, probed_at=time.time())
        elif "51002" in url:
            return probe.ProbeResult(proxy_id=pid, status="rate_limited", probed_at=time.time())
        else:
            return probe.ProbeResult(proxy_id=pid, status="dead", error="connect error", probed_at=time.time())

    with mock.patch("warp.probe._probe_single", side_effect=fake_probe):
        summary = probe.probe_all(pool, log=lambda *a, **k: None)

    assert summary.healthy == 1
    assert summary.rate_limited == 1
    assert summary.dead == 1

    expired_healed = probe.heal_expired(pool, summary, wm, log=lambda *a, **k: None)
    rl_healed = probe.heal_rate_limited(pool, summary, wm, log=lambda *a, **k: None)

    assert expired_healed == 1
    assert rl_healed == 1
    assert set(wm.regenerated_indices) == {2, 3}
    assert pool.get_by_id("warp-1") is not None
    assert pool.get_by_id("warp-2") is None
    assert pool.get_by_id("warp-3") is None


def test_probe_config_defaults():
    from core import config
    assert config.PROBE_ON_STARTUP is True
    assert config.PROBE_MODEL == "deepseek-v4-flash-free"
    assert config.PROBE_TIMEOUT > 0
