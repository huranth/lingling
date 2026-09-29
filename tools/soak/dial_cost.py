"""What dial failures cost, and whether a lane that dial-fails is really sick."""
import collections
import json
import pathlib

LOG = pathlib.Path(r"C:/Users/W/AppData/Local/lingling/proof.log")

#: `socks5_open` returns the string; the exceptions are something else
DIAL = ("timed out", "ConnectionRefusedError")


def load():
    recs = []
    for line in LOG.open(encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            recs.append(r)
    return recs


def pair(recs):
    """Attempts in order, each callend attached to its own call."""
    sess, seq, pending, t0s = None, collections.defaultdict(list), {}, {}
    for r in recs:
        k = r.get("type")
        if k == "start":
            sess = r.get("session")
            pending = {}
            t0s[sess] = r.get("t", 0)
            continue
        if k == "call":
            att = {"t": r.get("t", 0), "cc": r.get("cc") or "??",
                   "ip": r.get("ip"), "lane": None, "end": None}
            seq[sess].append(att)
            pending.setdefault((r.get("n"), r.get("c")), []).append(att)
        elif k == "callend":
            slot = pending.get((r.get("n"), r.get("c"))) or []
            att = slot.pop(0) if slot else (seq[sess][-1] if seq[sess] else None)
            if att is not None:
                att["end"] = r
                att["lane"] = r.get("lane")
    return seq, t0s


def err_of(a):
    return ((a["end"] or {}).get("err") or "").split(":")[-1].strip()


def ok(a):
    e = a["end"] or {}
    return e.get("status") == 200 and not e.get("err")


def main():
    seq, t0s = pair(load())
    attempts = [a for s in seq for a in seq[s] if a["end"]]
    print(f"paired attempts: {len(attempts)}\n")

    print("=== 1. what dial failures cost, by country ===")
    calls, dial, secs = (collections.Counter() for _ in range(3))
    ipfail, ipall = collections.Counter(), collections.Counter()
    for a in attempts:
        calls[a["cc"]] += 1
        if a["ip"]:
            ipall[(a["cc"], a["ip"])] += 1
        if err_of(a) in DIAL:
            dial[a["cc"]] += 1
            secs[a["cc"]] += (a["end"] or {}).get("secs") or 0
            if a["ip"]:
                ipfail[(a["cc"], a["ip"])] += 1
    print(f"{'cc':>4}{'calls':>8}{'dial':>7}{'rate':>8}{'wall lost':>12}")
    for cc in sorted(calls, key=lambda x: -dial[x]):
        if not dial[cc]:
            continue
        print(f"{cc:>4}{calls[cc]:>8}{dial[cc]:>7}"
              f"{100 * dial[cc] / max(1, calls[cc]):>7.1f}%"
              f"{secs[cc] / 60:>10.1f}m")
    print(f"  total {sum(dial.values())} dial failures, "
          f"{sum(secs.values()) / 60:.1f} min of wall")
    print("  worst exits:")
    for (cc, ip), n in ipfail.most_common(5):
        print(f"    {cc:>3} {str(ip):>16}  {n}/{ipall[(cc, ip)]} failed")

    print("\n=== 2. is the lane sick, or just unlucky? ===")
    base = sum(1 for a in attempts if ok(a))
    print(f"  baseline success                 : "
          f"{100 * base / max(1, len(attempts)):.1f}%")
    same = diff = collections.Counter()
    for s, lst in seq.items():
        lst.sort(key=lambda a: a["t"])
        for i, a in enumerate(lst):
            if not a["end"] or err_of(a) not in DIAL or i + 1 >= len(lst):
                continue
            nxt = lst[i + 1]
            if not nxt["end"]:
                continue
            same[nxt["lane"] == a["lane"]] += 1
            if nxt["lane"] != a["lane"]:
                diff["ok"] += ok(nxt)
    print(f"  immediate retry, same lane       : {same[True]} of "
          f"{same[True] + same[False]}  (the retry always goes elsewhere)")
    if same[False]:
        print(f"  immediate retry, another lane    : "
              f"{100 * diff['ok'] / same[False]:.1f}%")

    later, ctrl, why, ages = [], [], collections.Counter(), []
    for s, lst in seq.items():
        lst.sort(key=lambda a: a["t"])
        t0 = t0s.get(s, min((a["t"] for a in lst), default=0))
        for i, a in enumerate(lst):
            nxt = next((x for x in lst[i + 1:] if x["lane"] == a["lane"]), None)
            if not nxt or not nxt["end"]:
                continue
            if err_of(a) in DIAL:
                later.append(ok(nxt))
                e = err_of(nxt)
                why["dial failure again" if e in DIAL else
                    (f"clean 200" if ok(nxt) else (e or f"status {nxt.get('status')}"))] += 1
                if e in DIAL:
                    ages.append((a["t"] - t0) / 60.0)
            elif ok(a):
                ctrl.append(ok(nxt))
    if later:
        print(f"  next use of the SAME lane, later : "
              f"{100 * sum(later) / len(later):.1f}%  (n={len(later)})")
    if ctrl:
        print(f"  control, next use after a 200    : "
              f"{100 * sum(ctrl) / len(ctrl):.1f}%  (n={len(ctrl)})")
    print("  what that lane does next:")
    for k, v in why.most_common(4):
        print(f"    {k:<26} {v}")

    if ages:
        ages.sort()
        print("\n=== 3. when do the repeats happen? (boot vs unreachable) ===")
        print(f"  age into the session: min={ages[0]:.1f}m  "
              f"median={ages[len(ages) // 2]:.1f}m  max={ages[-1]:.1f}m")
        b = collections.Counter()
        for a in ages:
            b["<2m" if a < 2 else "2-10m" if a < 10 else ">10m"] += 1
        for k in ("<2m", "2-10m", ">10m"):
            if b[k]:
                print(f"    {k:<7} {b[k]:>3}  ({100 * b[k] / len(ages):.0f}%)")
        print("  The early share is a lane still coming up -- retiring THOSE is")
        print("  inventing a failure state for a healthy lane. Gate any tally on")
        print("  the lane not still booting (`healing` already tells you).")

    print("\n=== 4. would a tally ever FIRE? ===")
    # Even if he decides to count dial failures,
    per_lane = collections.defaultdict(list)
    for s, lst in seq.items():
        for a in sorted(lst, key=lambda x: x["t"]):
            per_lane[(s, a["lane"])].append(a)
    for label, pred in (("dial failures ('timed out'/refused)",
                         lambda a: err_of(a) in DIAL),
                        ("what the tally counts today (TimeoutError)",
                         lambda a: err_of(a).endswith("TimeoutError"))):
        runs = collections.Counter()
        caught = 0.0
        for lst in per_lane.values():
            run = 0
            for a in lst:
                if pred(a):
                    run += 1
                    if run >= 3:          # only from the 3rd onward is it "saved"
                        caught += (a["end"] or {}).get("secs") or 0
                else:
                    if run:
                        runs[run] += 1
                    run = 0
            if run:
                runs[run] += 1
        tot = sum(runs.values())
        print(f"  {label}")
        if not tot:
            print("    no runs at all")
            continue
        print("    consecutive runs by length: "
              + ", ".join(f"{k} in a row x{v}" for k, v in sorted(runs.items())))
        print(f"    runs reaching 3 (the threshold): "
              f"{sum(v for k, v in runs.items() if k >= 3)} of {tot}")
        print(f"    wall that retiring at 3 would have reclaimed: "
              f"{caught / 60:.1f}m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
