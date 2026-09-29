"""Floors under the three windows nothing was guarding.

    python tools/verify/verify_window_floors.py

Why this exists. `tools/verify/sweep_guards.py` breaks every env-overridable
value and reports which suites fail. Two are guarded — `_SEND_TIMEOUT` by
`verify_send_window` (120s against a measured 29.8s max) and `_KEEPALIVE_S` by
`verify_pool_ttl` (600s against his 70s lap) — and both got there because they
were MEASURED into place, so a guard had a number to defend.

The other three were set by reasoning and had no live guard at all: every
suite that touches them PINS its own window (the convention, to stop hangs),
so setting `LINGLING_FIRST_BYTE_S=2`, `LINGLING_STREAM_TIMEOUT=2` or
`LINGLING_STREAM_IDLE_S=2` leaves the whole suite set green. A value that can
be set to nonsense without a single test noticing is not protected by the
convention that made it untestable.

These floors read the LIVE module value, not the source text. `verify_audit.py`
already has a floor for `_STREAM_IDLE_TIMEOUT`, but it greps the default string
out of `mitm.py`, so it defends the shipped default and cannot see an env
override at all. Reading the live value catches both.

The numbers:

  600s for the two stream windows. The head read IS time-to-first-token, and
  at 20s it cut real answers — his `effort=xhigh` calls died at 0.0 KB after
  20s and retried onto a lane thinking at the same speed, so the retry could
  never win. 20s was measured as too small; 600s is 30x the longest healthy
  gap (16.6s) and is the same floor the audit already uses for the idle phase.

  10s for the cold connect, deliberately LOOSE. This window covers the cold
  connect only, and the measured warm dial is 546ms + 723ms. There is no
  measured cold maximum, so this floor is not a claim that 30s is right — it
  exists to catch a shrink into single seconds, which would fail circuits that
  do succeed today. Do not tighten it without a measured cold-connect
  distribution.
"""
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
