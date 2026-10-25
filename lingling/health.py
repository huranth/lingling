"""Lane health daemon -- two facts, and nothing else."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import bounds, exits, netutil
from .bench import EXIT_HOLD_S, EXIT_SOFT_CAP, FREE_LIMIT, RATE_LIMIT
from .lanes import Lane, TorManager

UPSTREAM_HOST = "opencode.ai"
UPSTREAM_PROBE_PATH = "/zen/v1/models"
UPSTREAM_MODEL_PATH = "/zen/v1/responses"
UPSTREAM_CHAT_PATH = "/zen/v1/chat/completions"
PROBE_MODEL = os.environ.get("LINGLING_PROBE_MODEL",
                             "mimo-v2.6-flash-free")
PROBE_PATH = os.environ.get("LINGLING_PROBE_PATH", UPSTREAM_CHAT_PATH)


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
UPSTREAM_UA = os.environ.get("LINGLING_UPSTREAM_USER_AGENT", "opencode/1.0")

PROBE_TIMEOUT = 15.0

SWEEP_S = bounds.number("LINGLING_SWEEP_S", 20, 0.0, bounds.WINDOW_S)

SWEEP_MOVES = bounds.count("LINGLING_SWEEP_MOVES", 3, 1, 64)

PROBE_VERDICTS = (200, 403, 429)


def _lane_event(emit: Callable[[Dict], None], lane: Lane, kind: str,
                message: str) -> None:
    """One lane event, shaped the one way the pane reads."""
    emit({
        "type": "lane", "kind": kind, "t": time.time(),
        "lane": lane.index, "cc": lane.exit_country,
        "ip": lane.exit_ip, "msg": message,
    })


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
        self._emit = event or (lambda e: None)
        self.log = log or (lambda *a, **k: None)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
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
        """One request through the lane: its HTTP status, or 0 if nothing came back."""
        lane.probe_note = ""
        lane.probe_reason = ""
        if not netutil.port_is_open("127.0.0.1", lane.socks_port,
                                    timeout=netutil.PORT_CHECK_TIMEOUT):
            lane.probe_note = "port closed"
            return 0
        code = 0
        body = b""
        try:
            code, body = netutil.https_via_socks(
                lane.socks_port, UPSTREAM_HOST, "POST", PROBE_PATH,
                UPSTREAM_UA, body=_scan_body(PROBE_MODEL, PROBE_PATH),
                timeout=probe_timeout,
                cred=netutil.lane_cred(lane.index))
        except Exception as exc:  # noqa: BLE001
            lane.probe_note = type(exc).__name__
            return 0
        if code == 429:
            if netutil.UPSTREAM_UNAVAILABLE in body:
                lane.probe_note = "opencode down"
                return 0
            lane.probe_reason = (FREE_LIMIT if netutil.QUOTA_SPENT in body
                                 else RATE_LIMIT)
        if code == 410:
            lane.probe_note = ("probe model retired"
                               if b"ModelDeprecated" in body else "gone")
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
            if not lane.wanted:
                continue
            if lane.healing:
                continue
            if (lane.process is None or lane.process.poll() is not None
                    or not lane.healthy):
                self._revive(lane)
                continue
            if not netutil.port_is_open("127.0.0.1", lane.socks_port,
                                        timeout=netutil.PORT_CHECK_TIMEOUT):
                lane.probe_code = -1
                self._revive(lane)
                continue
            if lane.asked:
                continue
            code = self.reachable(lane)
            if code == 429:
                self.tor.note_result(lane.exit_country, code)
                self.refuse(lane, code)
                continue
            if code in PROBE_VERDICTS:
                lane.asked = True
                lane.probe_code = code
                self._emit_lane(
                    lane, "up",
                    f"lane {lane.index} {{{lane.exit_country}}} is up "
                    f"-- exit {lane.exit_ip or '?'}")
                continue
            if lane.probe_code != code:
                lane.probe_code = code
                note = getattr(lane, "probe_note", "")
                self._emit_lane(
                    lane, "probe",
                    f"lane {lane.index} probe got {code or 'nothing'}"
                    + (f" ({note})" if note else "")
                    + " -- not a verdict, re-asking next sweep")

    def refuse(self, lane: Lane, status: int = 429) -> None:
        """A 429 the probe saw through this lane: write it off, then move it.

        A call that was refused writes its exit off itself, because only there
        is the far end's own wait and section in hand; this is for the paths
        that asked the lane directly -- the boot check, the demo and the health
        sweep -- where ``reachable`` has already read the reason off the reply.
        """
        if status != 429:
            return
        self.tor.note_limited(lane, reason=lane.probe_reason or RATE_LIMIT)
        self.on_refused(lane, status)

    def on_refused(self, lane: Lane, status: int = 429,
                   fingerprint: str = "") -> None:
        """A real 429 from the far end: move this lane off the exit it hit.

        ``fingerprint`` names the exit the refusal came from, which is not
        always the one the lane rides by now: a copy already in flight when its
        lane moved reports on the exit it dialled, and the exit that moved the
        lane came in first.  Moving again on that stale verdict would cost a
        country rotation and a recook for nothing, so a lane that has already
        left the refused exit is left where it is.
        """
        if status != 429:
            return
        exit_fp = fingerprint or lane.exit_fingerprint
        if exit_fp and exit_fp != lane.exit_fingerprint:
            return
        if not self.tor.claim_move(lane):
            return
        moved = self.tor.rotate_exit_country(lane)
        if self.tor.restart_lane(lane):
            if moved:
                self._emit_lane(
                    lane, "limited",
                    f"lane {lane.index} was refused ({status}) -- moved to "
                    f"{{{moved}}}")
            else:
                self._emit_lane(
                    lane, "limited",
                    f"lane {lane.index} was refused ({status}) and nothing has "
                    f"room left -- keeping its own exit, retrying")

    def _revive(self, lane: Lane) -> None:
        """Bring a down lane back, and say so either way."""
        if self.tor.restart_lane(lane):
            lane.down_reported = False
            self._emit_lane(
                lane, "up",
                f"lane {lane.index} was down -- brought it back")
            return
        if not getattr(lane, "down_reported", False):
            lane.down_reported = True
            self._emit_lane(
                lane, "down",
                f"lane {lane.index} is down -- could not bring it back")

    def _emit_lane(self, lane: Lane, kind: str, message: str) -> None:
        _lane_event(self._emit, lane, kind, message)


class ExitSweep:
    """Rotates idle lanes onto fresh exits, so our own calls spread out."""

    def __init__(
        self,
        tor: TorManager,
        event: Optional[Callable[[Dict], None]] = None,
        log: Optional[Callable[..., None]] = None,
        interval: float = SWEEP_S,
    ) -> None:
        self.tor = tor
        self.interval = interval
        self._emit = event or (lambda e: None)
        self.log = log or (lambda *a, **k: None)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="exit-sweep", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001
                self.log("sweep: cycle flamed out: %s", exc)
            self._stop.wait(self.interval)

    def step(self) -> None:
        """Move idle lanes onto the freshest exits their countries can give.

        A lane that is sitting out a spent exit goes first: that is the pool
        coming back, and it costs nothing to ask a lane that is carrying
        nothing to re-pin itself.  A lane whose exit we have already leaned on
        hard comes next, and that one is the whole point of the ledger: leave
        an exit while it still has something in hand rather than sitting on it
        until it walls, which is how one exit ends up carrying a whole session
        and then taking every lane down with it.  A lane that has simply been
        riding one address the longest comes third, because nobody else's spend
        shows up in any ledger of ours, so time on an exit is the only other
        warning there is.  A move that fails is not a reason to stop, so the
        next idle lane gets its turn -- otherwise one lane whose tor will not
        co-operate would hold every other lane's rescue behind it.

        A few lanes move per cycle rather than one: only idle lanes are ever
        asked, so a sweep that moves one lane per cycle needs ten cycles to
        turn a ten-lane fleet over, and a lane waiting its turn is a lane still
        sitting on an address other people are spending.
        """
        if self.interval <= 0:
            return
        now = time.time()
        idle = self.tor.idle_lanes()
        riding = {id(l): self._riding_for(l) for l in idle}
        benched = [l for l in idle if l.limited_until > now]
        worn = [l for l in idle
                if l.limited_until <= now
                and self._carried(l) >= EXIT_SOFT_CAP]
        held = sorted((l for l in idle
                       if l.limited_until <= now
                       and riding[id(l)] >= EXIT_HOLD_S),
                      key=lambda l: riding[id(l)], reverse=True)
        queue: List[Tuple[Lane, str]] = []
        seen = set()
        for lane, reason in ([(l, "sitting out") for l in benched]
                             + [(l, "worn") for l in worn]
                             + [(l, "held") for l in held]
                             + [(l, "") for l in idle if l.limited_until <= now]):
            if id(lane) in seen:
                continue
            seen.add(id(lane))
            queue.append((lane, reason))
        moved = 0
        for lane, reason in queue:
            if self._reaim(lane, reason):
                moved += 1
                if moved >= SWEEP_MOVES:
                    return

    def _riding_for(self, lane: Lane) -> float:
        """How long this lane has ridden the exit it names, -1 when unknown.

        The clock is the pin, not the boot.  A lane moved onto another relay
        without a restart rides the new one the moment tor answers, and a
        window measured from the boot would call that exit old on the way in,
        which is a lane that never stops moving.
        """
        if EXIT_HOLD_S <= 0:
            return -1.0
        since = getattr(lane, "exit_since_ts", 0.0)
        if not since:
            return -1.0
        return max(0.0, time.time() - since)

    def _carried(self, where) -> int:
        """How much of its day one exit has carried, 0 when nothing counts.

        A lane or a relay may be handed in, and the ledger is keyed the way
        the far end meters it.  A tor that keeps no ledger -- a stand-in in a
        test, the paths that only ever ask a lane directly -- reports nothing
        worn, which is the old behaviour exactly.
        """
        spent_of = getattr(self.tor, "spent_of", None)
        if spent_of is None:
            return 0
        key = exits.exit_key(where)
        if not key:
            return 0
        try:
            return int(spent_of(key) or 0)
        except (TypeError, ValueError):
            return 0

    def _reaim(self, lane: Lane, reason: str = "") -> bool:
        """Move one lane to another exit in its own country, if it has one.

        A lane whose exit is already hard used stays where it is when the
        freshest exit its country can offer is no fresher than the one it
        rides: that move would cost a circuit and buy nothing.  A lane that is
        only out of time is a different case -- the address it holds is the one
        other people have had longest to spend -- so it takes any other exit
        its country has, fresher or not.
        """
        candidates = self.tor.sweep_candidates(lane.exit_country)
        if not candidates:
            return False
        target = candidates[0]
        if target.fingerprint == lane.exit_fingerprint:
            return False
        carried = self._carried(lane)
        ridden = self._riding_for(lane)
        if (carried >= EXIT_SOFT_CAP and reason != "sitting out"
                and self._carried(target) >= carried):
            return False
        lane.healing = True
        try:
            moved = self.tor.move_exit(lane, target)
        finally:
            lane.healing = False
        if not moved:
            return False
        if reason == "sitting out":
            said = (f"lane {lane.index} {{{lane.exit_country}}} was sitting out "
                    f"a spent exit -- back on {target.ip}")
        elif reason == "worn":
            said = (f"lane {lane.index} {{{lane.exit_country}}} had carried "
                    f"{carried} requests on its exit -- moved on to "
                    f"{target.ip}")
        elif reason == "held":
            said = (f"lane {lane.index} {{{lane.exit_country}}} had ridden its "
                    f"exit for {ridden / 60:.0f} min -- moved on to "
                    f"{target.ip}")
        else:
            said = (f"lane {lane.index} {{{lane.exit_country}}} spread to "
                    f"exit {target.ip}")
        self._say(lane, "spread", said)
        return True

    def _say(self, lane: Lane, kind: str, message: str) -> None:
        """The pane hears every exit we ask."""
        _lane_event(self._emit, lane, kind, message)
