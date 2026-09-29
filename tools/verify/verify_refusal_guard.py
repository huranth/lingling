"""The health probe's 429 actually reaches the refusal handler.

`verify_limit_gates` owns the two refusal rules and now tests both of them:
section A pins that a 429 moves the lane on ONE strike with no confirm gate, and
section B pins that a 403 moves nothing. This file covers the one path that
suite does not reach -- the WIRING from `check_once` to `on_refused`.

That wiring is worth a test of its own because it has broken twice in this
codebase's history: a gate added to `on_refused` silently applied to the probe
too, so a burnt exit was not moved on the first sweep. Calling `on_refused` by
hand would not have caught it -- driving the real `check_once` does.
"""
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import health  # noqa: E402
from lingling.lanes import Lane  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


class FakeProc:
    """A tor process that is alive, so check_once does not restart it."""

    def poll(self):
        return None


class StubTor:
    """Records what the daemon asked for, and does nothing else."""

    def __init__(self):
        self.rotations = 0
        self.restarts = 0
        self.limited = 0
        self.limit_hook = None
        self.lanes = []

    def note_limited(self, lane, retry_after=0.0):
        self.limited += 1
        lane.limited_until = time.time() + 600.0
        return lane.limited_until

    def rotate_exit_country(self, lane):
        self.rotations += 1
        lane.exit_country = "zz"
        return "zz"

    def restart_lane(self, lane):
        self.restarts += 1
        lane.limited_until = 0.0
        return True

    def note_result(self, country, status):
        pass


def sweep(reply):
    """Run the real `check_once` once with the probe forced to `reply`."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="lingling-wire-"))
    tor = StubTor()
    daemon = health.HealthDaemon(tor)
    lane = Lane(index=1, socks_port=0, control_port=0, exit_country="de",
                data_dir=tmp)
    lane.exit_fingerprint = "A" * 40
    lane.process = FakeProc()
    lane.healthy = True
    # asked to run: the daemon now leaves a lane alone until something wants
    # it, which is what stops it racing the CLI's staggered boot
    lane.wanted = True
    lane.asked = False
    tor.lanes = [lane]
    daemon.reachable = lambda _lane: reply
    daemon.check_once()
    return tor, lane


def main():
    print("=== a 429 probe moves the lane in a single sweep ===")
    tor, lane = sweep(429)
    print(f"  rotations={tor.rotations} restarts={tor.restarts} "
          f"asked={lane.asked}")
    check("check_once hands a probe 429 to on_refused",
          tor.rotations == 1 and tor.restarts == 1,
          f"rotations={tor.rotations} restarts={tor.restarts} -- the probe's "
          f"429 did not reach the refusal handler")

    print("\n=== a 403 probe leaves the lane in service ===")
    # A 403 means the exit is FINE -- the gate refused the hand-rolled probe --
    # so the lane is marked asked and nothing moves.
    tor, lane = sweep(403)
    print(f"  rotations={tor.rotations} restarts={tor.restarts} "
          f"asked={lane.asked}")
    check("a 403 probe marks the lane asked and moves nothing",
          tor.rotations == 0 and tor.restarts == 0 and lane.asked is True,
          f"rotations={tor.rotations} asked={lane.asked}")

    print()
    if FAILS:
        print("PROBE REFUSAL WIRING: FAILED")
        return 1
    print("PROBE REFUSAL WIRING: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
