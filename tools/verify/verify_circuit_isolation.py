"""Live proof that one lane serves many circuits, not one."""
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import netutil  # noqa: E402
from lingling.cli import DATA_DIR, load_countries  # noqa: E402
from lingling.lanes import TorManager  # noqa: E402

HOST = "opencode.ai"
FAILS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def cookie_path() -> Path:
    for p in (DATA_DIR / "lanes").glob("tor-*/control_auth_cookie"):
        return p
    raise RuntimeError("no control cookie found")


def open_control(control_port: int):
    """An authenticated control connection, left open for the caller."""
    import stem.connection
    from stem.control import Controller

    ctl = Controller.from_port(port=control_port)
    stem.connection.authenticate_cookie(ctl, cookie_path=str(cookie_path()))
    return ctl


def dial_open(lane, cred):
    """A SOCKS5 CONNECT, held open, with no TLS on top."""
    sock = socket.create_connection(("127.0.0.1", lane.socks_port), timeout=60)
    sock.settimeout(60)
    local = sock.getsockname()
    err = netutil.socks5_open(sock, HOST, 443, cred=cred)
    if err:
        sock.close()
        return err, None, local
    return "", sock, local


def stream_circuits(ctl, port=443):
    """(stream_id, circuit_id) for every live stream to `port`."""
    out = []
    try:
        raw = ctl.get_info("stream-status") or ""
    except Exception:  # noqa: BLE001
        return out
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[3].endswith(f":{port}"):
            continue
        try:
            out.append((int(parts[0]), int(parts[2])))
        except ValueError:
            continue
    return out


def live_session() -> bool:
    """True when a lingling or opencode process is already running."""
    import subprocess
    try:
        out = subprocess.check_output(["tasklist"], text=True, timeout=15)
    except Exception:  # noqa: BLE001
        return False
    low = out.lower()
    return "lingling.exe" in low or "opencode.exe" in low


def main():
    if live_session():
        print(" !! a lingling/opencode session is running -- refusing to start.")
        print("    setup_lanes reaps the whole lane port range and would kill")
        print("    that session's lanes. Close it, then re-run.")
        return 2

    lanes_wanted = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    countries, fallback, preferred = load_countries()
    mgr = TorManager(DATA_DIR, count=lanes_wanted,
                     exit_countries=countries,
                     fallback_countries=fallback,
                     preferred_countries=preferred,
                     log=lambda *a: None)
    err = mgr.setup_lanes()
    if err:
        print(f" !! tor unavailable: {err}")
        return 1

    lane = mgr.lanes[0]
    print(f"== booting lane {lane.index} ==")
    mgr.start_lanes([lane])
    deadline = time.time() + 240
    while time.time() < deadline:
        if netutil.port_is_open("127.0.0.1", lane.socks_port, timeout=0.5):
            break
        time.sleep(2)
    if not netutil.port_is_open("127.0.0.1", lane.socks_port, timeout=0.5):
        print(" !! the lane never listened")
        mgr.stop_all()
        return 1
    # settle
    time.sleep(6)
    print(f"   listening on {lane.socks_port}, control {lane.control_port}")

    held = []
    try:
        print("\n== 1. Tor accepts the credential ==")
        err_a, a, _la = dial_open(lane, netutil.slot_cred(lane.index, 0))
        held.append(a)
        check("credential dial accepted", err_a == "", f"err={err_a!r}")
        if err_a:
            return 1

        print("\n== 2. a second username ==")
        err_b, b, _lb = dial_open(lane, netutil.slot_cred(lane.index, 1))
        held.append(b)
        check("second credential dial accepted", err_b == "", f"err={err_b!r}")
        if err_b:
            return 1

        print("\n== 3. and each rode its OWN circuit ==")
        verified = False
        try:
            ctl = open_control(lane.control_port)
        except Exception as exc:  # noqa: BLE001
            # Full traceback, not just the type name: the
            import traceback
            traceback.print_exc()
            print(f"   [SKIP] control port unreadable: {type(exc).__name__}")
            ctl = None
        if ctl is not None:
            try:
                pairs = stream_circuits(ctl)
            finally:
                ctl.close()
            circs = [c for _sid, c in pairs]
            check("both dials have a live stream", len(pairs) >= 2,
                  f"{len(pairs)} streams: {pairs}")
            check("each credential rode its own circuit",
                  len(circs) >= 2 and len(set(circs)) == len(circs),
                  f"circuits {circs}")
            verified = True

        print("\n" + "=" * 46)
        if FAILS:
            print(f"FAILURES: {len(FAILS)}")
            for f in FAILS:
                print("  - " + f)
            return 1
        if not verified:
            # The credential is accepted, but the claim that
            print("INCONCLUSIVE: credential accepted, isolation NOT verified")
            return 1
        print("CIRCUIT ISOLATION: CONFIRMED")
        return 0
    finally:
        for s in held:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        mgr.stop_all()


if __name__ == "__main__":
    sys.exit(main())
