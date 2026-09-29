"""Offline proof of the only two things allowed to move a lane. No network, no
tor, and no fake clock -- there is nothing left to sweep.

Run against whichever install is live:

    python tools/verify/verify_limit_gates.py

A lane may be moved by the far end's own words (a 429 or a 403), or restarted
because its tor process has exited. Those are the two facts. Everything this
codebase used to do on top -- probe verdicts, stall strikes, first-strike
pulls, sidelining, revival ladders, benchmarking -- was inference about exits
we had never measured, and it retired healthy ones. Five accused exits were
re-tested with a raw request: not one answered 429 or 403.
"""
import asyncio
import inspect
import io
import sys
import time

sys.path.insert(0, r"C:/Users/W/AppData/Local/Programs/Python/Python312/Lib/site-packages")

from pathlib import Path  # noqa: E402

from lingling import health as H  # noqa: E402
from lingling import mitm  # noqa: E402
from lingling.lanes import Lane  # noqa: E402
from lingling.relay import Relay  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (("   " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


class FakeProc:
    """A tor process stand-in: poll() is None while it is alive."""

    def __init__(self, code=None):
        self._code = code

    def poll(self):
        return self._code


class FakeTor:
    """Lanes with a controllable process, and a record of what was asked."""

    def __init__(self, n=6):
        # `wanted=True` because these lanes are meant to be RUNNING: the health
        # daemon now leaves a lane alone until something has asked for it, which
        # is what stops it racing the CLI's staggered boot. A test lane that
        # never says it is wanted is a lane nobody asked for.
        self.lanes = [
            Lane(index=i, socks_port=52000 + i, control_port=52300 + i,
                 exit_country="xx", data_dir=Path("."),
                 process=FakeProc(), healthy=True, wanted=True)
            for i in range(1, n + 1)
        ]
        self.limit_hook = None
        self.notes = []
        self.restarted = []
        self.rotated = []

    def healthy_lanes(self):
        return [l for l in self.lanes if l.healthy and not l.healing]

    def score_of(self, country):
        return 0

    def note_result(self, country, status):
        self.notes.append((country, status))

    def rotate_exit_country(self, lane):
        self.rotated.append(lane.index)
        lane.exit_country = "yy"
        return "yy"

    def note_limited(self, lane, retry_after=0.0):
        lane.limited_until = time.time() + 3600.0
        return lane.limited_until

    def restart_lane(self, lane):
        self.restarted.append(lane.index)
        return True

    def regenerate_lane(self, lane):
        return True


def daemon(n=6):
    tor = FakeTor(n)
    ev = []
    d = H.HealthDaemon(tor, event=ev.append, log=lambda *a: None)
    return tor, d, ev


print("\n=== A. a real 429 moves that one lane to a fresh exit ===")
# A 429 is the far end's own words about one exit IP, so it is the strongest
# evidence there is. No confirm gate, no second opinion.
tor, _d, ev = daemon()
Relay(tor).report_refused(tor.lanes[0], 429)
check("the exit is scored", tor.notes == [("xx", 429)], str(tor.notes))
check("the lane is re-pinned", tor.restarted == [1], str(tor.restarted))
check("and it says so", any("429" in e.get("msg", "") for e in ev),
      str([e.get("msg") for e in ev]))
check("no other lane is touched", tor.restarted == [1], str(tor.restarted))
check("the lane is usable again", tor.lanes[0].healthy is True,
      str(tor.lanes[0].healthy))
check("and it moved COUNTRY, not just relay", tor.rotated == [1],
      str(tor.rotated))
check("the message names the new country",
      any("{yy}" in e.get("msg", "") for e in ev),
      str([e.get("msg") for e in ev]))

print("\n=== B. a 403 moves nothing ===")
# The free tier gates on the CLIENT: a hand-rolled request gets
# `403 FreeTierError "can only be used from within OpenCode"` whatever headers
# it sends, and lingling's own --demo and probes are hand-rolled. Real opencode
# never sees it -- measured, driving the real binary: 8 runs, only 429, no 403.
# Re-pinning a lane cannot fix a client gate, so 403 is not retried at all.
check("403 is not retryable", 403 not in mitm._RETRYABLE)
check("429 is retryable", 429 in mitm._RETRYABLE)
check("there is no second refusal set", not hasattr(mitm, "_REFUSED"))
# The heading above promises this and it was never checked, which is how a 403
# came to be rotating and restarting lanes. `on_refused` now returns on
# anything but a 429, so a 403 must move nothing.
tor, _d, _ev = daemon()
Relay(tor).report_refused(tor.lanes[0], 403)
check("a 403 moves nothing", not tor.restarted and not tor.rotated,
      f"restarted={tor.restarted} rotated={tor.rotated}")

print("\n=== C. a lane whose tor has exited is restarted ===")
# A process fact, not a judgement about the exit.
tor, d, ev = daemon()
tor.lanes[1].process = FakeProc(code=1)
d.check_once()
check("the dead lane is restarted", tor.restarted == [2], str(tor.restarted))
# Check the EVENT, not its wording. This asserted `"went down" in msg` and so
# broke the moment the message was reworded to cover a lane whose launch failed
# (which has not "gone down", it never came up). The intent is "the pane is
# told", so measure that.
check("and it says so",
      any(e.get("type") == "lane" and e.get("kind") == "up"
          and e.get("lane") == 2 for e in ev),
      str([e.get("msg") for e in ev]))

print("\n=== D. a lane already asked is left alone ===")
# No periodic probing: a lane is asked once, for its exit IP, and then not
# again. Only the far end's refusal or its tor exiting moves it.
tor, d, ev = daemon()
for _l in tor.lanes:
    _l.asked = True
d.check_once()
check("nothing is restarted", tor.restarted == [], str(tor.restarted))
check("no lane is marked unhealthy", all(l.healthy for l in tor.lanes),
      str([l.healthy for l in tor.lanes]))
check("nothing is reported", ev == [], str(ev))

print("\n=== D2. a new lane is asked once, for its exit IP ===")
tor, d, ev = daemon(2)
asked = []
d.reachable = lambda lane: (asked.append(lane.index), 200)[1]
d.check_once()
check("each unasked lane is asked once", asked == [1, 2], str(asked))
check("and the pane is told which exit it rides",
      all("is cooking" in e.get("msg", "") for e in ev), str(len(ev)))
asked.clear()
d.check_once()
check("and never asked again", asked == [], str(asked))


class FakeReader:
    def __init__(self, data):
        self._b = io.BytesIO(data)

    async def readline(self):
        return self._b.readline()


class FakeWriter:
    def __init__(self):
        self.got = b""

    def write(self, d):
        self.got += d

    async def drain(self):
        pass

    def close(self):
        pass

    def get_extra_info(self, name):
        return None


async def _unreachable(lane, host, port):
    raise OSError("no route to host")


print("\n=== E. a failed dial retires nothing ===")
# The plain-CONNECT path carries everything that is not an LLM API host --
# github, npm, the search MCPs. It used to mark a lane dead the moment a dial
# raised, so one transient failure to one host pulled a healthy lane.
tor, d, ev = daemon()
relay = Relay(tor, event=ev.append)
relay.wait_budget = 0.05  # do not sit in the retry loop for 90s
relay._dial = _unreachable
head = (b"CONNECT github.com:443 HTTP/1.1\r\n"
        b"host: github.com:443\r\n\r\n")
w = FakeWriter()
asyncio.run(relay._handle(FakeReader(head), w))
check("a failed dial says so", any("couldn't reach" in e.get("msg", "") for e in ev),
      str([e.get("msg") for e in ev if "reach" in e.get("msg", "")][:1]))
check("no lane is retired for it", all(l.healthy for l in tor.lanes),
      str([l.healthy for l in tor.lanes]))
check("nothing is re-cooked", tor.restarted == [], str(tor.restarted))
check("the client still gets an answer", b"502" in w.got, repr(w.got[:24]))

print("\n=== F. there is no concurrency cap ===")
# Holding traffic while a healthy lane sat idle, and describing our own limit
# as the lane being "at cap", was withdrawn. A loaded lane still carries.
tor, _d, _e = daemon()
for _l in tor.lanes:
    _l.active = 5
pick = Relay(tor).pick_lane()
check("a loaded pool still hands out a lane", pick is not None,
      str(getattr(pick, "index", None)))
check("pick_lane takes no cap",
      "max_active" not in inspect.signature(Relay.pick_lane).parameters,
      str(list(inspect.signature(Relay.pick_lane).parameters)))

print("\n" + "=" * 46)
if FAIL:
    print(f"FAILURES: {len(FAIL)}")
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("ALL GATES PASS")
