"""The notepad's two sections: what limited an exit, and until when."""
import json
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import proof  # noqa: E402
from lingling.cli import PROOF_LOG  # noqa: E402
from lingling.bench import (FREE_LIMIT, LIMITED_PATH, RATE_LIMIT,  # noqa: E402
                            RATE_LIMIT_FALLBACK_S, next_free_reset)
from lingling.lanes import Lane, TorManager

FAILS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def make_manager(root):
    return TorManager(root, count=1, exit_countries=["de"],
                      log=lambda *a, **k: None)


def make_lane(root, fingerprint="A" * 40):
    lane = Lane(index=1, socks_port=0, control_port=0, exit_country="de",
                data_dir=root)
    lane.exit_fingerprint = fingerprint
    return lane


def pane(status, note, retry_after=0.0, limit=""):
    """One proof-pane line, as the live log writes it."""
    return proof._render({
        "type": "callend", "t": time.time(), "n": 1, "c": 1, "lane": 1,
        "cc": "at", "status": status, "kb": 0.1, "secs": 2.0, "err": "",
        "cut": False, "reused": False, "raced": 4, "note": note,
        "retry_after": retry_after, "limit": limit})


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="lingling-limited-"))

    print("=== the free wall: the exit is out until the reset it named ===")
    mgr = make_manager(tmp)
    lane = make_lane(tmp)
    until = mgr.note_limited(lane, 43677, FREE_LIMIT)
    span = until - time.time()
    print(f"  note_limited(43677, free) -> the exit is out {span/60:.1f} min")
    check("the wall's own reset becomes the deadline",
          43675 < span < 43679, f"{span/60:.1f} min")
    check("the lane carries the same deadline",
          abs(lane.limited_until - until) < 0.01,
          f"{lane.limited_until - time.time():.0f}s")
    check("and the lane names the section",
          lane.limit_reason == FREE_LIMIT, repr(lane.limit_reason))

    print("\n=== no header at all: the wall's reset is the next 00:00 UTC ===")
    lane2 = make_lane(tmp)
    until2 = mgr.note_limited(lane2, 0.0, FREE_LIMIT)
    stamp = time.gmtime(until2)
    check("a headerless wall benches to the next 00:00 UTC",
          (stamp.tm_hour, stamp.tm_min, stamp.tm_sec) == (0, 0, 0),
          time.strftime("%Y-%m-%d %H:%M:%S UTC", stamp))
    check("which is never more than a day out",
          until2 - time.time() <= 86400,
          f"{(until2 - time.time())/3600:.1f}h")

    print("\n=== a nonsense reset cannot take an exit out forever ===")
    lane3 = make_lane(tmp)
    span3 = mgr.note_limited(lane3, 5 * 86400, FREE_LIMIT) - time.time()
    print(f"  a five-day claim -> the exit is out {span3/3600:.1f}h")
    check("the claim is clipped to the ceiling",
          span3 <= 86400 + 300, f"{span3/3600:.1f}h")

    print("\n=== the burst limit: 25 minutes, or the header, whichever is less ===")
    lane4 = make_lane(tmp)
    span4 = mgr.note_limited(lane4, 0.0, RATE_LIMIT) - time.time()
    print(f"  no header -> the exit is out {span4/60:.1f} min")
    check("no header means the stand-off",
          abs(span4 - RATE_LIMIT_FALLBACK_S) < 2, f"{span4:.0f}s")
    check("and it is written in the rate section",
          lane4.limit_reason == RATE_LIMIT, repr(lane4.limit_reason))
    lane5 = make_lane(tmp)
    span5 = mgr.note_limited(lane5, 30.0, RATE_LIMIT) - time.time()
    check("a short header is used as given", 29 <= span5 <= 31, f"{span5:.0f}s")
    lane6 = make_lane(tmp)
    span6 = mgr.note_limited(lane6, 9 * 3600, RATE_LIMIT) - time.time()
    print(f"  a nine-hour claim -> the exit is out {span6/60:.0f} min")
    check("a long header cannot outlast the stand-off",
          span6 <= RATE_LIMIT_FALLBACK_S + 1, f"{span6/60:.1f} min")

    print("\n=== the notepad keeps the two sections apart, keyed by exit ===")
    book = pathlib.Path(tempfile.mkdtemp(prefix="ll-sections-"))
    mgr2 = make_manager(book)
    mgr2.note_limited(make_lane(book, "F" * 40), 3600.0, FREE_LIMIT)
    mgr2.note_limited(make_lane(book, "R" * 40), 0.0, RATE_LIMIT)
    rec = json.loads((book / LIMITED_PATH).read_text())
    sections = {fp[0]: entry.get("reason") for fp, entry in rec.items()}
    print(f"  on disk -> {sections}")
    check("a free exit and a rate exit are told apart",
          sections.get("F") == FREE_LIMIT and sections.get("R") == RATE_LIMIT,
          str(sections))
    check("every entry carries a deadline and a section",
          all(set(entry) == {"until", "reason"} and entry["until"] > time.time()
              for entry in rec.values()), str(rec))

    print("\n=== an expired entry, and an older notepad, both stay honest ===")
    stale = pathlib.Path(tempfile.mkdtemp(prefix="ll-stale-"))
    (stale / LIMITED_PATH).write_text(json.dumps(
        {"D" * 40: {"until": time.time() - 5, "reason": FREE_LIMIT}}))
    check("an expired entry is not loaded",
          "D" * 40 not in make_manager(stale)._limited)
    old = pathlib.Path(tempfile.mkdtemp(prefix="ll-old-"))
    (old / LIMITED_PATH).write_text(json.dumps({"B" * 40: time.time() + 600}))
    kept = make_manager(old)._limited
    check("a bare deadline from the older file is still honoured",
          kept.get("B" * 40, {}).get("until", 0) > time.time(), str(kept))

    print("\n=== the pane names the section, and never benches their edge ===")
    wall = next_free_reset() - time.time()
    free = pane(429, '{"error":{"type":"FreeUsageLimitError"}}', wall,
                FREE_LIMIT)
    check("the free line names the section and the wall's reset",
          "free limit" in free and "moving it" in free
          and "00:00 UTC" in free, f"line={free!r}")
    rate = pane(429, '{"error":{"type":"rate"}}', 0, RATE_LIMIT)
    check("the rate line names its stand-off",
          "rate limit" in rate and "25m" in rate, f"line={rate!r}")
    edge = pane(429, '{"error":{"type":"server_error"}}', 0, "")
    check("their edge is loud, and owns no exit",
          "OPENCODE EDGE UNAVAILABLE" in edge and "not this exit" in edge,
          f"line={edge!r}")
    legacy = pane(429, '{"type":"error","error":{"type":"FreeUsageLimitError"}}',
                  12373)
    check("an older log event is classified from its own body",
          "free limit" in legacy, f"line={legacy!r}")
    nine = 9 * 3600
    stored = mgr.note_limited(make_lane(tmp, "N" * 40), nine, RATE_LIMIT)
    span9 = stored - time.time()
    shown = pane(429, '{"error":{"type":"rate"}}', nine, RATE_LIMIT)
    check("the pane shows the bench the notepad applies, not the raw header",
          f"{round(span9 / 60)}m" in shown
          and span9 <= RATE_LIMIT_FALLBACK_S + 1,
          f"stored={span9:.0f}s line={shown.strip()!r}")

    print("\n=== the live log: every wall in a day resolves to one instant ===")
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
                    # keep
                    resets.append(e["t"] + ra)
    if not resets:
        print(f"  [SKIP] every 429 in a day resolves to one instant -- "
              f"no 429 with a retry-after in {log.name}")
        print("  [SKIP] and the instant repeats at the same time of day -- "
              "no 429 with a retry-after")
    else:
        tod = [(t % 86400) for t in resets]
        days = {time.strftime("%Y-%m-%d", time.gmtime(t)) for t in resets}
        # A window that resets at 23:59:59 UTC lands on both sides of the
        # modulo, so the raw spread reads as a whole day when every reset is
        # really the same instant. The wrap is the shorter way round, and it
        # is what both checks below must judge -- the day buckets now use the
        # same UTC base the printout already did.
        span_tod = max(tod) - min(tod)
        if span_tod > 43200:
            span_tod = 86400 - span_tod
        print(f"  {len(resets)} walls over {len(days)} day(s) -> "
              f"time-of-day {time.strftime('%H:%M:%S', time.gmtime(min(resets)))}"
              f" UTC +/-{span_tod:.0f}s")
        check("every 429 in a day resolves to one instant",
              span_tod <= 300,
              f"spread {span_tod:.0f}s over {len(resets)} walls")
        check("and the instant repeats at the same time of day",
              len(days) < 2 or span_tod <= 300,
              f"time-of-day spread {span_tod:.0f}s across {len(days)} days")
        if len(days) < 2:
            print(f"  [note] log covers {len(days)} day -- the daily "
                  f"repeat is not proven by this run")

    print()
    for name in FAILS:
        print(f"  FAILED: {name}")
    if FAILS:
        print("\nLIMITED WINDOW: FAILED")
        return 1
    print("LIMITED WINDOW: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
