"""Ship check: does a real boot produce distinct, fresh, working exits?"""
import json
import socket
import ssl
import sys
import sysconfig
import time

#: the console script's own copy, resolved per machine
sys.path.insert(0, sysconfig.get_paths()["purelib"])

from lingling import netutil  # noqa: E402
from lingling.cli import DATA_DIR, load_countries  # noqa: E402
from lingling.lanes import USED_PATH, TorManager  # noqa: E402


def live_session() -> bool:
    """True when a lingling or opencode process is already running.

    This suite boots lanes on the real data dir, and a boot reaps the
    orphan tor processes holding lane ports. Against a live session that
    is not a reap, it is a kill -- so refuse rather than steal the ports.
    """
    import subprocess
    try:
        out = subprocess.check_output(["tasklist"], text=True, timeout=15)
    except Exception:  # noqa: BLE001
        return False
    low = out.lower()
    return "lingling.exe" in low or "opencode.exe" in low


def exit_ip(port):
    """What address one lane's traffic comes out of."""
    for host in ("api.ipify.org", "icanhazip.com"):
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=60)
            if netutil.socks5_open(sock, host, 443):
                continue
            tls = ssl.create_default_context().wrap_socket(
                sock, server_hostname=host)
            tls.sendall((f"GET / HTTP/1.1\r\nHost: {host}\r\n"
                         f"User-Agent: curl/8\r\nConnection: close\r\n\r\n"
                         ).encode())
            out = b""
            while len(out) < 32768:
                chunk = tls.recv(8192)
                if not chunk:
                    break
                out += chunk
            tls.close()
            text = out.partition(b"\r\n\r\n")[2].decode("utf-8", "replace").strip()
            if text and text[0].isdigit():
                return text.split()[0]
        except Exception:  # noqa: BLE001
            continue
    return "?"


def main():
    if live_session():
        print("a lingling/opencode session is running -- refusing to touch "
              "the lane ports. Close it and re-run.")
        return
    used_before = {}
    if (DATA_DIR / USED_PATH).exists():
        used_before = json.loads((DATA_DIR / USED_PATH).read_text(encoding="utf-8"))

    countries, fallback, preferred = load_countries()
    mgr = TorManager(DATA_DIR, count=6, exit_countries=countries,
                     fallback_countries=fallback,
                     preferred_countries=preferred, log=lambda *a: None)
    err = mgr.setup_lanes()
    assert not err, err

    pins = {l.index: (l.exit_fingerprint, l.exit_ip, l.exit_country)
            for l in mgr.lanes}
    fps = [f for f, _, _ in pins.values() if f]
    print(f"[pins] {len(fps)}/{len(mgr.lanes)} lanes pinned")
    for idx, (fp, ip, cc) in pins.items():
        print(f"  lane {idx} {cc:>3} -> {fp[:16] if fp else '(country only)':<16} {ip}")

    fresh = [i for i, (fp, _, _) in pins.items()
             if fp and used_before.get(fp, 0) <= time.time()]
    print(f"\n[1] distinct : {len(set(fps))}/{len(fps)} unique")
    print(f"[2] fresh    : {len(fresh)}/{len(fps)} were not used lately")

    mgr.start_all()
    deadline = time.time() + 420
    while time.time() < deadline:
        ready = [l for l in mgr.lanes
                 if netutil.port_is_open("127.0.0.1", l.socks_port)]
        if len(ready) == len(mgr.lanes):
            break
        time.sleep(3)
    print(f"\n[boot] {len(ready)}/{len(mgr.lanes)} lanes listening")

    print(f"\n{'lane':>4} {'cc':>3} {'pinned':>16} {'observed':>16} {'match':>6}")
    ok, seen = True, {}
    for lane in mgr.lanes:
        _fp, pinned, cc = pins[lane.index]
        got = exit_ip(lane.socks_port)
        seen[lane.index] = got
        match = "yes" if got == pinned else ("NO" if pinned else "n/a")
        if pinned and got != pinned:
            ok = False
        print(f"{lane.index:>4} {cc:>3} {pinned or '-':>16} {got:>16} {match:>6}")

    got = [v for v in seen.values() if v != "?"]
    print(f"\n[3] observed {len(got)} exits, {len(set(got))} distinct, "
          f"all matched: {ok}")
    if (DATA_DIR / USED_PATH).exists():
        after = json.loads((DATA_DIR / USED_PATH).read_text(encoding="utf-8"))
        print(f"[4] freshness memory grew {len(used_before)} -> {len(after)}")

    print("\nRESULT:", "READY" if (ok and len(set(got)) == len(got) and got)
          else "CHECK FAILED")
    mgr.stop_all()
    print("[stop] lanes down")


if __name__ == "__main__":
    try:
        main()
    finally:
        print("[done]")
