"""A lane that is down must be brought back, not skipped forever."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import health  # noqa: E402
from lingling.lanes import Lane  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    """Detail is the failure reason, so only show it when it failed."""
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    if not ok:
        FAILS.append(name)


class FakeProc:
    def __init__(self, alive=True):
        self._alive = alive

    def poll(self):
        return None if self._alive else 1


class FakeTor:
    def __init__(self, lanes):
        self.lanes = lanes
        self.restarted = []

    def restart_lane(self, lane, repin=False):
        self.restarted.append(lane.index)
        return True


def lane(i, *, process, healthy, wanted, healing=False):
    return Lane(index=i, socks_port=50000 + i, control_port=51000 + i,
                exit_country="de", data_dir=Path("."),
                process=process, healthy=healthy, wanted=wanted,
                healing=healing)


def drive(lanes):
    tor = FakeTor(lanes)
    events = []
    d = health.HealthDaemon(tor, event=events.append, log=lambda *a: None)
    d.check_once()
    return tor, events


def main():
    print("=== a lane whose launch FAILED (no process) ===")
    tor, events = drive([lane(1, process=None, healthy=False, wanted=True)])
    print(f"  restarted={tor.restarted}  events={len(events)}")
    check("it is brought back",
          tor.restarted == [1],
          f"restarted={tor.restarted} -- a failed launch was terminal")
    check("and the log says so",
          any(e.get("type") == "lane" and e.get("kind") == "up" for e in events),
          f"no lane event: {events}")

    print("\n=== a lane that went UNHEALTHY with a live process ===")
    tor, events = drive([lane(2, process=FakeProc(alive=True), healthy=False,
                              wanted=True)])
    print(f"  restarted={tor.restarted}  events={len(events)}")
    check("it is brought back",
          tor.restarted == [2],
          f"restarted={tor.restarted} -- an unhealthy lane was never "
          f"re-probed")

    print("\n=== a lane the CLI has NOT started yet must be left alone ===")
    tor, events = drive([lane(3, process=None, healthy=None, wanted=False)])
    print(f"  restarted={tor.restarted}  events={len(events)}")
    check("it is not touched",
          tor.restarted == [],
          f"restarted={tor.restarted} -- reviving it would race the CLI's own "
          f"staggered boot")
    check("and nothing is reported about it", events == [], str(events))

    print("\n=== a lane that is MID-LAUNCH must not be restarted ===")
    # `_launch_lane` sets healthy=False at the start and takes
    tor, events = drive([lane(5, process=FakeProc(alive=True), healthy=False,
                              wanted=True, healing=True)])
    print(f"  restarted={tor.restarted}  events={len(events)}")
    check("the in-flight launch is left to finish",
          tor.restarted == [],
          f"restarted={tor.restarted} -- the daemon killed a booting lane")

    print("\n=== a healthy lane is not restarted ===")
    tor, events = drive([lane(4, process=FakeProc(alive=True), healthy=True,
                              wanted=True)])
    print(f"  restarted={tor.restarted}")
    check("left alone", tor.restarted == [], f"restarted={tor.restarted}")

    print("\n=== orphaned tor processes are reaped before any lane launches ===")
    # A tor that outlived its lingling holds its
    from lingling import netutil
    from lingling.lanes import TorManager

    seen = {}
    real_pids = netutil.pids_on_ports
    real_kill = netutil.kill_pid

    class Mgr:
        socks_base = 52001
        control_base = 52301

    def fake_pids(ports):
        ports = list(ports)
        seen["asked"] = ports
        # Only ports we were ASKED about. A real
        want = set(ports)
        return {p: pid for p, pid in ((52003, 111), (9999, 222)) if p in want}

    killed = []
    netutil.pids_on_ports = fake_pids
    netutil.kill_pid = lambda pid, grace_s=2.0: (killed.append(pid), True)[1]
    try:
        n = TorManager._reap_orphans(Mgr())
    finally:
        netutil.pids_on_ports = real_pids
        netutil.kill_pid = real_kill

    asked = seen.get("asked", [])
    print(f"  asked for {len(asked)} ports; killed={killed}")
    check("it sweeps the whole lane port range",
          len(asked) > 100,
          f"only asked about {len(asked)} ports")
    check("it kills the leftover holding a lane port",
          killed == [111],
          f"killed={killed}")
    # The scoping guarantee: it never even ASKS about
    lo = min(asked) if asked else 0
    hi = max(asked) if asked else 0
    check("and it only ever asks about ports in the lane range",
          lo >= Mgr.socks_base and hi <= Mgr.control_base + 4000,
          f"asked about {lo}..{hi} -- outside the lane range")

    print("\n=== lane dirs for lanes the pool does not have are pruned ===")
    # A lane dir is ~47 MB, almost all
    import tempfile
    from lingling.lanes import TorManager as _TM

    tmp = Path(tempfile.mkdtemp(prefix="lingling-prune-"))
    for n in range(1, 6):
        (tmp / f"tor-{n}").mkdir()
        (tmp / f"tor-{n}" / "cached-microdescs").write_text("x" * 100)
    (tmp / "keepme").mkdir()               # not a lane dir
    (tmp / "tor-notanumber").mkdir()       # matches nothing

    class Mgr2:
        lanes_dir = tmp

        def __init__(self):
            class L:
                pass
            self.lanes = [L(), L()]
            self.lanes[0].index, self.lanes[1].index = 1, 2

    pruned = _TM._prune_lane_dirs(Mgr2())
    left = sorted(p.name for p in tmp.iterdir())
    print(f"  pruned={pruned}  left={left}")
    check("the unconfigured lane dirs are removed",
          pruned == 3 and not (tmp / "tor-3").exists(),
          f"pruned={pruned}, left={left}")
    check("the live lanes' dirs are kept",
          (tmp / "tor-1").exists() and (tmp / "tor-2").exists(),
          f"left={left} -- a running lane's data was deleted")
    check("nothing that is not a lane dir is touched",
          (tmp / "keepme").exists() and (tmp / "tor-notanumber").exists(),
          f"left={left}")

    print()
    if FAILS:
        print("LANE REVIVE: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("LANE REVIVE: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
