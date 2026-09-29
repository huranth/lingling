"""Lane health daemon -- two facts, and nothing else.

1. A lane whose tor process has exited is restarted. That is a process fact,
   not a judgement about the exit.
2. A lane that answered a real 429 gets a fresh exit. That is the far end's own
   words about one exit IP, so it is the only thing allowed to move a lane.

A 403 is NOT a signal. The free tier gates on the client: a hand-rolled request
gets `403 FreeTierError "can only be used from within OpenCode"` whatever
headers it sends, and lingling's own `--demo` and probes are hand-rolled. Real
opencode never sees it -- measured, driving the real binary: 8 runs, only 429,
no 403 at all. Re-pinning a lane cannot fix a client gate anyway.

Everything this daemon used to do beyond that -- probe verdicts, stall strikes,
sidelining, revival ladders, benchmarking -- was inference about exits we had
never measured, and it retired healthy ones. Re-tested with a raw request:
five exits that had been accused answered 200, and not one answered 429 or 403.

A 429 is evidence against one EXIT, never against the pool. opencode rate
limits the exit IP, and each 429 names its own reset via `retry-after`.
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
    """The probe body, shaped for the path it is going to.

    **The model and the PATH are a pair, and they are not interchangeable.**
    Measured on a live lane with `tools/probe/model_matrix.py`:

        POST /zen/v1/responses         muse-spark -> 403   mimo -> 500
        POST /zen/v1/chat/completions  muse-spark -> 500   mimo -> 403
        either path, a bogus name                 -> 401

    Crossing them answers **500 Internal server error**, and a 500 is not a
    verdict about the lane -- so a probe sent to the wrong path for its model
    stops being a probe at all. The body shape has to follow the path too: the
    responses API takes `input` with typed content parts, the chat API takes
    `messages`. Sending one shape to the other path is a second way to earn
    that 500. `PROBE_PATH` and `PROBE_MODEL` are therefore overridden together
    or not at all.

    The name used to be welded into the bytes, which made this the ONE place
    in the package where "which model" changes behaviour. Two reasons it is
    now a parameter:

      * the model name is validated BEFORE the limit check and before the
        client gate, so a name the far end has retired turns every lane's
        probe into a 401 -- and `check_once` reads any truthy code as healthy,
        so the 429 branch stops firing and burnt exits are never caught. That
        has already happened once here, with the placeholder `"x"`.
      * the quota may be per (exit, model). If it is, a probe riding one model
        says nothing about another, and the 403 it reports is not evidence
        about the model actually in use.

    The default pair is the one measured good. Pointing it at whatever is
    being used is the honest setting -- but it takes BOTH variables, because
    half a pair is a 500."""
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

    def reachable(self, lane: Lane) -> int:
        """One hand-rolled request through the lane: its HTTP status, or 0 if
        nothing came back.

        It asks the MODEL path, because that is the only one whose status
        reflects the free-tier quota -- `/zen/v1/models` answers 200 even on a
        spent exit, which is why the old probe could not see the limit.

        The limit check runs before the client gate, so the answers are:

            429  this exit's quota is spent
            403  the exit is fine (the gate refused the hand-rolled request)
            0    nothing came back

        And because the gate always refuses a hand-rolled request, the probe
        never reaches a model -- it spends no quota. It also learns the exit
        IP, which is what the pane shows.

        Being exact about what is measured: the 403 is measured on a live lane,
        every time. **The 429 is inferred, not measured** -- a probe spends no
        quota, so it can never make an exit 429 by itself. What is tested is
        that a 429 arriving from the far end survives to `on_refused` and moves
        the lane (`verify_probe_branch.py`), and that a 403 does not. The
        remaining gap -- whether the real far end would answer a hand-rolled
        request 429 rather than 403 on a genuinely spent exit -- is not
        directly observable without spending the window.

        The model name in `PROBE_MODEL` has to be a REAL one. Measured on a live
        lane: with the placeholder `"x"` this returned

            401 ModelError "Model x is not supported"

        because the model name is validated before the limit check and before
        the gate. So every lane came back 401, `check_once` read a truthy code
        as healthy, and the 429 branch below could never fire -- which is why
        burnt exits were never caught and their first real request paid the
        429. With the real name it returns 403 (exit fine) or 429 (spent), as
        this docstring always claimed. Pinned by
        `tools/verify/verify_probe_status.py`."""
        if not netutil.port_is_open("127.0.0.1", lane.socks_port,
                                    timeout=netutil.PORT_CHECK_TIMEOUT):
            return 0
        code = 0
        try:
            code, _ = netutil.https_via_socks(
                lane.socks_port, UPSTREAM_HOST, "POST", PROBE_PATH,
                UPSTREAM_UA, body=_scan_body(PROBE_MODEL, PROBE_PATH),
                timeout=PROBE_TIMEOUT)
        except Exception:  # noqa: BLE001
            return 0
        try:
            c2, body = netutil.https_get_via_socks(
                lane.socks_port, "check.torproject.org", "/api/ip",
                UPSTREAM_UA, timeout=PROBE_TIMEOUT)
            if c2 == 200:
                obj = json.loads(body.decode("utf-8", "replace"))
                if obj.get("IsTor") and obj.get("IP"):
                    lane.exit_ip = str(obj["IP"])
        except Exception:  # noqa: BLE001
            pass
        return code

    def check_once(self) -> None:
        """Bring up any lane that is down, and check a new lane's exit.

        A lane with no exit IP yet has not been asked, so we ask -- which is
        also how the pane learns which exit a lane rides. And the answer is
        worth reading: a lane that answers 429 to that probe **arrived burnt**,
        so it moves country and the next sweep asks again. That is what stops
        the very first request failing for a reason that was never ours.

        A lane that is DOWN in any sense is brought back and reported, not
        skipped. This used to skip a lane with no process outright, and skip an
        unhealthy lane before it could be re-probed -- so both states were
        terminal and silent, and a lane whose launch failed was dead for the
        whole session while `start.lanes` still advertised the full pool.
        Found as a soak that put **all 46 calls on lane 6**: five of six lanes
        gone, with no lane event in the log at all. (The soak prints its own
        healthy count to stdout; the pane and the log said nothing, which is
        what the owner would have seen.)

        `wanted` separates "its launch failed" from "the CLI has not started it
        yet". Reviving the latter would race the CLI's own staggered boot, so
        a lane nobody asked for is left alone.

        **Only 200, 403 and 429 are verdicts.** Everything else -- 401, 500, or
        nothing at all -- means the PROBE failed, not that the lane is fine.
        This used to be `if code:`, so ANY truthy status marked the lane up and
        set `asked`, and it was never probed again. Moving the probe's model
        away from a placeholder once made every probe a 401, and because 401 is
        truthy the 429 branch stopped firing and burnt exits were never caught.
        The model NAME was fixed that day; the LOGIC was not, which left the
        bug sitting there waiting for the next non-403.

        200 belongs in the set even though the gate is documented never to
        allow one: if it ever does, the exit answered a real request, which is
        the strongest possible verdict. Leaving it out would have a working
        lane re-probed forever.

        And the non-verdict case is not hypothetical. Measured on a live lane
        with `tools/probe/model_matrix.py`, the model and the PATH are paired
        and crossing them answers 500:

            POST /zen/v1/responses         muse-spark -> 403    mimo -> 500
            POST /zen/v1/chat/completions  muse-spark -> 500    mimo -> 403
            either path, a bogus name                 -> 401

        So one wrong pairing turns every probe into a 500 -- truthy, and under
        the old rule indistinguishable from a healthy lane. A non-verdict now
        leaves `asked` False, so the lane is asked again next sweep, and the
        code is reported once per CHANGE rather than every 15s."""
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
        """A real 429 from the far end: move this lane to a different country.

        The quota is per exit IP, and limited IPs cluster by operator --
        measured, three of four IPs in one range were already limited while a
        neighbouring range was clean. So re-pinning inside the same country can
        land straight back in a burnt range. `rotate_exit_country` walks to a
        country that still has an exit to give, and skips countries whose
        relays are all in `_limited` (which carries the far end's own reset).

        A 403 is NOT a signal and returns immediately. The free tier gates on
        the client, so a hand-rolled request is refused whatever exit it rides,
        and re-pinning a lane cannot fix a client gate.

        The deadline is recorded first: with a pin in the torrc a plain restart
        comes back on the very same exit. No confirm gate and no second opinion
        -- real traffic is the strongest evidence there is.

        One strike, deliberately. A two-strike gate was tried and reverted: it
        overruled this design for a saving of about 2 minutes of lane downtime
        across the whole log (8 owner 429s x ~15s a reboot), and
        `verify_limit_gates` pins the one-strike behaviour. The measurements
        that argued for it were real -- 17 of 23 exits that 429'd served a 200
        afterwards, and the far end's `retry-after` resolves to a single instant
        for every exit -- but they are not worth contradicting this rule over."""
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
