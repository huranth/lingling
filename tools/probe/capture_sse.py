"""What a reasoning-only 200 actually contains: dump the raw SSE, byte for byte."""
import json
import socket
import ssl
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lingling import netutil
from lingling.cli import DATA_DIR, load_countries
from lingling.lanes import TorManager
from lingling.health import UPSTREAM_UA

HOST = "opencode.ai"
PATH = "/zen/v1/responses"
MODEL = "muse-spark-1.3-contributor-free"
READ_TIMEOUT = 180.0

BODY = json.dumps({
    "model": MODEL,
    "stream": True,
    "input": [{"type": "message", "role": "user",
               "content": [{"type": "input_text",
                            "text": "Count from 1 to 40, one number per line."}]}],
    "max_output_tokens": 4000,
}).encode()

OUT = Path(__file__).resolve().parent / "sse-capture"


def open_lane(port):
    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
    err = netutil.socks5_open(sock, HOST, 443)
    if err:
        sock.close()
        raise ConnectionError(err)
    return ssl.create_default_context().wrap_socket(sock, server_hostname=HOST)


def read_head(f):
    buf = b""
    while b"\r\n\r\n" not in buf:
        ch = f.read(1)
        if not ch:
            return None
        buf += ch
    return buf


def one_call(port, tag):
    t0 = time.time()
    up = open_lane(port)
    f = up.makefile("rb")
    head = (f"POST {PATH} HTTP/1.1\r\nhost: {HOST}\r\n"
            f"user-agent: {UPSTREAM_UA}\r\n"
            "content-type: application/json\r\n"
            f"content-length: {len(BODY)}\r\n"
            "connection: close\r\n\r\n").encode()
    up.sendall(head + BODY)
    rhead = read_head(f)
    if rhead is None:
        print(f"  [{tag}] no response head", flush=True)
        up.close()
        return None
    status = rhead.split(b" ", 2)[1].decode()
    print(f"  [{tag}] HTTP {status}", flush=True)
    if status != "200":
        # drain briefly, show what it said
        rest = b""
        up.settimeout(10)
        try:
            while len(rest) < 4000:
                ch = up.read(4096)
                if not ch:
                    break
                rest += ch
        except OSError:
            pass
        print(f"  [{tag}] body: {rest[:600]!r}", flush=True)
        up.close()
        return None

    chunked = b"chunked" in rhead.lower()
    up.settimeout(READ_TIMEOUT)
    evs = []          # logged chunks
    raw = b""         # full reassembled body
    total = 0
    while True:
        try:
            ch = up.read(4096)
        except (OSError, ssl.SSLError) as exc:
            evs.append({"t": round(time.time() - t0, 2), "err": type(exc).__name__})
            print(f"  [{tag}] READ ERROR {type(exc).__name__} after {total}B", flush=True)
            break
        if not ch:
            evs.append({"t": round(time.time() - t0, 2), "eof": True})
            print(f"  [{tag}] clean EOF after {total}B", flush=True)
            break
        total += len(ch)
        raw += ch
        evs.append({"t": round(time.time() - t0, 2), "n": len(ch)})

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{tag}.bin").write_bytes(raw)
    (OUT / f"{tag}.chunks.json").write_text(json.dumps(evs, indent=1))
    print(f"  [{tag}] {total}B in {time.time() - t0:.1f}s -> {tag}.bin", flush=True)

    # which event names appeared
    names = {}
    import re
    for m in re.findall(rb'"type"\s*:\s*"([a-z_.]+)"', raw):
        k = m.decode()
        names[k] = names.get(k, 0) + 1
    print(f"  [{tag}] event types: {names}", flush=True)
    up.close()
    return {"tag": tag, "bytes": total, "names": names, "chunked": chunked}


def main():
    wanted = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    countries, fallback, preferred = load_countries()
    mgr = TorManager(DATA_DIR, count=6, exit_countries=countries,
                     fallback_countries=fallback,
                     preferred_countries=preferred, log=lambda *a: None)
    if not mgr.lanes:
        err = mgr.setup_lanes()
        assert not err, err
    mgr.start_all()
    print("[boot] waiting for SOCKS ports", flush=True)
    deadline = time.time() + 420
    ready = []
    while time.time() < deadline:
        ready = [l for l in mgr.lanes
                 if netutil.port_is_open("127.0.0.1", l.socks_port)]
        if ready:
            break
        time.sleep(3)
    print(f"[boot] {len(ready)}/{len(mgr.lanes)} lanes listening", flush=True)
    if not ready:
        print("[boot] no lane came up -- aborting", flush=True)
        return
    # every call rides the lanes that actually answered
    lanes = ready

    results = []
    for i in range(wanted):
        lane = lanes[i % len(lanes)]
        tag = f"call{i + 1:02d}-lane{lane.index}"
        print(f"[run] {tag}", flush=True)
        try:
            r = one_call(lane.socks_port, tag)
            if r:
                results.append(r)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{tag}] FAILED {type(exc).__name__}: {exc}", flush=True)
        time.sleep(2)

    print("\n=== summary ===")
    for r in results:
        print(f"  {r['tag']:>22}  {r['bytes']:>7}B  {r['names']}")
    no_text = [r for r in results
               if not any(k in r["names"] for k in
                          ("output_text.delta", "function_call"))]
    print(f"\nreasoning-only calls (no output_text): {len(no_text)}/{len(results)}")
    print(f"raw bytes in {OUT}")


if __name__ == "__main__":
    main()
