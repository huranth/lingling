"""Lane health daemon -- this is what makes rate limits invisible.

A TLS tunnel can't be inspected by the relay, so a 429 can't be seen *in
flight*. Instead, every ``check_interval`` seconds each lane fires a real
``GET https://opencode.ai/zen/v1/models`` (with the ``opencode/1.0``
User-Agent, without which the free tier instantly 429s) through its own
SOCKS port, plus an IsTor probe that fingerprints the lane's real exit IP.

Verdicts:
  * probe raises / port dead      -> lane dead: restart, then regenerate
  * probe returns 429             -> destination burn: NEWNYM, then rebuild,
                                     then rotate country + regenerate
  * 2xx (or any non-429 status)   -> healthy; lane serves traffic

A burned or dead lane leaves rotation *before* your next request would have
used it, so the limit you never see is the one that was already healed.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable, Dict, Optional

from . import netutil
from .lanes import Lane, TorManager

UPSTREAM_HOST = "opencode.ai"
UPSTREAM_PROBE_PATH = "/zen/v1/models"
# The free tier instantly 429s requests without this UA -- verified live by
# the old gateway. A probe without it would call every healthy lane burned.
UPSTREAM_UA = os.environ.get("LINGLING_UPSTREAM_USER_AGENT", "opencode/1.0")

PROBE_TIMEOUT = 15.0
# Consecutive failed cycles before a dead lane escalates from restart to a
# wholesale regenerate.
_ESCALATE_AFTER = 2
# Consecutive 429 probes before the cheap heals (NEWNYM/rebuild, tried every
# cycle) give way to rotating the exit country + regenerating.
_BURN_ESCALATE_AFTER = 3
# Minimum wall-clock gap between regenerates of the same lane; Tor's full
# bootstrap is 30-90s and re-rolling faster just keeps the lane in boot.
_REGEN_COOLDOWN_S = 1200.0
# A sidelined lane gets one re-probe after this long; blocks do lift.
_SIDELINE_RECHECK_S = 3600.0
_FAST_FAIL_CYCLES = 4


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

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as exc:  # noqa: BLE001
                self.log("health: cycle flamed out: %s", exc)
            self._stop.wait(self.check_interval)

    def probe_lane(self, lane: Lane) -> str:
        """One probe round. Returns "healthy" | "burned" | "dead"."""
        if not netutil.port_is_open("127.0.0.1", lane.socks_port,
                                    timeout=netutil.PORT_CHECK_TIMEOUT):
            return "dead"
        try:
            code, _ = netutil.https_get_via_socks(
                lane.socks_port, UPSTREAM_HOST, UPSTREAM_PROBE_PATH,
                UPSTREAM_UA, timeout=PROBE_TIMEOUT)
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
        for lane in self.tor.lanes:
            if self._stop.is_set():
                return
            if lane.healing:
                continue
            if lane.sidelined:
                self._maybe_revive(lane)
                continue

            verdict = self.probe_lane(lane)
            if verdict == "healthy":
                was = lane.healthy
                lane.healthy = True
                lane.unhealthy_cycles = 0
                lane.burned_cycles = 0
                if was is not True:
                    self._emit_lane(lane, "up",
                                    f"lane {lane.index} {{{lane.exit_country}}} "
                                    f"is cooking -- exit {lane.exit_ip or '?'}")
                continue

            lane.healthy = False
            # Warmup grace: a port-open lane whose first probe fails is still
            # building its first circuit, not broken.
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

    # ------------------------------------------------------------------
    def _heal_burn(self, lane: Lane) -> None:
        """429 from the destination. Cheap circuit heals first; a persistent
        burn means the whole country persona is throttled, so rotate."""
        lane.healing = True
        try:
            if lane.burned_cycles < _BURN_ESCALATE_AFTER:
                self._emit_lane(lane, "burn",
                                f"lane {lane.index} hit a hidden limit -- "
                                f"fresh circuits cooking")
                if self.tor.renew(lane):
                    return
                self.tor.rebuild_circuits(lane)
                return
            cooldown_left = (_REGEN_COOLDOWN_S
                             - (time.time() - lane.last_regenerate_at))
            if lane.last_regenerate_at and cooldown_left > 0:
                return
            new_cc = self.tor.rotate_exit_country(lane)
            lane.last_regenerate_at = time.time()
            lane.burned_cycles = 0
            if new_cc:
                self._emit_lane(lane, "rotate",
                                f"lane {lane.index} burned through "
                                f"-- re-cooking on {{{new_cc}}}")
            else:
                self._emit_lane(lane, "rotate",
                                f"lane {lane.index} burned through -- "
                                f"re-cooking from scratch")
            self.tor.regenerate_lane(lane)
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
                self.tor.restart_lane(lane)
                return
            cooldown_left = (_REGEN_COOLDOWN_S
                             - (time.time() - lane.last_regenerate_at))
            if lane.last_regenerate_at and cooldown_left > 0:
                return
            lane.last_regenerate_at = time.time()
            self._emit_lane(lane, "heal",
                            f"lane {lane.index} stayed down -- re-cooking "
                            f"from scratch")
            self.tor.regenerate_lane(lane)
        finally:
            lane.healing = False

    def _maybe_revive(self, lane: Lane) -> None:
        if time.time() - lane.last_sideline_at < _SIDELINE_RECHECK_S:
            return
        if self.probe_lane(lane) == "healthy":
            lane.sidelined = False
            lane.unhealthy_cycles = 0
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
