"""Floors under the three windows nothing was guarding."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import mitm  # noqa: E402

FAILS = []

#: see the docstring: 600s is 30x the longest healthy gap; 10s is loose on purpose
STREAM_FLOOR = 600.0
COLD_FLOOR = 10.0


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    if not ok:
        FAILS.append(name)


def main():
    print(f"shipped: cold={mitm._FIRST_BYTE_TIMEOUT}s  "
          f"read={mitm._READ_TIMEOUT}s  idle={mitm._STREAM_IDLE_TIMEOUT}s")

    print("\n=== the two stream windows: a silent peer is never cut ===")
    for name, val in (("pre-commit read", mitm._READ_TIMEOUT),
                      ("post-commit idle", mitm._STREAM_IDLE_TIMEOUT)):
        check(f"the {name} window is at least {STREAM_FLOOR:.0f}s",
              val >= STREAM_FLOOR,
              f"{name}={val}s -- the head read IS the model's time-to-first-"
              f"token, and 20s was measured cutting real xhigh answers at "
              f"0.0 KB; a shrink here is that bug returning")

    print("\n=== the cold-connect budget ===")
    check(f"the cold connect is at least {COLD_FLOOR:.0f}s",
          mitm._FIRST_BYTE_TIMEOUT >= COLD_FLOOR,
          f"_FIRST_BYTE_TIMEOUT={mitm._FIRST_BYTE_TIMEOUT}s -- the warm dial "
          f"alone measures 546ms + 723ms; single seconds would fail circuits "
          f"that succeed today")

    print()
    if FAILS:
        print("WINDOW FLOORS: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("WINDOW FLOORS: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
