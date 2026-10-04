"""The probe must warm the circuit the requests ride, not its own.

A credential that changes per request makes Tor build a three-hop circuit
every call. A probe that dials without the lane's credential builds a
circuit no request ever touches, so its warmth is spent on nothing.

Two halves: the source invariant (static), and the transport fact it
depends on (live) -- one credential, reused across separate connections.
"""
import ast
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
    print(f"  [{tag}] {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    if not ok:
        FAILS.append(name)


def _calls(path: Path, fn_name: str):
    """Every call to `fn_name` in `path`, `mod.fn(...)` or bare `fn(...)`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id == fn_name:
            out.append(node)
        elif isinstance(f, ast.Attribute) and f.attr == fn_name:
            out.append(node)
    return out


def _kw(call, name: str):
    """The value node of keyword `name`, or None."""
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def _creds(call):
    """The `cred=` argument's source text, or "" when absent."""
    node = _kw(call, "cred")
    if node is None:
        return ""
    return ast.unparse(node)


def static_checks():
    print("=== the probe warms the lane's own circuit ===")
    probes = _calls(ROOT / "lingling" / "health.py", "https_via_socks")
    check("health.py has one probe call", len(probes) == 1, f"{len(probes)}")
    if probes:
        src = _creds(probes[0])
        print(f"  probe cred = {src or '(none)'}")
        check("the probe dials the lane's credential, not the default",
              "lane_cred" in src,
              f"cred={src or '(absent)'} -- the probe builds a circuit no "
              f"request uses, so judging the lane warms nothing")

    print("\n=== live traffic rides that same circuit ===")
    for mod, fn in (("mitm.py", "socks5_open"), ("relay.py", "_dial")):
        calls = [c for c in _calls(ROOT / "lingling" / mod, fn)
                 if _kw(c, "cred") is not None]
        check(f"{mod}:{fn} passes a credential", bool(calls), "no cred kwarg")
        if calls:
            src = _creds(calls[0])
            print(f"  {mod}:{fn} cred = {src}")
            check(f"{mod} uses the lane's stable credential",
                  "lane_cred" in src,
                  f"cred={src} -- a per-request credential makes Tor rebuild a "
                  f"circuit on every call")

    print("\n=== the credential does not vary per request ===")
    src = (ROOT / "lingling" / "netutil.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "lane_cred"),
              None)
    check("lane_cred exists", fn is not None)
    if fn:
        body = ast.unparse(fn)
        print(f"  lane_cred -> {body.splitlines()[-1]}")
        check("lane_cred takes no request-varying argument",
              [a.arg for a in fn.args.args] == ["lane_index"],
              f"args={[a.arg for a in fn.args.args]} -- a seq/slot argument "
              f"is how the credential drifted per request")
        check("lane_cred delegates to the stable slot, not a varying one",
              "slot_cred" in body and "0" in body,
              f"{body.splitlines()[-1]} -- it must name slot 0")

    print("\n=== isolation itself is untouched ===")
    ccalls = [c for c in _calls(ROOT / "lingling" / "mitm.py", "slot_cred")
              if _kw(c, "cred") is not None]
    check("slot_cred still exists for isolated circuits",
          "def slot_cred" in src, "the isolation helper was removed")
    socks = (ROOT / "lingling" / "lanes.py").read_text(encoding="utf-8")
    check("SocksPort still isolates by credential",
          "IsolateSOCKSAuth" in socks and "KeepAliveIsolateSOCKSAuth" in socks,
          "circuit isolation was dropped, not just made stable")
    check("no live call site passes a per-request slot",
          not ccalls,
          f"mitm.py still builds a credential per call: "
          f"{[ast.unparse(c) for c in ccalls]}")


def cookie_path():
    for p in (DATA_DIR / "lanes").glob("tor-*/control_auth_cookie"):
        return p
    raise RuntimeError("no control cookie found")


def dial(lane, cred):
    """A SOCKS5 CONNECT with `cred`, held open."""
    sock = socket.create_connection(("127.0.0.1", lane.socks_port), timeout=60)
    sock.settimeout(60)
    err = netutil.socks5_open(sock, HOST, 443, cred=cred)
    if err:
        sock.close()
        return err, None
    return "", sock


def stream_circuits(ctl, port=443):
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
    import subprocess
    try:
        out = subprocess.check_output(["tasklist"], text=True, timeout=15)
    except Exception:  # noqa: BLE001
        return False
    low = out.lower()
    return "lingling.exe" in low or "opencode.exe" in low


def live_checks():
    if live_session():
        print("\n !! a lingling/opencode session is running -- refusing to "
              "touch the lane ports.")
        return False
    countries, fallback, preferred = load_countries()
    mgr = TorManager(DATA_DIR, count=1, exit_countries=countries,
                     fallback_countries=fallback,
                     preferred_countries=preferred, log=lambda *a: None)
    err = mgr.setup_lanes()
    if err:
        print(f"\n !! tor unavailable: {err}")
        return False
    lane = mgr.lanes[0]
    print(f"\n== booting lane {lane.index} ==")
    mgr.start_lanes([lane])
    deadline = time.time() + 240
    while time.time() < deadline:
        if netutil.port_is_open("127.0.0.1", lane.socks_port, timeout=0.5):
            break
        time.sleep(2)
    if not netutil.port_is_open("127.0.0.1", lane.socks_port, timeout=0.5):
        print(" !! the lane never listened")
        mgr.stop_all()
        return False
    time.sleep(6)
    held = []
    try:
        print("\n=== one credential, reused across separate connections ===")
        cred = netutil.lane_cred(lane.index)
        print(f"  credential = {cred[0]}")
        err1, a = dial(lane, cred)
        check("the lane's credential is accepted", err1 == "", f"err={err1!r}")
        if err1:
            return False
        held.append(a)

        # second connection, same credential
        err2, b = dial(lane, cred)
        check("a later connection reuses the credential", err2 == "",
              f"err={err2!r}")
        if err2:
            return False
        held.append(b)

        try:
            import stem.connection
            from stem.control import Controller
            ctl = Controller.from_port(port=lane.control_port)
            stem.connection.authenticate_cookie(ctl, cookie_path=str(
                cookie_path()))
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print(f"   [SKIP] control port unreadable: {type(exc).__name__}")
            return False
        try:
            pairs = stream_circuits(ctl)
        finally:
            ctl.close()
        circs = [c for _s, c in pairs]
        print(f"  streams {pairs}")
        check("both same-credential dials are live", len(pairs) >= 2,
              f"{len(pairs)} streams")
        check("they ride ONE circuit, so the probe's warmth is reused",
              len(circs) >= 2 and len(set(circs)) == 1,
              f"circuits {circs} -- more than one means the credential did not "
              f"pin a single circuit")
        return True
    finally:
        for s in held:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        mgr.stop_all()


def main():
    static_checks()
    verified = live_checks()
    print("\n" + "=" * 52)
    if FAILS:
        print(f"FAILURES: {len(FAILS)}")
        for f in FAILS:
            print("  - " + f)
        return 1
    if not verified:
        print("INCONCLUSIVE: source is right, reuse not verified live")
        return 1
    print("PROBE CIRCUIT: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())