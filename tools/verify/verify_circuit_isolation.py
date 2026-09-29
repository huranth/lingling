"""Live proof that one lane serves many circuits, not one.

    python tools/verify/verify_circuit_isolation.py [lanes]

Boots a real lane and dials it twice with two different SOCKS credentials,
then asks the lane's own control port how many circuits exist.

Why this matters. Tor's ``IsolateSOCKSAuth`` is on by default and keys circuit
isolation on the SOCKS **username**. The code used to greet every stream with
no-auth, so every stream shared one empty key -- one lane, one circuit, and one
``RELAY_END`` took every concurrent stream on that lane down together. That is
the mechanism behind the SSLEOFError bursts and the mid-body cuts: with ~20
streams per lane, a single teardown cost ~20 requests.

Offering username/password auth is what fixes it, but only if Tor actually
*accepts* the credential and actually *isolates* on it. Both are testable
here, so neither has to be assumed:

  1. the greeting is accepted (the dial returns "")
  2. two usernames yield two circuits, not one

Costs no model quota: a bare CONNECT sends no API request at all.
"""
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
    """An authenticated control connection, left open for the caller.

    Deliberately not a context manager: the caller must hold it open across
    both dials, because a stream only carries its circuit id while it is
    alive. The first version returned a controller whose `with` block had
    already closed -- so every query would have come back empty."""
    import stem.connection
    from stem.control import Controller

    ctl = Controller.from_port(port=control_port)
    stem.connection.authenticate_cookie(ctl, cookie_path=str(cookie_path()))
    return ctl


def dial_open(lane, cred):
    """A SOCKS5 CONNECT, held open, with no TLS on top.

    Deliberately no TLS handshake: a bare handshake sends no HTTP request, so
    the far end closes the connection and the stream is gone before it can be
    read -- measured, `stream-status` came back empty and the test reported
    FAIL on a working fix. A bare CONNECT keeps the stream alive for as long as
    the socket is held, which is all this needs.

    Returns (err, socket, local_address)."""
    sock = socket.create_connection(("127.0.0.1", lane.socks_port), timeout=60)
    sock.settimeout(60)
    local = sock.getsockname()
    err = netutil.socks5_open(sock, HOST, 443, cred=cred)
    if err:
        sock.close()
        return err, None, local
    return "", sock, local


def stream_circuits(ctl, port=443):
    """(stream_id, circuit_id) for every live stream to `port`.

    Read from the raw ``stream-status`` rather than ``get_streams()``. stem
    leaves ``Stream.source_address`` as None, so the obvious "match the stream
    by the local port I dialled from" approach matches nothing and reports a
    FAIL on a working fix -- which is exactly what the first version of this
    test did. The raw line is:

        9 SUCCEEDED 3 172.65.90.20:443
        ^id ^status ^circuit ^target

    and that circuit id is the thing worth asserting on."""
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
    """True when a lingling or opencode process is already running.

    ``setup_lanes`` calls ``_reap_orphans``, which kills anything LISTENING in
    the lane port range and cannot tell an orphan from a session someone is
    using. Measured, the hard way: running this against a live session killed
    lanes 2, 3 and 4, and the health daemon revived them, three times in a row.
    ``live_soak``'s docstring warns about exactly this and I did it anyway.

    So refuse rather than reap. The same hazard applies to any harness that
    boots lanes while the CLI is up."""
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
            # Full traceback, not just the type name: the first version printed
            # "NameError" and nothing else, which named the bug without
            # locating it.
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
            # The credential is accepted, but the claim that matters was never
            # tested. Saying CONFIRMED here would be the same class of mistake
            # this whole exercise is about: reporting a result that was not
            # measured.
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
