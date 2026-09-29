"""The boot gate must not restart a tor that is still downloading."""
import sys
import itertools
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import cli  # noqa: E402
from lingling.lanes import Lane, TorManager  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def lane(tmp: Path, i: int = 1) -> Lane:
    return Lane(index=i, socks_port=52000 + i, control_port=52300 + i,
                exit_country="us", data_dir=tmp / f"tor-{i}")


class FakeClock:
    """The gate reads time.time() and sleeps; both are bent here."""

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def advance(self, secs):
        self.now += secs


class FakeLoader:
    def __init__(self):
        self.messages = []

    def set(self, msg):
        self.messages.append(msg)


class FakeDaemon:
    """Answers 0 (nothing) until `good_at`, then a real 200."""

    def __init__(self, good_at=float("inf")):
        self.good_at = good_at
        self.clock = None

    def reachable(self, _lane):
        if self.clock is not None and self.clock.now >= self.good_at:
            return 200
        return 0

    def on_refused(self, _lane, _status):
        pass


class FakeTor:
    """The manager surface the gate touches, with controllable evidence."""

    def __init__(self, cache_at, mtime_fn=None):
        self.calls = []
        self.cache_at = cache_at
        self._mtime_fn = mtime_fn

    def restart_lane(self, ln, repin=False):
        self.calls.append("restart")
        return True

    def unpin_lane(self, ln):
        self.calls.append("unpin")
        return True

    def regenerate_lane(self, ln):
        self.calls.append("regen")
        return True

    def lane_bootstrap_pct(self, ln):
        return -1

    def cache_mtime(self, ln):
        if self._mtime_fn is not None:
            return self._mtime_fn(ln)
        return self.cache_at


@contextmanager
def fake_clock():
    """Bend cli.time onto a FakeClock for one gate run, then restore."""
    clock = FakeClock()
    real_time, real_sleep = cli.time.time, cli.time.sleep
    cli.time.time = clock.time
    cli.time.sleep = lambda s: clock.advance(s)
    try:
        yield clock
    finally:
        cli.time.time, cli.time.sleep = real_time, real_sleep


def fresh_lane(tmp: Path) -> Lane:
    ln = lane(tmp)
    ln.data_dir.mkdir(parents=True, exist_ok=True)
    return ln


def main():
    tf = Path(__file__).parent
    import tempfile as _tf
    del tf

    print("=== lane_bootstrap_pct reads the notice sink, not circuit noise ===")
    reader = TorManager.lane_bootstrap_pct

    class M:
        pass

    d = Path(_tf.mkdtemp(prefix="ll-coldpct-"))
    ln = fresh_lane(d)
    (ln.data_dir / "tor.log").write_text(
        "info [circ] circuit opened\ninfo [edge] bytes moved\n",
        encoding="utf-8")
    check("circuit noise with no boot.log is no evidence",
          reader(M(), ln) == -1, "tor.log noise leaked into the gate")
    (ln.data_dir / "boot.log").write_text(
        "Sep 29 17:00:00.000 [notice] Bootstrapped 0% (starting): Starting\n"
        "Sep 29 17:00:05.000 [notice] Bootstrapped 14% (handshake_dir): "
        "Finishing handshake with directory server\n",
        encoding="utf-8")
    check("boot.log carries the percent to the gate",
          reader(M(), ln) == 14, f"pct={reader(M(), ln)}")
    (ln.data_dir / "boot.log").write_text("", encoding="utf-8")
    check("an empty boot.log reads as -1, not 0",
          reader(M(), ln) == -1, f"pct={reader(M(), ln)}")

    print("\n=== cache_mtime reads the descriptor files the gate watches ===")
    d2 = Path(_tf.mkdtemp(prefix="ll-coldcache-"))
    ln2 = fresh_lane(d2)
    check("no cache files -> 0.0, the cold signal",
          TorManager.cache_mtime(M(), ln2) == 0.0)
    (ln2.data_dir / "cached-microdescs").write_bytes(b"x" * 10)
    got = TorManager.cache_mtime(M(), ln2)
    check("a present cache file is reported", got > 0.0, f"got={got}")
    (ln2.data_dir / "cached-microdescs.new").write_bytes(b"y" * 10)
    check("the newest file wins",
          TorManager.cache_mtime(M(), ln2) >= got)

    print("\n=== a MOVING cache is progress: the gate holds its fire ===")
    moves = itertools.count(start=2)
    tor = FakeTor(0.0, mtime_fn=lambda _ln: float(next(moves)))
    daemon = FakeDaemon()
    loader = FakeLoader()
    tmp = Path(_tf.mkdtemp(prefix="ll-coldmove-"))
    ln3 = fresh_lane(tmp)
    with fake_clock() as clock:
        daemon.clock = clock
        ok = cli._boot_gate(tor, ln3, daemon, loader, deadline_s=600)
    print(f"  calls={tor.calls}  sim={clock.now - 1_000_000.0:.0f}s")
    check("a downloading tor is never restarted",
          tor.calls == [],
          f"calls={tor.calls} -- the restart loop came back")
    check("the gate ran out its full budget instead of flinching early",
          clock.now - 1_000_000.0 >= 598,
          f"only {clock.now - 1_000_000.0:.0f}s of a 600s budget used")
    check("and the user is told what is happening",
          any("relay directory" in m for m in loader.messages),
          f"messages={loader.messages[:3]}")
    check("the gate reports failure honestly at the deadline", ok is False)

    print("\n=== a tor silent in BOTH sources is still escalated ===")
    tor = FakeTor(1_000_000.0)
    daemon = FakeDaemon()
    loader = FakeLoader()
    tmp = Path(_tf.mkdtemp(prefix="ll-colddead-"))
    ln4 = fresh_lane(tmp)
    with fake_clock() as clock:
        daemon.clock = clock
        cli._boot_gate(tor, ln4, daemon, loader, deadline_s=600)
    print(f"  calls={tor.calls}  sim={clock.now - 1_000_000.0:.0f}s")
    check("restart comes first", tor.calls[:1] == ["restart"],
          f"calls={tor.calls}")
    check("then unpin", "unpin" in tor.calls, f"calls={tor.calls}")
    check("then regenerate", "regen" in tor.calls, f"calls={tor.calls}")
    check("escalation stayed bounded at three",
          len(tor.calls) == 3, f"calls={tor.calls}")

    print("\n=== a long cold download that ENDS is a success, not a timeout ===")
    moves = itertools.count(start=2)
    tor = FakeTor(0.0, mtime_fn=lambda _ln: float(next(moves)))
    daemon = FakeDaemon(good_at=1_000_000.0 + 200)
    loader = FakeLoader()
    tmp = Path(_tf.mkdtemp(prefix="ll-coldok-"))
    ln5 = fresh_lane(tmp)
    with fake_clock() as clock:
        daemon.clock = clock
        ok = cli._boot_gate(tor, ln5, daemon, loader, deadline_s=600)
    print(f"  calls={tor.calls}  sim={clock.now - 1_000_000.0:.0f}s")
    check("the lane comes up and the gate says so", ok is True, f"ok={ok}")
    check("no pokes were spent on the download",
          tor.calls == [], f"calls={tor.calls}")

    print()
    if FAILS:
        print("COLD BOOT: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("COLD BOOT: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
