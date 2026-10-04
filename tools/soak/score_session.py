"""Score every session in proof.log: attempts, calls, failures by class."""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
try:
    from lingling import mitm  # noqa: E402
    SEND_WINDOW = mitm._SEND_TIMEOUT
except Exception:  # noqa: BLE001
    SEND_WINDOW = 0.0

from lingling import data_dir  # noqa: E402

DEFAULT = data_dir() / "proof.log"


def load(path: pathlib.Path) -> list:
    """Every JSON line in the log, in order."""
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return [e for e in out if "t" in e]


def sessions(evs: list) -> list:
    """Slice the log by `start`: each event belongs to the last start before it."""
    marks = [e for e in evs if e.get("type") == "start"]
    spans = []
    for i, s in enumerate(marks):
        end = marks[i + 1]["t"] if i + 1 < len(marks) else float("inf")
        spans.append((s, end))
    return spans


def requests(evs: list, lo: float, hi: float) -> dict:
    """Group callends into requests."""
    host = {}
    for e in evs:
        if e.get("type") == "call" and lo <= e["t"] < hi:
            host[(e["n"], e["c"], e["lane"])] = e.get("host", "?")
    groups = collections.defaultdict(list)
    for e in evs:
        if e.get("type") != "callend" or not (lo <= e["t"] < hi):
            continue
        e["_host"] = host.get((e["n"], e["c"], e["lane"]), "?")
        groups[(e["n"], e["c"])].append(e)
    return groups


def label(e: dict) -> str:
    """The one honest name for how an attempt ended."""
    return str(e["status"]) if e.get("err") == "" else str(e.get("err"))


#: the two errors that mean the DIAL failed, not the request
DIAL = ("timed out", "ConnectionRefusedError")


def is_dial(e: dict) -> bool:
    """True when the attempt never got a stream -- the circuit build failed."""
    return (e.get("err") or "").split(":")[-1].strip() in DIAL


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=str(DEFAULT))
    ap.add_argument("--session", help="show one session in full")
    args = ap.parse_args()
    evs = load(pathlib.Path(args.log))
    spans = sessions(evs)

    if args.session:
        for s, end in spans:
            if s.get("session") != args.session:
                continue
            show(s, evs, end)
        return 0

    print(f"{'session':<13}{'ver':<12}{'att':<6}{'attOK':<7}"
          f"{'calls':<7}{'callOK':<8}{'reuse%':<8}{'sendmax':<9}"
          f"{'win*':<6}{'zero-t.o':<10}{'dial':<6}failures")
    print("-" * 124)
    for s, end in spans:
        groups = requests(evs, s["t"], end)
        if not groups:
            continue
        att = [a for v in groups.values() for a in v]
        ok = sum(1 for a in att if a.get("status") == 200)
        fin = [v[-1] for v in groups.values()]
        fok = sum(1 for a in fin if a.get("status") == 200 and a.get("err") == "")
        zero = sum(1 for a in att
                   if a.get("err") == "TimeoutError" and a.get("kb") == 0)
        bad = collections.Counter(label(a) for a in fin
                                  if label(a) != "200")
        # reuse and the send window: the two numbers
        ru = sum(1 for a in att if a.get("reused"))
        smax = max((a.get("send_s") or 0) for a in att) if att else 0
        dial = sum(1 for a in att if is_dial(a))
        tag = time.strftime("%m-%d %H:%M", time.localtime(s["t"]))
        worst = "  ".join(f"{k}={v}" for k, v in bad.most_common(4))
        print(f"{tag:<13}{str(s.get('version'))[:11]:<12}{len(att):<6}"
              f"{100 * ok / len(att):<7.1f}{len(groups):<7}"
              f"{100 * fok / len(groups):<8.1f}{100 * ru / len(att):<8.0f}"
              f"{smax:<9.1f}{SEND_WINDOW:<6.0f}{zero:<10}{dial:<6}{worst}")
    if SEND_WINDOW:
        print()
        print(f"sendmax = the largest `send_s` in that session.")
        print(f"win*    = TODAY'S shipped _SEND_TIMEOUT ({SEND_WINDOW:.0f}s). "
              f"It is NOT the window that session ran with --")
        print("          the value has been 20, 30, 300 and 120 over this "
              "log, so a sendmax of 30.0 in an")
        print("          older row means that session was AT its ceiling, "
              "not comfortably under this one.")
        print("A window is only marginal when sendmax APPROACHES it. Do not "
              "judge it by max_wait_s --")
        print("that covers the reads too and reaches 257s on this log, which "
              "is what argued a correct")
        print("fix back down to the value that was cutting uploads.")
        print()
        print("dial = attempts whose DIAL failed ('timed out' / refused). "
              "These are invisible to the")
        print("country scorer, which counts only 200 and 429 -- so a country "
              "failing 8.7% of its dials")
        print("looks identical to one failing 0.2%. Measured: nl 59/679=8.7%, "
              "se 27/698=3.9%,")
        print("lu 11/905=1.2%, de 4/833=0.5%, at 2/607=0.3%, us 1/456=0.2%.")
    return 0


def show(s: dict, evs: list, end: float) -> None:
    """One session, request by request, with every attempt underneath."""
    groups = requests(evs, s["t"], end)
    att = [a for v in groups.values() for a in v]
    ok = sum(1 for a in att if a.get("status") == 200)
    fin = [v[-1] for v in groups.values()]
    fok = sum(1 for a in fin if a.get("status") == 200 and a.get("err") == "")
    print(f"session {s['session']}  {s.get('version')}  "
          f"{s.get('lanes')} lanes  {s.get('countries')}")
    print(f"  started {time.strftime('%m-%d %H:%M:%S', time.localtime(s['t']))}")
    print(f"  attempts {len(att)}  ok {ok} = {100 * ok / len(att):.1f}%")
    print(f"  calls    {len(groups)}  ok {fok} = "
          f"{100 * fok / len(groups):.1f}%")
    print()
    for lane in sorted({a["lane"] for a in att}):
        la = [a for a in att if a["lane"] == lane]
        lok = sum(1 for a in la if a.get("status") == 200)
        lto = [a for a in la if a.get("err") == "TimeoutError"]
        lz = sum(1 for a in lto if a.get("kb") == 0)
        ld = sum(1 for a in la if is_dial(a))
        lru = sum(1 for a in la if a.get("reused"))
        print(f"  lane {lane}: att={len(la):<4} ok={lok:<4} "
              f"timeouts={len(lto)} (zero-byte {lz})  dial={ld}  "
              f"reuse={lru}/{len(la)}")
    print()
    print("  every request whose final attempt was not a 200:")
    for k in sorted(groups, key=lambda k: groups[k][-1]["t"]):
        v = groups[k]
        if v[-1].get("status") == 200 and v[-1].get("err") == "":
            continue
        print(f"    n={k[0]} c={k[1]}  host={v[-1].get('_host')}")
        for a in v:
            # `client_kb` is the field that answers whether a
            extra = ""
            if a.get("reused"):
                extra += " reused"
            if a.get("send_s"):
                extra += f" send_s={a['send_s']}"
            if a.get("peer_close"):
                extra += " peer_close"
            if a.get("client_kb") is not None:
                extra += f" client_kb={a['client_kb']}"
            print(f"      {time.strftime('%H:%M:%S', time.localtime(a['t']))} "
                  f"{label(a):<14} lane={a['lane']} {a['cc']:<3} "
                  f"kb={a['kb']:<7} secs={a['secs']:<7} "
                  f"first_byte={a.get('first_byte_s')}{extra}")


if __name__ == "__main__":
    sys.exit(main())
