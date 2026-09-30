"""Where the first-byte latency goes: tunnel stages, measured separately."""
import socket
import ssl
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lingling import netutil
from lingling.cli import DATA_DIR, load_countries
from lingling.lanes import TorManager

HOST = "opencode.ai"
ROUNDS = 3


def stage_times(port):
    """(connect_ms, socks_ms, tls_ms) for one handshake through a lane."""
    t0 = time.monotonic()
    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
    t1 = time.monotonic()
    err = netutil.socks5_open(sock, HOST, 443)
    if err:
        sock.close()
        raise ConnectionError(err)
    t2 = time.monotonic()
    tls = ssl.create_default_context().wrap_socket(sock, server_hostname=HOST)
    t3 = time.monotonic()
    tls.close()
    return ((t1 - t0) * 1000, (t2 - t1) * 1000, (t3 - t2) * 1000)


def main():
    countries, fallback, preferred = load_countries()
    mgr = TorManager(DATA_DIR, count=6, exit_countries=countries,
                     fallback_countries=fallback,
                     preferred_countries=preferred, log=lambda *a: None)
    err = mgr.setup_lanes()
    assert not err, err
    mgr.start_all()

    print("[boot] waiting for SOCKS ports", flush=True)
    deadline = time.time() + 420
    while time.time() < deadline:
        ready = [l for l in mgr.lanes
                 if netutil.port_is_open("127.0.0.1", l.socks_port)]
        if len(ready) == len(mgr.lanes):
            break
        time.sleep(3)
    print(f"[boot] {len(ready)}/{len(mgr.lanes)} lanes listening", flush=True)

    rows = []
    print(f"\n{'lane':>4} {'cc':>4} {'round':>5} {'connect':>8} {'socks':>8} "
          f"{'tls':>8} {'total':>8}", flush=True)
    for lane in mgr.lanes:
        for r in range(ROUNDS):
            try:
                c, s, t = stage_times(lane.socks_port)
            except Exception as exc:  # noqa: BLE001
                print(f"{lane.index:>4} {lane.exit_country:>4} {r:>5}   "
                      f"FAILED {type(exc).__name__}: {exc}", flush=True)
                continue
            rows.append((lane.index, lane.exit_country, r, c, s, t))
            print(f"{lane.index:>4} {lane.exit_country:>4} {r:>5} "
                  f"{c:>7.0f}ms {s:>7.0f}ms {t:>7.0f}ms {c + s + t:>7.0f}ms",
                  flush=True)

    def avg(rs, i):
        return sum(r[i] for r in rs) / len(rs) if rs else 0.0

    first = [r for r in rows if r[2] == 0]
    later = [r for r in rows if r[2] > 0]
    print("\n=== stage averages ===")
    for label, rs in (("first handshake (circuit may be cold)", first),
                      ("repeat handshakes (circuit warm)", later)):
        if not rs:
            continue
        total = avg(rs, 3) + avg(rs, 4) + avg(rs, 5)
        print(f"  {label}")
        print(f"    connect (localhost) : {avg(rs, 3):7.0f}ms")
        print(f"    SOCKS5 CONNECT      : {avg(rs, 4):7.0f}ms")
        print(f"    TLS handshake       : {avg(rs, 5):7.0f}ms")
        print(f"    TOTAL               : {total:7.0f}ms   (n={len(rs)})")

    if first and later:
        cold = avg(first, 3) + avg(first, 4) + avg(first, 5)
        warm = avg(later, 3) + avg(later, 4) + avg(later, 5)
        print(f"\n  cold -> warm saves {cold - warm:.0f}ms "
              f"({100 * (cold - warm) / max(cold, 1):.0f}%)")
        print("  the warm total is what a pooled tunnel avoids entirely")

    mgr.stop_all()
    print("\n[stop] lanes down", flush=True)


if __name__ == "__main__":
    main()
