"""Measure the upload: how long a 4.5 MB body actually takes to push."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import mitm  # noqa: E402

MODEL = "muse-spark-1.3-contributor-free"
HOST = "opencode.ai"
PATH = "/zen/v1/responses"

#: what his own calls carry
DEFAULT_BYTES = 4_500_000


def big_body(target: int) -> bytes:
    """A JSON request body of at least `target` bytes."""
    filler = "x" * 4096
    piece = ('{"role":"user","content":[{"type":"input_text","text":"'
             + filler + '"}]}')
    n = max(1, target // (len(piece) + 1))
    inner = ",".join([piece] * n)
    return ('{"model":"' + MODEL + '","stream":true,"input":['
            + inner + ']}').encode()


class Discard:
    """A client that throws the response away -- the timings are the point."""

    def __init__(self):
        self.got = 0

    def sendall(self, data):
        self.got += len(data)

    def settimeout(self, t):
        return None


class Sink:
    """Collects the callends."""

    def __init__(self):
        self.events = []

    def __call__(self, ev):
        self.events.append(ev)

    @property
    def ends(self):
        return [e for e in self.events if e.get("type") == "callend"]


def parse_args():
    """`[calls] [bytes] [--concurrency N] [--lanes K]`."""
    argv = sys.argv[1:]
    pos, conc, nl = [], 1, 0
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--concurrency":
            conc = max(1, int(argv[i + 1])); i += 2
        elif a == "--lanes":
            nl = max(1, int(argv[i + 1])); i += 2
        elif a.startswith("--"):
            i += 1
        else:
            pos.append(a); i += 1
    calls = int(pos[0]) if pos else 6
    nbytes = int(pos[1]) if len(pos) > 1 else DEFAULT_BYTES
    return calls, nbytes, conc, nl


def one(i, lane, body, relay):
    """A single upload."""
    sink = Sink()
    t0 = time.time()
    mitm._roundtrip(
        Discard(), lane, HOST, 443, "POST", PATH,
        # `host` and `user-agent` are BOTH required. `_roundtrip`
        {"host": HOST, "accept": "text/event-stream",
         "user-agent": "opencode/1.0"},
        body, sink, 900 + i, 1, t0, relay,
        # never demolish a lane for a probe
        charge_timeout=False)
    return sink.ends[-1] if sink.ends else {}


def show(n, end):
    print(f"  #{n} lane {end.get('lane')}/{end.get('cc')}  "
          f"send_s={end.get('send_s')}  fb={end.get('first_byte_s')}  "
          f"secs={end.get('secs')}  status={end.get('status')}  "
          f"reused={end.get('reused')}  err={end.get('err')!r}", flush=True)


def main():
    calls, nbytes, conc, nl = parse_args()

    from tools.soak.live_soak import boot  # noqa: E402

    mgr, daemon, relay, _port = boot()
    try:
        body = big_body(nbytes)
        shape = (f"calls={calls}  concurrency={conc}"
                 + (f"  pinned to {nl} lanes" if nl else ""))
        print(f"[upload] body={len(body)} bytes  {shape}  "
              f"send window={mitm._SEND_TIMEOUT}s", flush=True)

        lanes = mgr.healthy_lanes()
        if nl:
            lanes = lanes[:nl]
        print(f"[upload] lanes: {[l.index for l in lanes]}", flush=True)

        rows = []
        if conc == 1:
            for i in range(calls):
                end = one(i, lanes[i % len(lanes)], body, relay)
                rows.append(end)
                show(i + 1, end)
        else:
            # concurrency IS the production shape: `handle_conn` already runs
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=conc) as ex:
                futs = {ex.submit(one, i, lanes[i % len(lanes)], body, relay): i
                        for i in range(calls)}
                for f in as_completed(futs):
                    try:
                        end = f.result()
                    except Exception as e:  # noqa: BLE001 - report, not crash
                        end = {"err": f"{type(e).__name__}: {e}"}
                    rows.append(end)
                    show(futs[f] + 1, end)

        print("\n=== the upload ===")
        sents = sorted(r.get("send_s") or 0 for r in rows)
        med = sents[len(sents) // 2]
        print(f"  send_s   : min={sents[0]}  median={med}  max={sents[-1]}"
              f"  (n={len(sents)})")
        print(f"  statuses : {[r.get('status') for r in rows]}")
        print(f"  errors   : {[r.get('err') for r in rows]}")
        print()
        # A send that returns in under a second
        took = [r for r in rows if r.get("status")]
        if not took:
            print("  VOID. Not one call got a status: the far end closed before")
            print("  reading the body (the free-tier gate fires on the headers).")
            print("  send_s here is the local buffer, not Tor -- this tool cannot")
            print("  measure the upload, and any conclusion drawn from it would")
            print("  be invented. Use the log instead: on his 150 clean 200s,")
            print("  max_wait_s is p50 7.1s / p90 21.7s / max 257.5s, and every")
            print("  value above 30 is a READ, because a send would be cut at 30.")
        else:
            print(f"  {len(took)} of {len(rows)} calls got a status, so the far")
            print(f"  end did take those bodies. median send_s={med}s at")
            print(f"  {len(body) / 1024 / max(med, 0.001):.0f} KB/s.")
            over = [s for s in sents if s > mitm._SEND_TIMEOUT]
            print(f"  over the shipped {mitm._SEND_TIMEOUT:.0f}s window: "
                  f"{len(over)} of {len(sents)}")
            old = [s for s in sents if s > 30]
            # The old wall is the comparison that matters:
            band = [s for s in sents if 20 <= s <= 30]
            print(f"  over the OLD 30s wall : {len(old)} of {len(sents)}   "
                  f"({len(band)} of them in the 20-30s band)")
    finally:
        daemon.stop()
        relay.stop()
        mgr.stop_all()


if __name__ == "__main__":
    main()
