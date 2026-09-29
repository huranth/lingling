"""Measure the upload: how long a 4.5 MB body actually takes to push.

    python tools/soak/upload_probe.py [calls] [bytes] [--concurrency N] [--lanes K]

Why this exists. `max_wait_s` is not one quantity: for the send it wraps the
WHOLE `sendall` in a single call, while for the body it wraps ONE chunk. So it
is neither a per-write gap nor a total, and reading it as either has already
produced one wrong conclusion.

It works now, and the first working run corrected two earlier claims.

**It DOES measure the upload.** A 4.5 MB body takes ~3.5s on an idle lane
(`send_s` 2.8-4.1s over 5 calls, ~1255 KB/s) and the far end answers 403 --
which means it READ the body, because the free-tier gate runs after the
request is parsed. So the earlier reading, "the gate closes a hand-rolled
request before the body is read", was wrong. It was my own malformed request:
`_roundtrip` rebuilds the head from the headers the caller passes and does NOT
invent a `Host`, so without one the far end answers **400 with
`connection: close`** and the send goes nowhere. `host` and `user-agent` are
both required -- see the call below.

**And it does not by itself justify the window.** 3.5s on an idle lane is not
near any ceiling. What justifies `_SEND_TIMEOUT` = 120s is the owner's OWN
loaded traffic, where over 107 successful >=1MB uploads `send_s` runs
p50 10.8s, p90 20.8s and **max 29.8s against a 30s wall, with zero above
it** -- 13 of 107 inside 10s of the limit, and 10 uploads cut. This tool
measures the optimistic case; the log measures his.

**The loaded mode measures the middle.** Sequential calls see idle lanes
(p50 3.5s here); his real traffic shares each lane with other sessions. To
reproduce that load on THIS machine:

    python tools/soak/upload_probe.py 8 4500000 --concurrency 4 --lanes 2

That pushes 4 uploads at once onto 2 lanes, so each lane carries two
simultaneous 4.5 MB bodies. Concurrency is the production shape -- `_roundtrip`
already runs one per connection thread in `handle_conn` -- and each call takes
its OWN Sink, because the sequential loop's `sink.ends[-1]` is another call's
end the moment two run at once.

**What the loaded mode measured (2026-09-22).** `--concurrency 6 --lanes 1`:
12 calls of 4.5 MB, six in flight the whole time, every one answered 403 so
the bodies really went up the wire.

    send_s   min 1.8   median 7.5   max 21.5   (n=12)
    3 of 12 in the 20-30s band

Per-upload throughput fell from ~1255 KB/s idle to ~209 KB/s at six-way load,
so load is worth roughly 6x on the send. **21.5s is 72% of the OLD 30s wall
with bodies 20% smaller than his current 5.4 MB median** -- that reproduces
the shape of his log (13 of 107 inside 10s of the limit) on this machine, and
it is why 30s was marginal rather than merely cautious.

**And it killed a hypothesis of mine.** A two-lane run had looked like "a cold
circuit costs 3x a reused tunnel": its two fast sends (4.4s, 6.5s) were both
`reused=True`. At matched concurrency they are not -- the warm wave spans
4.9-21.5s and the cold wave 1.8-10.3s, so `reused` does not predict the send
at all. The early reading was unequal load wearing a reuse mask: those fast
reused calls were also the ones running while fewer uploads were in flight.
Two waves at the same `--concurrency` is what separates them; unequal load
does not.
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
    """`[calls] [bytes] [--concurrency N] [--lanes K]`.

    --concurrency pushes that many uploads at once; --lanes pins them to the
    first K healthy lanes, so each lane carries N/K simultaneous bodies.
    Defaults keep the old sequential behaviour exactly.
    """
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
    """A single upload. Its OWN Sink: under threads, the sequential loop's
    `sink.ends[-1]` can be another call's end -- that read was only correct
    because nothing else was running."""
    sink = Sink()
    t0 = time.time()
    mitm._roundtrip(
        Discard(), lane, HOST, 443, "POST", PATH,
        # `host` and `user-agent` are BOTH required. `_roundtrip`
        # rebuilds the head from whatever the caller passes and does NOT
        # invent a `Host`; without one the far end answers 400 with
        # `connection: close`, and the first version of this tool read
        # that as "the gate refused the body" when it was really its
        # own malformed request. Do not remove either.
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
            # one `_roundtrip` per connection thread.
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
        # A send that returns in under a second for 4.5 MB is NOT Tor moving
        # 4.5 MB -- that is the kernel send buffer absorbing it locally. If the
        # far end never answered (status 0) the body was never read, and the
        # number says nothing about how long an upload takes. The first
        # version of this tool printed the opposite conclusion from exactly
        # this data, which is why the branch is now keyed on the evidence.
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
            # The old wall is the comparison that matters: his log ran against
            # 30s and piled up under it (13 of 107 in the 20-30s band). Load
            # is what moves an idle 3.5s send toward it, so a loaded run that
            # stays clear of 30 is the real answer, and a loaded run that
            # crosses it is the proof the old value was cutting uploads.
            band = [s for s in sents if 20 <= s <= 30]
            print(f"  over the OLD 30s wall : {len(old)} of {len(sents)}   "
                  f"({len(band)} of them in the 20-30s band)")
    finally:
        daemon.stop()
        relay.stop()
        mgr.stop_all()


if __name__ == "__main__":
    main()
