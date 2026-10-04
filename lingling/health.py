"""Lane health daemon -- two facts, and nothing else."""

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
#: responses path
UPSTREAM_MODEL_PATH = "/zen/v1/responses"
#: chat path
UPSTREAM_CHAT_PATH = "/zen/v1/chat/completions"
#: probe model
PROBE_MODEL = os.environ.get("LINGLING_PROBE_MODEL",
                             "muse-spark-1.3-contributor-free")
#: probe path
PROBE_PATH = os.environ.get("LINGLING_PROBE_PATH", UPSTREAM_MODEL_PATH)


def _scan_body(model: str, path: str = UPSTREAM_MODEL_PATH) -> bytes:
    """The probe body, shaped for the path it is going to."""
    name = model.encode("utf-8")
    if "chat/completions" in path:
        return (b'{"model":"' + name + b'","stream":false,"max_tokens":1,'
                b'"messages":[{"role":"user","content":"x"}]}')
    return (b'{"model":"' + name + b'","stream":false,'
            b'"max_output_tokens":1,'
            b'"input":[{"role":"user","content":'
            b'[{"type":"input_text","text":"x"}]}]}')
# required UA
UPSTREAM_UA = os.environ.get("LINGLING_UPSTREAM_USER_AGENT", "opencode/1.0")

PROBE_TIMEOUT = 15.0

#: probe verdicts
PROBE_VERDICTS = (200, 403, 429)


class HealthDaemon:
    def __init__(
        self,
        tor: TorManager,
        check_interval: float = 15.0,
        event: Optional[Callable[[Dict], None]] = None,
        log: Optional[Callable[..., None]] = None,
    ) -> None:
        self.tor = tor
        self.check_interval = check_interval
        # event sink
        self._emit = event or (lambda e: None)
        self.log = log or (lambda *a, **k: None)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # refusal hook
        tor.limit_hook = self.on_refused

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
            except Exception as exc:  # noqa: BLE001
                self.log("health: cycle flamed out: %s", exc)
            self._stop.wait(self.check_interval)

    def reachable(self, lane: Lane, probe_timeout: float = PROBE_TIMEOUT) -> int:
        """One hand-rolled request through the lane: its HTTP status, or 0 if nothing came back."""
        if not netutil.port_is_open("127.0.0.1", lane.socks_port,
                                    timeout=netutil.PORT_CHECK_TIMEOUT):
            return 0
        code = 0
        try:
            code, _ = netutil.https_via_socks(
                lane.socks_port, UPSTREAM_HOST, "POST", PROBE_PATH,
                UPSTREAM_UA, body=_scan_body(PROBE_MODEL, PROBE_PATH),
                timeout=probe_timeout,
                cred=netutil.lane_cred(lane.index))
        except Exception:  # noqa: BLE001
            return 0
        try:
            c2, body = netutil.https_get_via_socks(
                lane.socks_port, "check.torproject.org", "/api/ip",
                UPSTREAM_UA, timeout=probe_timeout)
            if c2 == 200:
                obj = json.loads(body.decode("utf-8", "replace"))
                if obj.get("IsTor") and obj.get("IP"):
                    lane.exit_ip = str(obj["IP"])
        except Exception:  # noqa: BLE001
            pass
        return code

    def check_once(self) -> None:
        """Bring up any lane that is down, and check a new lane's exit."""
        for lane in self.tor.lanes:
            # not ours
            if not lane.wanted:
                continue
            # mid-launch
            if lane.healing:
                continue
            # down
            if (lane.process is None or lane.process.poll() is not None
                    or not lane.healthy):
                if self.tor.restart_lane(lane):
                    self._emit_lane(
                        lane, "up",
                        f"lane {lane.index} was down -- brought it back")
                continue
            if lane.asked:
                continue
            code = self.reachable(lane)
            if code == 429:
                #: burnt
                self.tor.note_result(lane.exit_country, code)
                self.on_refused(lane, code)
                continue
            if code in PROBE_VERDICTS:
                lane.asked = True
                lane.probe_code = code
                self._emit_lane(
                    lane, "up",
                    f"lane {lane.index} {{{lane.exit_country}}} is cooking "
                    f"({code}) -- exit {lane.exit_ip or '?'}")
                continue
            # no verdict
            if lane.probe_code != code:
                lane.probe_code = code
                self._emit_lane(
                    lane, "probe",
                    f"lane {lane.index} probe got {code or 'nothing'} -- that "
                    f"is not a verdict about the exit, so the lane is left "
                    f"unasked and asked again next sweep")

    def on_refused(self, lane: Lane, status: int = 429) -> None:
        """A real 429 from the far end: move this lane to a different country."""
        if status != 429:
            # no signal
            return
        if lane.limited_until <= time.time():
            self.tor.note_limited(lane)
        moved = self.tor.rotate_exit_country(lane)
        if self.tor.restart_lane(lane):
            self._emit_lane(
                lane, "limited",
                f"lane {lane.index} was refused ({status}) -- moved to "
                f"{{{moved or lane.exit_country}}}")

    def _emit_lane(self, lane: Lane, kind: str, message: str) -> None:
        self._emit({
            "type": "lane", "kind": kind, "t": time.time(),
            "lane": lane.index, "cc": lane.exit_country,
            "ip": lane.exit_ip, "msg": message,
        })
