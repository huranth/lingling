"""Does the stall track reasoning effort? Answer it from the log, not by feel.

    python tools/soak/stall_by_effort.py [proof.log]

What this is for. The owner's report is that the timeouts only land on hard
reasoning tasks. Until `_model_of` started carrying effort, that could not be
checked at all -- the request body was never recorded, so the correlation had
no data behind it. This reads the field and cross-tabulates.

It reports and never asserts. A tool that decided the answer would be the same
mistake as theorising without data: with a handful of stalls, any split looks
like a pattern. So the output leads with the COUNTS, states whether they are
enough to conclude anything, and refuses to print a verdict when they are not.

The two shapes are kept apart on purpose, because they are different bugs:

  never spoke   kb == 0 and first_byte_s == 0   -- a cold circuit
  spoke, slowed kb > 0  and TimeoutError        -- the owner's stall

Mixing them is what made an earlier pass of this session report numbers that
described neither.
"""
import collections
import json
import pathlib
import re
import sys

DEFAULT = (pathlib.Path.home() / "AppData" / "Local" / "lingling" /
           "proof.log")

#: enough stalls per bucket before a split means anything
MIN_PER_BUCKET = 8

EFFORT = re.compile(r"effort=(\S+)")
CAP = re.compile(r"cap=(\S+)")


def read(path):
    """Every record in the log, in order."""
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
    return out


def parse_params(model_field):
    """(effort, cap) out of the `model` string, or ('-', '-')."""
    effort = EFFORT.search(model_field or "")
    cap = CAP.search(model_field or "")
    return (effort.group(1) if effort else "-",
            cap.group(1) if cap else "-")


def pair(recs):
    """(params, rows): every callend joined to its call's effort and cap.

    Keyed by (session, n, c), and the session MUST be tracked as we walk. A
    first version looked up `(None, n, c)` against params stored under a
    session tuple -- it matched nothing and reported every attempt as "?". A
    second version fixed the store but reused the `sess` left over from the
    collecting pass, which is the LAST session, so callends were matched
    against one session's params; because (n, c) repeats across sessions that
    invented buckets -- 886 "high" attempts out of 1701 when the real figure
    was 28. Both bugs were invisible to a single-session test, which is why
    `--selftest` uses two sessions sharing the same (n, c) values."""
    params = {}
    rows = []
    sess = None
    for r in recs:
        kind = r.get("type")
        if kind == "start":
            sess = r.get("session")
        elif kind == "call":
            params[(sess, r.get("n"), r.get("c"))] = parse_params(
                r.get("model", ""))
        elif kind == "callend":
            eff, cap = params.get((sess, r.get("n"), r.get("c")), ("?", "?"))
            rows.append((eff, cap, r))
    return params, rows


def selftest():
    """Two sessions, same (n, c), different effort: catches a bad join."""
    recs = []
    t = 1
    for sess, eff, stall in (("A", "high", True), ("B", "low", False)):
        recs.append({"type": "start", "t": t, "session": sess,
                     "version": "soak"})
        for i in range(10):
            n, c = i + 1, 1
            recs.append({"type": "call", "t": t, "n": n, "c": c, "lane": 1,
                         "cc": "de", "ip": "1.2.3.4", "method": "POST",
                         "path": "/zen/v1/responses",
                         "model": f"m effort={eff} cap=32000",
                         "host": "opencode.ai"})
            recs.append({
                "type": "callend", "t": t + 30, "n": n, "c": c, "lane": 1,
                "status": 0 if stall else 200, "kb": 0.0 if stall else 70.0,
                "secs": 30.0, "err": "TimeoutError" if stall else "",
                "first_byte_s": 0 if stall else 3.0})
            t += 90
    _params, rows = pair(recs)
    tally = collections.defaultdict(collections.Counter)
    for eff, _cap, r in rows:
        c = tally[eff]
        c["total"] += 1
        if r.get("err") == "TimeoutError":
            c["never"] += 1
        else:
            c["ok"] += 1
    want = {"high": (10, 10, 0), "low": (10, 0, 10)}
    got = {e: (tally[e]["total"], tally[e]["never"], tally[e]["ok"])
           for e in tally}
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] the session-aware join")
    if not ok:
        print(f"        want {want}")
        print(f"        got  {got}")
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        print("=== the join is session-aware ===")
        print()
        return selftest()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = pathlib.Path(args[0]) if args else DEFAULT
    if not path.exists():
        print(f"no log at {path}")
        return 1
    recs = read(path)

    params, rows = pair(recs)
    sessions = {r.get("session") for r in recs if r.get("type") == "start"}

    logged = {e for (e, _c) in params.values() if e != "-"}
    print(f"records        : {len(recs)}")
    print(f"sessions       : {len(sessions)}")
    print(f"attempts       : {len(params)}")
    print(f"effort values seen: {sorted(logged) if logged else 'none'}")
    print()

    if not logged:
        print("No attempt has recorded effort or cap yet.")
        print()
        print("That is expected until the relay is restarted on the build that")
        print("added it: the field is written by `_model_of` when a request")
        print("arrives, so only traffic AFTER the restart carries it.")
        print()
        print("Once there is some, run a soak that actually asks for long")
        print("answers -- the short prompt pool cannot reproduce the stall:")
        print()
        print("    python tools/soak/live_soak.py --hard 200")
        return 0

    print("=== every callend, by effort ===")
    for eff, n in collections.Counter(e for e, _c, _r in rows).most_common():
        print(f"  effort={eff:6} {n:5} attempts")

    print("\n=== outcome by effort, both stall shapes kept apart ===")
    print(f"  {'effort':8} {'total':>6} {'200':>6} {'never-spoke':>12} "
          f"{'spoke-slowed':>13}")
    by = collections.defaultdict(lambda: collections.Counter())
    for eff, _cap, r in rows:
        c = by[eff]
        c["total"] += 1
        if r.get("status") == 200:
            c["200"] += 1
        elif r.get("err") == "TimeoutError" and not r.get("kb"):
            c["never"] += 1
        elif r.get("err") == "TimeoutError" and r.get("kb"):
            c["spoke"] += 1
    for eff in sorted(by):
        c = by[eff]
        print(f"  {eff:8} {c['total']:6} {c['200']:6} {c['never']:12} "
              f"{c['spoke']:13}")

    print("\n=== is there enough to conclude anything? ===")
    stalls = {e: by[e]["never"] + by[e]["spoke"] for e in by}
    comparable = [e for e, v in stalls.items() if v >= MIN_PER_BUCKET]
    total_stalls = sum(stalls.values())
    print(f"  stalls recorded    : {total_stalls}")
    print(f"  buckets with stalls: {sum(1 for v in stalls.values() if v)}")
    print(f"  buckets at >= {MIN_PER_BUCKET} stalls: {len(comparable)}"
          f"   (need two, to compare)")

    if len(comparable) < 2:
        print()
        print("  NOT ENOUGH DATA. Any two buckets differ by a few stalls by")
        print("  chance, and one bucket alone cannot show a correlation. Keep")
        print("  soaking with --hard and re-run this; it will say when it is")
        print("  ready rather than guessing.")
        return 0

    print()
    print(f"  Comparable: {', '.join(sorted(comparable))}")
    print("  Read the two shapes separately -- a cold circuit and a mid-answer")
    print("  silence have different causes, so a correlation in one says")
    print("  nothing about the other.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
