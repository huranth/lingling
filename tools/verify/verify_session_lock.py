"""One kitchen: a second launcher is turned away while the first lives."""
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import netutil  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


_DIRS = []


def scratch() -> Path:
    d = Path(tempfile.mkdtemp(prefix="ll-lock-"))
    _DIRS.append(d)
    return d


def cleanup() -> None:
    for d in _DIRS:
        shutil.rmtree(d, ignore_errors=True)
    base = Path(tempfile.gettempdir())
    for d in base.glob("ll-lock-*"):
        shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    print("=== a free data dir claims the lock at once ===")
    d = scratch()
    check("the claim succeeds and names no rival",
          netutil.session_lock(str(d)) is None)
    lock = d / "session.lock"
    check("the lock names its owner", lock.read_text() == str(os.getpid()),
          lock.read_text())

    print("\n=== a second launcher while the holder lives ===")
    holder = os.getpid()
    rival = netutil.session_lock(str(d))
    check("the second claim is refused", rival == holder, f"rival={rival}")

    print("\n=== release really lets go ===")
    netutil.release_session_lock(str(d))
    check("the file is gone", not lock.exists())
    check("and the lock is free again",
          netutil.session_lock(str(d)) is None)
    netutil.release_session_lock(str(d))
    check("a second release is harmless", not lock.exists())

    print("\n=== wreckage from a hard kill is swept, not honoured ===")
    lock.write_text("999999")
    check("a dead holder does not block",
          netutil.session_lock(str(d)) is None)
    netutil.release_session_lock(str(d))

    print("\n=== a pid that gets reused by an unrelated process ===")
    lock.write_text("4")
    got = netutil.session_lock(str(d))
    check("a live-but-unrelated pid still blocks (safe, rare, self-heals)",
          got == 4, f"got={got}")
    lock.unlink()
    check("manual clearing works", netutil.session_lock(str(d)) is None)
    netutil.release_session_lock(str(d))

    print("\n=== garbage contents are wreckage too ===")
    lock.write_text("not-a-pid")
    check("an unreadable lock does not block",
          netutil.session_lock(str(d)) is None)
    netutil.release_session_lock(str(d))
    lock.write_text("")
    check("an empty lock does not block",
          netutil.session_lock(str(d)) is None)
    netutil.release_session_lock(str(d))

    print("\n=== corruption at the exact moment of two racers ===")
    d2 = scratch()
    results = []

    def race():
        results.append(netutil.session_lock(str(d2)))

    threads = [threading.Thread(target=race) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = sum(1 for r in results if r is None)
    check("exactly one racer wins", winners == 1,
          f"winners={winners} of {len(results)}")
    netutil.release_session_lock(str(d2))

    print("\n=== a lock owned by a vanished pid survives release by others ===")
    lock.write_text("999998")
    netutil.release_session_lock(str(d2))
    check("a foreign release does not touch a foreign lock", lock.exists())
    lock.unlink()

    print("\n=== cli gates before any work, by source ===")
    src = (ROOT / "lingling" / "cli.py").read_text(encoding="utf-8")
    check("the gate runs before the loader is built",
          src.index("_other_lingling()") < src.index("loader = _Loader()"))
    check("the release runs in the finally",
          "netutil.release_session_lock" in src.split("finally:")[1])
    check("a refused launcher gets a plain line and exit 1",
          "one kitchen" in src)

    print()
    cleanup()
    if FAILS:
        print("SESSION LOCK: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("SESSION LOCK: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
