"""A probe 429 must actually reach the refusal handler."""
import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import health, netutil  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    """Detail is the failure reason, so only show it when it failed."""
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    if not ok:
        FAILS.append(name)


class FakeLane:
    def __init__(self):
        self.index = 1
        self.socks_port = 9050
        self.exit_country = "de"
        self.exit_ip = None
        self.process = _AliveProcess()
        self.asked = False
        self.probe_code = -1
        self.healthy = True
        # asked to run
        self.wanted = True
        # not mid-launch
        self.healing = False
        self.limited_until = 0.0
        self.exit_fingerprint = "FAKEFP"


class _AliveProcess:
    def poll(self):
        return None


class FakeTor:
    def __init__(self):
        self.lanes = [FakeLane()]
        self.notes = []
        self.rotated = []

    def restart_lane(self, lane, repin=False):
        return False

    def rotate_exit_country(self, lane):
        self.rotated.append(lane.index)
        return True

    def note_result(self, country, code):
        self.notes.append((country, code))

    def note_limited(self, lane, retry_after=0.0):
        lane.limited_until = 1e12

    def healthy_lanes(self):
        return list(self.lanes)


@contextlib.contextmanager
def daemon(status, events=None):
    """A daemon with the transport stubbed to answer `status`."""
    stubs = {
        "port_is_open": lambda *a, **k: True,
        "https_via_socks": lambda *a, **k: (
            status, b'{"error":{"type":"FreeUsageLimitError"}}'),
        "https_get_via_socks": lambda *a, **k: (
            200, b'{"IsTor":true,"IP":"203.0.113.9"}'),
    }
    tor = FakeTor()
    d = health.HealthDaemon(tor, check_interval=0.05,
                            event=(events.append if events is not None
                                   else None))
    saved = {n: getattr(netutil, n) for n in stubs}
    for n, fn in stubs.items():
        setattr(netutil, n, fn)
    try:
        yield tor, d
    finally:
        for n, fn in saved.items():
            setattr(netutil, n, fn)


def main():
    print("=== reachable() propagates the far end's status, not a constant ===")
    for status in (429, 403, 0, 500):
        with daemon(status) as (tor, d):
            code = d.reachable(tor.lanes[0])
        check(f"reachable() returns {status} unchanged", code == status,
              f"got {code}")

    print("\n=== a probe 429 moves the lane; a 403 does not ===")
    with daemon(429) as (tor, d):
        d.check_once()
    check("a probe 429 rotates the lane", tor.rotated == [1],
          f"rotated={tor.rotated}")
    check("a probe 429 is recorded against the country",
          tor.notes == [("de", 429)], f"notes={tor.notes}")
    check("a probe 429 marks the exit limited -- the lane did not stay put",
          tor.lanes[0].limited_until > 0, "deadline not set")

    with daemon(403) as (tor, d):
        d.check_once()
    check("a probe 403 moves nothing", not tor.rotated and not tor.notes,
          f"rotated={tor.rotated} notes={tor.notes}")
    check("a probe 403 still learns the exit IP",
          tor.lanes[0].exit_ip == "203.0.113.9",
          f"exit_ip={tor.lanes[0].exit_ip!r}")

    print("\n=== a non-verdict is NOT a verdict about the lane ===")
    # The rule used to be `if code:`, so
    for status in (401, 500):
        events = []
        with daemon(status, events) as (tor, d):
            d.check_once()
        lane = tor.lanes[0]
        kinds = [e.get("kind") for e in events]
        print(f"  probe {status}: asked={lane.asked} rotated={tor.rotated} "
              f"kinds={kinds}")
        check(f"a probe {status} does not mark the lane asked",
              lane.asked is False,
              f"asked={lane.asked} -- the lane is never probed again, so its "
              f"quota is never checked for the rest of the session")
        check(f"a probe {status} moves nothing",
              not tor.rotated and not tor.notes,
              f"rotated={tor.rotated} notes={tor.notes}")
        check(f"a probe {status} is reported as a probe failure",
              "probe" in kinds,
              f"kinds={kinds} -- a non-verdict passed silently")

    with daemon(403) as (tor, d):
        d.check_once()
    check("a probe 403 still marks the lane asked",
          tor.lanes[0].asked is True, f"asked={tor.lanes[0].asked}")

    print("\n=== the branch is load-bearing: it fails on the old code ===")
    calls = []
    real = health.HealthDaemon.on_refused

    def spy(self, lane, status=429):
        calls.append(status)
        return real(self, lane, status)

    health.HealthDaemon.on_refused = spy
    try:
        with daemon(429) as (tor, d):
            d.check_once()
    finally:
        health.HealthDaemon.on_refused = real
    check("check_once drives on_refused with the real status",
          calls == [429], f"calls={calls}")

    print()
    if FAILS:
        print("PROBE REFUSAL BRANCH: FAILED")
        return 1
    print("PROBE REFUSAL BRANCH: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
