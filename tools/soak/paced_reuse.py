"""Reuse at a given cadence -- the thing the soak could not reproduce."""
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

        # the first visit to a lane is COLD
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
