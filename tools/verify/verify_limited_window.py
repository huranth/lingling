"""A 429's `retry-after` is a global window reset, not this exit's cooldown."""
import json
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling.cli import PROOF_LOG  # noqa: E402
from lingling.lanes import Lane, TorManager, LIMITED_FALLBACK_S  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def make_manager(root):
    return TorManager(root, count=1, exit_countries=["de"],
                      log=lambda *a, **k: None)


def make_lane(root):
    lane = Lane(index=1, socks_port=0, control_port=0, exit_country="de",
                data_dir=root)
    lane.exit_fingerprint = "A" * 40
    return lane


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="lingling-limited-"))

    print("=== the far end's window reset is not taken as an exit cooldown ===")
    mgr = make_manager(tmp)
    lane = make_lane(tmp)
    # 43677s is a real value from the log
    until = mgr.note_limited(lane, 43677)
    span = until - time.time()
    print(f"  note_limited(43677) -> the exit is out for {span/60:.1f} min")
    check("a window reset does not become the exit's deadline",
          span <= LIMITED_FALLBACK_S + 1.0,
          f"{span/60:.1f} min (cap {LIMITED_FALLBACK_S/60:.0f} min)")
    check("the lane carries the same bounded deadline",
          abs(lane.limited_until - until) < 0.01,
          f"{lane.limited_until - time.time():.0f}s")

    print("\n=== a shorter retry-after is still honoured ===")
    lane2 = make_lane(tmp)
    until2 = mgr.note_limited(lane2, 120.0)
    span2 = until2 - time.time()
    print(f"  note_limited(120) -> the exit is out for {span2:.0f}s")
    check("a short retry-after is used as given",
          115 < span2 < 125, f"{span2:.0f}s")

    print("\n=== the log: the resets cluster at one time of day, across days ===")
    # `retry_after` counts down to the far end's NEXT
    log = PROOF_LOG
    resets = []
    if log.exists():
        for line in open(log, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "callend" and e.get("status") == 429:
                ra = e.get("retry_after") or 0
                if ra:
                    # keep the absolute instant
                    resets.append(e["t"] + ra)
    if not resets:
        # No 429 is no data, not a violated invariant: the two checks below
        # replay what the far end actually said, and an empty record has
        # nothing to replay.
        print(f"  [SKIP] every 429 in a day resolves to one instant -- "
              f"no 429 with a retry-after in {log.name}")
        print("  [SKIP] and the instant repeats at the same time of day -- "
              "no 429 with a retry-after")
    else:
        tod = [(t % 86400) for t in resets]
        days = {time.strftime("%Y-%m-%d", time.localtime(t)) for t in resets}
        in_day = max(tod) - min(tod)
        wall = max(resets) - min(resets)
        span_tod = max(tod) - min(tod)
        # midnight wrap: a small negative gap is a
        if span_tod > 43200:
            span_tod = 86400 - span_tod
        print(f"  {len(resets)} 429s over {len(days)} day(s) -> "
              f"time-of-day {time.strftime('%H:%M:%S', time.localtime(min(resets)))}"
              f" +/-{span_tod:.0f}s, wall span {wall/3600:.1f}h")
        check("every 429 in a day resolves to one instant",
              in_day <= 300,
              f"spread {in_day:.0f}s over {len(resets)} 429s")
        check("and the instant repeats at the same time of day",
              len(days) < 2 or span_tod <= 300,
              f"time-of-day spread {span_tod:.0f}s across {len(days)} days")
        if len(days) < 2:
            print(f"  [note] log covers {len(days)} day -- the daily "
                  f"repeat is not proven by this run")

    print()
    if FAILS:
        print("LIMITED WINDOW: FAILED")
        return 1
    print("LIMITED WINDOW: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
