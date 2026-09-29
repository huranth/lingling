"""Reuse at a given cadence -- the thing the soak could not reproduce.

    python tools/soak/paced_reuse.py [calls] [gap_s]

Why this exists. `_KEEPALIVE_S` went 30s -> 600s because his requests arrive
STRICTLY SEQUENTIALLY, round-robin, so two requests reach the same lane one
full lap apart -- median 70.2s, p75 150s, and in the 34-call session at 0%
reuse the MINIMUM was 98.8s. Against a 30s TTL no tunnel can survive, so every
request paid the dial. 600s was justified by ARITHMETIC over his log, not by a
run at his cadence.

The soak could not provide that run: at `SOAK_CONCURRENCY=1` it still fires
requests back-to-back, which is why it reported 55-94% reuse while his own
sessions sat at 0%. This tool takes the gap as an argument, so the lap can be
set to his:

    lane lap  = gap x lanes      (5 lanes, 14s gap -> a 70s lap, his median)

It costs NO quota. The far end answers 403 to these (the free-tier gate fires
on the client, after parsing), and a 403 spends nothing -- which is also why
`send_s` here is meaningful: the body was read. Do not turn this into a
200-seeking tool; that would spend his free tier to measure a TTL.

Usage note: gap is the wait BEFORE each call after the first, so total wall is
roughly `calls x gap` plus boot.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import mitm  # noqa: E402

MODEL = "muse-spark-1.3-contributor-free"
HOST = "opencode.ai"
PATH = "/zen/v1/responses"

#: tiny on purpose -- the question is the tunnel, not the upload
BODY = ('{"model":"' + MODEL + '","stream":true,"input":[{"role":"user",'
        '"content":[{"type":"input_text","text":"hi"}]}]}').encode()

#: `host` AND `user-agent` are both required: `_roundtrip` rebuilds the head
#: from the caller's headers and does NOT invent a `Host`, and without one the
#: far end answers 400 `connection: close` and nothing pools.
HEADERS = {"host": HOST, "accept": "text/event-stream",
           "user-agent": "opencode/1.0"}


class Discard:
    def sendall(self, data):
        return None

    def settimeout(self, t):
        return None


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    calls = int(argv[0]) if argv else 10
    gap = float(argv[1]) if len(argv) > 1 else 14.0

    from tools.soak.live_soak import boot  # noqa: E402

    mgr, daemon, relay, _port = boot()
    try:
        lanes = mgr.healthy_lanes()
        lap = gap * len(lanes)
        print(f"[paced] calls={calls}  gap={gap}s  lanes={len(lanes)}  "
              f"=> same-lane lap ~{lap:.0f}s   shipped TTL={mitm._KEEPALIVE_S}s",
              flush=True)
        print(f"[paced] lanes: {[l.index for l in lanes]}", flush=True)

        rows = []
        for i in range(calls):
            if i:
                time.sleep(gap)
            lane = lanes[i % len(lanes)]
            events = []
            mitm._roundtrip(Discard(), lane, HOST, 443, "POST", PATH, HEADERS,
                            BODY, events.append, 900 + i, 1, time.time(), relay,
                            # never demolish a lane for a probe
                            charge_timeout=False)
            end = [e for e in events if e.get("type") == "callend"]
            end = end[-1] if end else {}
            rows.append(end)
            print(f"  #{i + 1:<3} lane {end.get('lane')}/{end.get('cc')}  "
                  f"reused={str(end.get('reused')):<5} status={end.get('status')} "
                  f"send_s={end.get('send_s')} err={end.get('err')!r}", flush=True)

        done = [r for r in rows if r.get("status")]
        reu = sum(1 for r in rows if r.get("reused"))

        # the first visit to a lane is COLD by definition, so `reused/calls`
        # is capped at (calls - lanes)/calls and saturates -- with 10 calls over
        # 6 lanes it cannot exceed 40% no matter how well the pool works. The
        # number that means anything is reuse among the calls that COULD reuse.
        seen, possible, hit = set(), [], 0
        for r in rows:
            lane = r.get("lane")
            if lane in seen:
                possible.append(r)
                hit += bool(r.get("reused"))
            else:
                seen.add(lane)

        print(f"\n=== reuse at a ~{lap:.0f}s lap ===")
        print(f"  answered : {len(done)}/{len(rows)}  "
              f"(403 = the gate read the body and spent no quota)")
        print(f"  reused   : {reu}/{len(rows)} = {100 * reu / max(1, len(rows)):.0f}%"
              f"   (ceiling {(len(rows) - len(seen)) / max(1, len(rows)):.0%} -- "
              f"first visit per lane is always cold)")
        if possible:
            print(f"  of calls that COULD reuse : {hit}/{len(possible)} = "
                  f"{100 * hit / len(possible):.0f}%   <-- the real number")
        if mitm._KEEPALIVE_S < lap:
            print(f"  the TTL ({mitm._KEEPALIVE_S:.0f}s) is SHORTER than this lap "
                  f"({lap:.0f}s), so reuse must collapse to 0 -- that is the "
                  f"bug being fixed, and a nonzero number here means it is not")
        else:
            print(f"  the TTL ({mitm._KEEPALIVE_S:.0f}s) covers this lap "
                  f"({lap:.0f}s), so reuse should hold up")
        dial = [r for r in rows if (r.get("err") or "").endswith("timed out")]
        print(f"  dial failures: {len(dial)}")
    finally:
        daemon.stop()
        relay.stop()
        mgr.stop_all()


if __name__ == "__main__":
    main()
