"""Lane health daemon -- this is what makes rate limits invisible.

A TLS tunnel can't be inspected by the relay, so each lane periodically
probes the real upstream through its own SOCKS port and is healed (restarted
or regenerated from scratch) before your next request would have used it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from typing import Callable, Dict, Optional

from . import netutil
from .lanes import Lane, TorManager

UPSTREAM_HOST = "opencode.ai"
UPSTREAM_PROBE_PATH = "/zen/v1/models"
# The free tier instantly 429s requests without this UA.
UPSTREAM_UA = os.environ.get("LINGLING_UPSTREAM_USER_AGENT", "opencode/1.0")

# The startup race: identical tiny prompt for every lane, so timings compare.
_BENCH_PROMPT = "say ok"

PROBE_TIMEOUT = 15.0
# Dead cycles before restart gives way to regenerate.
_ESCALATE_AFTER = 2
# Burns before the exit country rotates.
_BURN_ESCALATE_AFTER = 3
# Re-probe sidelined lanes after this long; blocks lift.
_SIDELINE_RECHECK_S = 60.0
# Dead cycles before a lane is sidelined.
_FAST_FAIL_CYCLES = 4
# 429s across all lanes in the window that trip rest mode.
_REST_TRIGGER = 4
_REST_WINDOW_S = 300.0
# Quiet minutes that drain the upstream window (LINGLING_REST_S).
_REST_S = float(os.environ.get("LINGLING_REST_S", "120"))


class HealthDaemon:
    def __init__(
        self,
        tor: TorManager,
        check_interval: float = 45.0,
        event: Optional[Callable[[Dict], None]] = None,
        log: Optional[Callable[..., None]] = None,
    ) -> None:
        self.tor = tor
        self.check_interval = check_interval
        # ``event`` receives proof-log dicts ({"type": "lane", ...}).
        self._emit = event or (lambda e: None)
        self.log = log or (lambda *a, **k: None)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._warmup = True
        self._benchmarked = False
        self._burn_times: deque = deque()
        self._rest_until = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="lane-health", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
                if not self._benchmarked and not self._warmup:
                    self._benchmarked = True
                    self._benchmark_lanes()
            except Exception as exc:  # noqa: BLE001
                self.log("health: cycle flamed out: %s", exc)
            self._stop.wait(self.check_interval)

    def _benchmark_lanes(self) -> None:
        """One identical real prompt through every healthy lane, once per
        launch. Ranks lanes by true end-to-end speed so the sticky picker
        starts on facts, not on metadata-probe latency."""
        healthy = [l for l in self.tor.lanes if l.healthy]
        if len(healthy) < 2:
            self._benchmarked = False  # try again next cycle
            return
        # Any model works as long as every lane gets the SAME one; take the
        # first id from the models list the probe already trusts.
        try:
            code, body = netutil.https_get_via_socks(
                healthy[0].socks_port, UPSTREAM_HOST, UPSTREAM_PROBE_PATH,
                UPSTREAM_UA, timeout=PROBE_TIMEOUT)
            models = json.loads(body.decode("utf-8", "replace")).get("data", [])
            model = str(models[0]["id"])
        except Exception:  # noqa: BLE001
            self._benchmarked = False
            return
        bench_body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": _BENCH_PROMPT}],
            "max_tokens": 5, "stream": False,
        }).encode()
        self._emit({"type": "lane", "kind": "bench", "t": time.time(),
                    "msg": "racing the lanes with one real prompt each..."})
        for lane in healthy:
            if self._stop.is_set():
                return
            try:
                t0 = time.monotonic()
                code, _ = netutil.https_via_socks(
                    lane.socks_port, UPSTREAM_HOST,
                    "POST", "/zen/v1/chat/completions", UPSTREAM_UA,
                    body=bench_body, timeout=45.0, max_body=65536)
                ms = (time.monotonic() - t0) * 1000.0
                if code == 200:
                    lane.probe_ms = ms
                    self._emit({
                        "type": "lane", "kind": "bench", "t": time.time(),
                        "lane": lane.index, "cc": lane.exit_country,
                        "msg": f"lane {lane.index} {{{lane.exit_country}}} "
                               f"answered in {ms / 1000:.1f}s"})
                elif code == 429:
                    lane.healthy = False
                    lane.burned_cycles += 1
            except Exception:  # noqa: BLE001
                pass

    def probe_lane(self, lane: Lane) -> str:
        """One probe round. Returns "healthy" | "burned" | "dead"."""
        if not netutil.port_is_open("127.0.0.1", lane.socks_port,
                                    timeout=netutil.PORT_CHECK_TIMEOUT):
            return "dead"
        try:
            t0 = time.monotonic()
            code, _ = netutil.https_get_via_socks(
                lane.socks_port, UPSTREAM_HOST, UPSTREAM_PROBE_PATH,
                UPSTREAM_UA, timeout=PROBE_TIMEOUT)
            # Any completed round trip is a valid network-latency sample,
            # even a 429 -- burn handling is separate from speed ranking.
            ms = (time.monotonic() - t0) * 1000.0
            lane.probe_ms = ms if lane.probe_ms <= 0 else (
                lane.probe_ms * 0.7 + ms * 0.3)
            if code == 429:
                return "burned"
            if code == 0:
                return "dead"
        except Exception:  # noqa: BLE001
            return "dead"
        # Lane is carrying traffic; fingerprint its exit IP for the proof pane.
        try:
            code, body = netutil.https_get_via_socks(
                lane.socks_port, "check.torproject.org", "/api/ip",
                UPSTREAM_UA, timeout=PROBE_TIMEOUT)
            if code == 200:
                obj = json.loads(body.decode("utf-8", "replace"))
                if obj.get("IsTor") and obj.get("IP"):
                    lane.exit_ip = str(obj["IP"])
        except Exception:  # noqa: BLE001
            pass
        return "healthy"

    def check_once(self) -> None:
        if time.time() < self._rest_until:
            return  # rest mode: total silence drains the shared window
        if self._rest_until:
            self._rest_until = 0.0  # rest over: one canary probe first
            if not self._canary():
                return
        for lane in self.tor.lanes:
            if self._stop.is_set():
                return
            if lane.healing:
                continue
            if lane.sidelined:
                self._maybe_revive(lane)
                continue

            verdict = self.probe_lane(lane)
            # Real-traffic failures outrank the metadata probe: a probe
            # can succeed on an exit that stalls real streams.
            if verdict == "healthy" and lane.healthy is False:
                if lane.burned_cycles > 0:
                    verdict = "burned"
                elif lane.stall_cycles > 0:
                    verdict = "dead"
            if verdict == "healthy":
                was = lane.healthy
                lane.healthy = True
                lane.unhealthy_cycles = 0
                lane.burned_cycles = 0
                # A metadata probe proves nothing about real streams.
                if was is not True:
                    self._emit_lane(lane, "up",
                                    f"lane {lane.index} {{{lane.exit_country}}} "
                                    f"is cooking -- exit {lane.exit_ip or '?'}")
                continue

            lane.healthy = False
            # Warmup grace: first failed probe on a live port is just a slow first circuit.
            if self._warmup and netutil.port_is_open(
                    "127.0.0.1", lane.socks_port, timeout=netutil.PORT_CHECK_TIMEOUT):
                continue
            if verdict == "burned":
                lane.unhealthy_cycles = 0
                lane.burned_cycles += 1
                self._heal_burn(lane)
            else:
                lane.burned_cycles = 0
                lane.unhealthy_cycles += 1
                self._heal_dead(lane)
        self._warmup = False

    def _note_burn(self, lane: Lane) -> None:
        """Count a 429; trip rest mode when the wall is identity-wide."""
        now = time.time()
        self._burn_times.append(now)
        while self._burn_times and self._burn_times[0] < now - _REST_WINDOW_S:
            self._burn_times.popleft()
        if len(self._burn_times) < _REST_TRIGGER:
            return
        self._burn_times.clear()
        self._rest_until = now + _REST_S
        for l in self.tor.lanes:
            l.burned_cycles = 0
        self._emit_lane(
            lane, "rest",
            f"429s on every lane -- this limit follows your identity, not "
            f"the exits; resting {_REST_S:.0f}s so the window drains")

    def _canary(self) -> bool:
        """One probe after rest; False re-enters rest."""
        lane = next((l for l in self.tor.lanes
                     if not l.sidelined and not l.healing), None)
        if lane is None:
            return False
        if self.probe_lane(lane) == "burned":
            self._rest_until = time.time() + _REST_S
            self._emit_lane(lane, "rest",
                            f"still limited -- resting another {_REST_S:.0f}s")
            return False
        return True

    def _heal_burn(self, lane: Lane) -> None:
        """429 from upstream: rest on storms, else re-cook on repeats."""
        self._note_burn(lane)
        if time.time() < self._rest_until:
            lane.healthy = False
            return
        lane.healing = True
        try:
            self._emit_lane(
                lane, "burn",
                f"lane {lane.index} hit a hidden limit -- your traffic moved "
                f"to a fresh lane; re-cooking this one from scratch")
            if lane.burned_cycles >= _BURN_ESCALATE_AFTER:
                old_cc = lane.exit_country
                new_cc = self.tor.rotate_exit_country(lane)
                if new_cc and new_cc != old_cc:
                    self._emit_lane(
                        lane, "rotate",
                        f"lane {lane.index} keeps burning -- re-cooking on a "
                        f"new country {{{new_cc}}}")
                elif new_cc:
                    self._emit_lane(
                        lane, "rotate",
                        f"lane {lane.index} keeps burning -- re-cooking on a "
                        f"fresh {{{new_cc}}} exit")
            lane.last_regenerate_at = time.time()
            if self.tor.regenerate_lane(lane):
                lane.clear_stalls()
        finally:
            lane.healing = False

    def _heal_dead(self, lane: Lane) -> None:
        if lane.unhealthy_cycles >= _FAST_FAIL_CYCLES:
            lane.sidelined = True
            lane.last_sideline_at = time.time()
            self._emit_lane(lane, "sidelined",
                            f"lane {lane.index} sat out (blocked exit) -- "
                            f"will retry later")
            return
        lane.healing = True
        try:
            if lane.unhealthy_cycles <= _ESCALATE_AFTER:
                self._emit_lane(lane, "heal",
                                f"lane {lane.index} dropped -- poking it")
                if self.tor.restart_lane(lane):
                    # Fresh process, fresh circuits: the old stall record
                    # belongs to the previous exit.
                    lane.clear_stalls()
                else:
                    self._emit_lane(
                        lane, "fail",
                        f"lane {lane.index} would not restart -- will "
                        f"re-cook it from scratch if it stays down")
                return
            lane.last_regenerate_at = time.time()
            self._emit_lane(lane, "heal",
                            f"lane {lane.index} stayed down -- re-cooking "
                            f"from scratch")
            if lane.exit_country != "*":
                self.tor.rotate_exit_country(lane)
            if self.tor.regenerate_lane(lane):
                lane.clear_stalls()
            else:
                self._emit_lane(lane, "fail",
                                f"lane {lane.index} refused to re-cook")
        finally:
            lane.healing = False

    def _maybe_revive(self, lane: Lane) -> None:
        if time.time() - lane.last_sideline_at < _SIDELINE_RECHECK_S:
            return
        if self.probe_lane(lane) == "healthy":
            lane.sidelined = False
            lane.unhealthy_cycles = 0
            lane.clear_stalls()
            lane.healthy = True
            self._emit_lane(lane, "up",
                            f"lane {lane.index} revived -- back in the kitchen")
        else:
            lane.last_sideline_at = time.time()

    def _emit_lane(self, lane: Lane, kind: str, message: str) -> None:
        self._emit({
            "type": "lane", "kind": kind, "t": time.time(),
            "lane": lane.index, "cc": lane.exit_country,
            "ip": lane.exit_ip, "msg": message,
        })
