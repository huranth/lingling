"""How long does a pooled tunnel actually live?"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import mitm  # noqa: E402
from lingling.cli import DATA_DIR, load_countries  # noqa: E402
from lingling.lanes import TorManager  # noqa: E402
from lingling.health import (PROBE_MODEL, PROBE_PATH, UPSTREAM_UA,  # noqa: E402
                             _scan_body)

HOST = "opencode.ai"


class Discard:
    def sendall(self, data):
        return None

    def settimeout(self, t):
        return None


class Sink:
    def __init__(self):
        self.events = []

    def __call__(self, ev):
        self.events.append(ev)

    @property
    def ends(self):
        return [e for e in self.events if e.get("type") == "callend"]


def main():
    limit = float(sys.argv[1]) if len(sys.argv) > 1 else 300.0
    step = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0

    countries, fallback, preferred = load_countries()
    mgr = TorManager(DATA_DIR, count=1, exit_countries=countries,
                     fallback_countries=fallback,
                     preferred_countries=preferred, log=lambda *a: None)
    try:
        err = mgr.setup_lanes()
        if err:
            print(f"setup_lanes: {err}")
            return 1
        mgr.start_all()
        lane = mgr.lanes[0]
        for _ in range(60):
            if lane.running():
                break
            time.sleep(1)
        print(f"lane 1: {lane.exit_country} {lane.exit_ip}")

        pool = mitm.TunnelPool()
        relay = type("R", (), {"tor": mgr, "tunnels": pool})()
        sink = Sink()
        mitm._roundtrip(
            Discard(), lane, HOST, 443, "POST", PROBE_PATH,
            # Both of these are REQUIRED, and omitting either
            {"host": HOST, "content-type": "application/json",
             "user-agent": UPSTREAM_UA},
            _scan_body(PROBE_MODEL, PROBE_PATH),
            sink, 1, 1, time.time(), relay, charge_timeout=False)
        end = sink.ends[-1] if sink.ends else {}
        print(f"priming call: status={end.get('status')} "
              f"peer_close={end.get('peer_close')} cut={end.get('cut')} "
              f"err={end.get('err')!r}")

        idle = pool._idle.get(lane.index) or []
        print(f"pooled tunnels for lane 1: {len(idle)}")
        if not idle:
            print("\nNOTHING WAS POOLED, so there is nothing to measure. The")
            print("far end must have asked to close, or the response was not")
            print("framed. Raise `peer_close` on the priming call above.")
            return 1

        print(f"\nasking for it back every {step:.0f}s, up to {limit:.0f}s")
        t0 = time.monotonic()
        while True:
            time.sleep(step)
            elapsed = time.monotonic() - t0
            got = pool.take(lane)
            if got is None:
                print(f"  {elapsed:6.0f}s  CLOSED by the far end")
                print(f"\n=== the far end drops an idle tunnel at ~{elapsed:.0f}s ===")
                print(f"  _KEEPALIVE_S is {mitm._KEEPALIVE_S:.0f}s")
                if elapsed < mitm._KEEPALIVE_S:
                    print("  The TTL is NOT the binding constraint -- the peer")
                    print("  hangs up first, so raising the TTL buys nothing")
                    print("  and the pool can only ever reuse a tunnel younger")
                    print(f"  than about {elapsed:.0f}s.")
                else:
                    print("  The TTL expires first, so it is what governs reuse.")
                return 0
            print(f"  {elapsed:6.0f}s  alive")
            pool.give(lane, *got)
            if elapsed >= limit:
                print(f"\n=== still usable at {limit:.0f}s ===")
                print(f"  The peer outlived the observation window, so")
                print(f"  _KEEPALIVE_S={mitm._KEEPALIVE_S:.0f}s is the binding")
                print(f"  constraint and the TTL is what governs reuse.")
                return 0
    finally:
        mgr.stop_all()
        print("\n[stop] lane down")


if __name__ == "__main__":
    sys.exit(main())
