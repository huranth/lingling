"""Hermetic tests for the active-in-flight-stream guard.

The guard defers a re-roll/restart/heal whenever the egress has an open stream,
so a long stream (MuseSpark's hidden-reasoning) is no longer killed under by the
60s/300s healers tearing down its wireproxy. These tests pin three things:

1. **stream_chat** bumps the count for the whole stream lifetime and drops it on
   every exit path (clean close, upstream 4xx, httpx failure) -- an imbalance
   here is the only way a live stream could be left under-counted and killed.
2. **formation._roll_to** returns the slot's unchanged exit and never re-rolls
   a busy egress.
3. **WarpHealthDaemon** healers (_heal_instance / _ensure_min_healthy /
   _dump_burned_identities) defer while busy, and still heal when idle.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("LINGLING_DATA_DIR", tempfile.mkdtemp(prefix="lingling-asg-test-"))
os.environ.setdefault("LINGLING_REQUIRE_KEY", "0")
os.environ.setdefault("LINGLING_BOOTSTRAP_WARP", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from providers import active_streams  # noqa: E402
from providers.base import OpenAICompatibleProvider, UpstreamError  # noqa: E402
from providers.key_pool import KeyPool  # noqa: E402
from providers.proxy_pool import ProxyPool  # noqa: E402


# ---------------------------------------------------------------------------
# stream_chat: the inc/dec bracketing that keeps a live stream accurate
# ---------------------------------------------------------------------------

class _StreamProv(OpenAICompatibleProvider):
    id = "test"
    display_name = "test"
    base_url = "http://upstream.invalid"
    # keyless transport -- auth_headers returns the UA stub only.

    # The abstract Provider method is irrelevant to streaming; stub it so the
    # class is concrete (we only exercise stream_chat here).
    def is_model_free(self, model_id, meta):
        return False


class _FakeResp:
    """Stands in for the httpx streaming response inside client.stream()."""

    def __init__(self, lines=None, status_code=200, exc=None,
                 read_body=b"", on_iter=None, headers=None):
        self.status_code = status_code
        # Mirrors the real httpx streaming response: stream_chat reads
        # ``.headers`` to honor an upstream Retry-After on a 4xx/5xx.
        self.headers = headers if headers is not None else {}
        self._lines = lines or []
        self._exc = exc
        self._read_body = read_body
        self._on_iter = on_iter   # fired once at the top of iter_lines()

    def iter_lines(self):
        if self._on_iter is not None:
            self._on_iter()
        if self._exc is not None:
            raise self._exc
        for line in self._lines:
            yield line

    def read(self):
        return self._read_body


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    @contextlib.contextmanager
    def stream(self, method, url, json=None, headers=None):
        yield self._resp


def _patch_pool(resp):
    """Repoint get_connection_pool at a fake pool that hands out _FakeClient."""
    class _FakePool:
        @contextlib.contextmanager
        def get_client(self, proxy_id, proxy_url, timeout):
            yield _FakeClient(resp)
    return _FakePool()


def test_stream_chat_inc_before_stream_dec_after_clean_close():
    """On a normal end-of-stream the count is back to zero, and it was
    elevated for the whole stream lifetime (inc before the body opened)."""
    prov = _StreamProv(KeyPool())
    seen = []
    resp = _FakeResp(lines=["data: {}", "data: [DONE]"],
                     on_iter=lambda: seen.append(dict(active_streams.snapshot())))
    with mock.patch("providers.connection_pool.get_connection_pool",
                     return_value=_patch_pool(resp)):
        chunks = list(
            prov.stream_chat(
                [{"role": "user", "content": "hi"}], "m", "", timeout=5,
                proxy_id="warp-3", proxy_url="socks5://127.0.0.1:1234",
            )
        )
    assert chunks == [b"data: {}", b"data: [DONE]"]
    assert seen == [{"warp-3": 1}]               # elevated the whole body
    assert active_streams.active("warp-3") == 0  # released on close


def test_stream_chat_dec_on_upstream_4xx_error():
    """A 4xx before the first chunk raises UpstreamError -- the finally must
    still release the count, or the next heal under that egress would never
    fire (permanently over-counted)."""
    prov = _StreamProv(KeyPool())
    resp = _FakeResp(status_code=429, read_body=b'rate limited')
    with mock.patch("providers.connection_pool.get_connection_pool",
                     return_value=_patch_pool(resp)):
        gen = prov.stream_chat(
            [{"role": "user", "content": "hi"}], "m", "", timeout=5,
            proxy_id="warp-3", proxy_url="socks5://127.0.0.1:1234",
        )
        try:
            list(gen)
        except UpstreamError as exc:
            assert exc.status_code == 429
        else:
            assert False, "expected UpstreamError(429)"
    assert active_streams.active("warp-3") == 0


def test_stream_chat_dec_on_httpx_transport_failure():
    """An httpx transport error mid-stream is rewrapped as UpstreamError(504);
    the count must be released, not pinned at 1 for the egress's lifetime."""
    prov = _StreamProv(KeyPool())
    seen = []
    resp = _FakeResp(exc=httpx.ConnectError("socket died"),
                     on_iter=lambda: seen.append(dict(active_streams.snapshot())))
    with mock.patch("providers.connection_pool.get_connection_pool",
                     return_value=_patch_pool(resp)):
        gen = prov.stream_chat(
            [{"role": "user", "content": "hi"}], "m", "", timeout=5,
            proxy_id="warp-3", proxy_url="socks5://127.0.0.1:1234",
        )
        try:
            list(gen)
        except UpstreamError as exc:
            assert exc.status_code == 504
        else:
            assert False, "expected UpstreamError(504)"
    # The count was already inc'd when the transport died (inc precedes the
    # stream body), then dropped in the finally as the exception unwound.
    assert seen == [{"warp-3": 1}]
    assert active_streams.active("warp-3") == 0


def test_stream_chat_direct_request_does_not_enter_registry():
    """A direct (non-proxied) request passes no proxy_url, so there is no
    re-rollable egress to protect -- it must not touch the registry at all
    (no warp-/tor- id ever tracked, no transient _direct_ counter)."""
    prov = _StreamProv(KeyPool())
    resp = _FakeResp(lines=["data: {}"])
    with mock.patch("providers.connection_pool.get_connection_pool",
                     return_value=_patch_pool(resp)):
        mid = []
        gen = prov.stream_chat([{"role": "user", "content": "hi"}], "m", "",
                               timeout=5)
        # Record the registry at the halfway mark -- still empty mid-stream.
        for _ in gen:
            mid.append(dict(active_streams.snapshot()))
    assert mid == [{}]                      # untouched the whole time
    assert active_streams.snapshot() == {}  # and balanced after


# ---------------------------------------------------------------------------
# formation._roll_to: leave a busy egress on its current exit
# ---------------------------------------------------------------------------

def test_roll_to_returns_unchanged_exit_when_busy():
    """A streaming slot is never re-rolled by formation -- it stays put and
    formation breaks that round rather than killing the live stream."""
    from warp import formation
    inst = mock.Mock()
    inst.index = 7
    px = mock.Mock()
    px.id = "warp-7"
    px.url = "socks5://127.0.0.1:51007"
    slot = {"instance": inst, "proxy": px, "exit_ip": "8.8.8.8"}
    wm = mock.Mock()
    active_streams.inc("warp-7")
    try:
        with mock.patch("warp.formation.time.sleep"):
            got = formation._roll_to(wm, slot, lambda ip: True, ["edge-a"],
                                     log=lambda *a, **k: None)
    finally:
        active_streams.dec("warp-7")
    assert got == "8.8.8.8"                       # unchanged
    wm.re_roll_tunnel.assert_not_called()         # never killed it
    assert slot["exit_ip"] == "8.8.8.8"           # slot not mutated


def test_roll_to_re_rolls_idle_egress():
    """Control: an idle egress proceeds normally -- the guard only protects
    exits that are actually streaming."""
    from warp import formation
    inst = mock.Mock()
    inst.index = 7
    px = mock.Mock()
    px.id = "warp-7"
    px.url = "socks5://127.0.0.1:51007"
    slot = {"instance": inst, "proxy": px, "exit_ip": "8.8.8.8"}
    wm = mock.Mock()
    wm.re_roll_tunnel.return_value = "edge-a"
    with mock.patch("warp.formation._fetch_exit_ip", return_value="9.9.9.9"), \
         mock.patch("warp.formation.time.sleep"):
        got = formation._roll_to(wm, slot, lambda ip: ip == "9.9.9.9", ["edge-a"],
                                 log=lambda *a, **k: None)
    assert got == "9.9.9.9"
    wm.re_roll_tunnel.assert_called_once()


# ---------------------------------------------------------------------------
# WarpHealthDaemon: defer healers under a live stream, heal when idle
# ---------------------------------------------------------------------------

class _Inst:
    def __init__(self, index, port=51001):
        self.index = index
        self.port = port


def _make_daemon(wm, pool, logs):
    """Construct just enough of a WarpHealthDaemon to drive the three healers;
    __init__ spawns threads/stop-flags we don't need here."""
    from warp.health import WarpHealthDaemon
    d = WarpHealthDaemon.__new__(WarpHealthDaemon)
    d.warp = wm
    d.pool = pool
    d.log = lambda *a, **k: logs.append(str(a))
    return d


def _burn_pool_one():
    """A pool with one warp-1 proxy already over the burn thresholds."""
    pool = ProxyPool()
    px = pool.add("socks5://127.0.0.1:51001", proxy_id="warp-1", label="WARP #1")
    px.consecutive_failures = 999
    px.total_429 = 999
    return pool


def test_dump_burned_busy_defers_both_re_roll_and_remove():
    """The key regression: the None branch of re_roll_tunnel drops the proxy
    from the pool. A deferred instance must skip that branch entirely -- no
    re-roll attempt AND no pool.remove -- so the streaming slot survives."""
    logs = []
    wm = mock.Mock()
    wm.instances = [_Inst(1)]
    # Even a re-roll that would normally succeed must not be attempted.
    wm.re_roll_tunnel = mock.Mock(return_value="edge-x")
    pool = _burn_pool_one()
    daemon = _make_daemon(wm, pool, logs)

    active_streams.inc("warp-1")
    dumped = daemon._dump_burned_identities()

    assert dumped == 0
    wm.re_roll_tunnel.assert_not_called()
    px = pool.get_by_id("warp-1")
    assert px is not None                      # never removed
    # reset_counters ran in NO branch of this deferred path: the burn tally the
    # health daemon uses to decide to dump again is left exactly as it was.
    assert px.total_429 == 999
    assert px.consecutive_failures == 999
    assert any("deferred" in m for m in logs)


def test_dump_burned_idle_re_rolls_and_resets():
    """Control: an idle burned exit is re-rolled and its counters reset."""
    logs = []
    wm = mock.Mock()
    wm.instances = [_Inst(1)]
    wm.re_roll_tunnel = mock.Mock(return_value="edge-x")
    pool = _burn_pool_one()
    daemon = _make_daemon(wm, pool, logs)

    dumped = daemon._dump_burned_identities()

    assert dumped == 1
    wm.re_roll_tunnel.assert_called_once()
    assert pool.get_by_id("warp-1") is not None
    assert pool.get_by_id("warp-1").total_429 == 0
    assert pool.get_by_id("warp-1").consecutive_failures == 0


def test_dump_burned_idle_removes_when_tunnel_will_not_come_back():
    """Control: a re-roll that fails the tunnel is removed so the dead-tunnel
    path regenerates it -- this removal is exactly what the guard defers under
    a stream, so it must only happen when the egress is truly idle."""
    logs = []
    wm = mock.Mock()
    wm.instances = [_Inst(1)]
    wm.re_roll_tunnel = mock.Mock(return_value=None)   # tunnel dead
    pool = _burn_pool_one()
    daemon = _make_daemon(wm, pool, logs)

    dumped = daemon._dump_burned_identities()

    assert dumped == 0
    wm.re_roll_tunnel.assert_called_once()
    assert pool.get_by_id("warp-1") is None              # removed for regeneration


def test_heal_instance_busy_does_not_restart_wireproxy():
    """A streaming egress is not torn down by restart_instance (the graceful
    FIN that reads as 'upstream closed before completing')."""
    logs = []
    wm = mock.Mock()
    daemon = _make_daemon(wm, ProxyPool(), logs)

    active_streams.inc("warp-4")
    daemon._heal_instance(_Inst(4))
    active_streams.dec("warp-4")

    wm.restart_instance.assert_not_called()
    wm.regenerate_instance.assert_not_called()
    assert any("deferred" in m for m in logs)


def test_heal_instance_idle_restarts_wireproxy():
    """Control: an idle broken instance still gets its restart attempt."""
    logs = []
    wm = mock.Mock()
    daemon = _make_daemon(wm, ProxyPool(), logs)

    with mock.patch("warp.health.time.sleep"), \
         mock.patch("warp.health._port_is_open", return_value=True), \
         mock.patch("warp.health._identity_config_ok", return_value=True):
        daemon._heal_instance(_Inst(4))

    wm.restart_instance.assert_called_once()
    wm.regenerate_instance.assert_not_called()    # restart succeeded


def test_ensure_min_healthy_busy_defers_without_consuming_need():
    """A deferred (busy) instance is skipped AND its slot remains unfulfilled
    -- `need` is not decremented, so the min-healthy target survives to the
    next cycle instead of being silently under-counted."""
    logs = []
    wm = mock.Mock()
    wm.instances = [_Inst(1)]
    daemon = _make_daemon(wm, ProxyPool(), logs)
    daemon.min_healthy = 1
    daemon._warmup = False
    # One unhealthy instance.
    results = [{"healthy": False}]

    active_streams.inc("warp-1")
    regenerated = daemon._ensure_min_healthy(results)
    active_streams.dec("warp-1")

    assert regenerated == 0
    wm.regenerate_instance.assert_not_called()
    assert any("deferred" in m for m in logs)


def test_ensure_min_healthy_idle_regenerates():
    """Control: an idle unhealthy instance is regenerated toward the target."""
    logs = []
    wm = mock.Mock()
    wm.instances = [_Inst(1)]
    daemon = _make_daemon(wm, ProxyPool(), logs)
    daemon.min_healthy = 1
    results = [{"healthy": False}]

    with mock.patch("warp.health.time.sleep"):
        regenerated = daemon._ensure_min_healthy(results)

    assert regenerated == 1
    wm.regenerate_instance.assert_called_once()
