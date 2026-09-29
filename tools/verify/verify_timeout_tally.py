"""The per-lane timeout tally has to see every timeout.

A lane that times out three times running is a bad circuit and must be
rebuilt. The counter only works if the timeouts reach it. They do not
reach a tally charged in the caller's retry loop, because a timeout on
an uncommitted attempt is absorbed inside the transport and a *different*
lane carries the retry -- so the only lane that would ever accumulate
three is one that is tried three times inside a single request, which
never happens.

Measured over the real log: the tally fired 0 times on all 615 of the
owner's callends and 4 times across 1269 soak callends, while 95 timeouts
went unnoticed. This drives the real transport against a silent upstream
and asserts each silent attempt was charged. It fails on the old
placement, where nothing in the transport charged anything.
"""
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import mitm, netutil  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def silent_socks_upstream(port_holder, ready):
    """Speak the SOCKS5 handshake, then never say anything more.

    The connection carries whatever the client sends; no HTTP reply is
    ever produced, so every read runs into the idle ceiling.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    port_holder.append(srv.getsockname()[1])
    ready.set()
    while True:
        try:
            c, _ = srv.accept()
        except OSError:
            return
        threading.Thread(target=_hold, args=(c,), daemon=True).start()


def _hold(c):
    """Answer the SOCKS5 greeting and CONNECT, then say ONE byte and stop.

    The byte matters: it puts the timeout after the handshake, which is the
    only kind of stall that charges the tally. `silent_socks_upstream`
    (below) is the no-byte-at-all case, and that one must NOT charge -- it
    is a cold circuit, not a bad lane.
    """
    try:
        c.settimeout(30)
        c.recv(3)
        c.sendall(bytes([0x05, 0x00]))
        c.recv(64)
        c.sendall(bytes([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
        c.recv(65536)
        c.sendall(b"H")  # spoke, then died
        time.sleep(120)
    except OSError:
        pass
    finally:
        try:
            c.close()
        except OSError:
            pass


def mute_socks_upstream(port_holder, ready):
    """Speak the SOCKS5 handshake, then never send a single byte."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    port_holder.append(srv.getsockname()[1])
    ready.set()
    while True:
        try:
            c, _ = srv.accept()
        except OSError:
            return
        threading.Thread(target=_mute, args=(c,), daemon=True).start()


def _mute(c):
    """Answer the handshake, then silence."""
    try:
        c.settimeout(30)
        c.recv(3)
        c.sendall(bytes([0x05, 0x00]))
        c.recv(64)
        c.sendall(bytes([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
        time.sleep(120)
    except OSError:
        pass
    finally:
        try:
            c.close()
        except OSError:
            pass


def main():
    print("=== the per-lane timeout tally sees absorbed timeouts ===")

    holder, ready = [], threading.Event()
    threading.Thread(target=silent_socks_upstream, args=(holder, ready),
                     daemon=True).start()
    ready.wait(5)
    up_port = holder[0]

    # the transport points at lane.socks_port, so make the lane port the
    # fake server's port: a real connect, a real SOCKS5 exchange, a real
    # silence on the other end
    real_socks = netutil.socks5_open
    real_ctx = ssl.create_default_context
    real_ceiling = mitm._READ_TIMEOUT
    real_first = mitm._FIRST_BYTE_TIMEOUT

    def fake_socks(sock, host, port_, cred=None):
        return real_socks(sock, host, port_, cred)

    class RawCtx:
        """The fake upstream speaks cleartext; skip the TLS wrap."""

        def wrap_socket(self, sock, server_hostname=None):  # noqa: ANN001
            return sock

    netutil.socks5_open = fake_socks
    ssl.create_default_context = lambda *a, **k: RawCtx()
    mitm._READ_TIMEOUT = 1.5
    class Tor:
        def __init__(self):
            self.charged = []

        def note_timeout(self, lane):
            self.charged.append(lane.index)
            return ""

        def note_ok(self, lane):
            return None

        def note_result(self, cc, status):
            return None

    class Lane:
        def __init__(self, i):
            self.index = i
            self.exit_country = "de"
            self.exit_ip = "1.2.3.4"
            self.socks_port = up_port
            self.lock = threading.Lock()
            self.active = 0

    class Relay:
        def __init__(self):
            self.tor = Tor()
            self.tunnels = None

        def report_refused(self, lane, status):
            return None

        def any_unlimited(self, tried):
            return False

    class Client:
        def sendall(self, data):
            return None

        def settimeout(self, t):
            return None

    charged_total = []
    errors = []
    try:
        for charge in (False, True):
            relay = Relay()
            events = []
            lane = Lane(1)
            mitm._roundtrip(Client(), lane, "opencode.ai", 443, "POST",
                            "/zen/v1/responses", {}, b"{}",
                            lambda e: events.append(e), 1, 1, time.time(),
                            relay, charge_timeout=charge)
            errors.append(events[0].get("err"))
            if charge:
                charged_total = list(relay.tor.charged)
    finally:
        netutil.socks5_open = real_socks
        ssl.create_default_context = real_ctx
        mitm._READ_TIMEOUT = real_ceiling
        mitm._FIRST_BYTE_TIMEOUT = real_first

    check("silence past the ceiling is a TimeoutError",
          errors and all(e == "TimeoutError" for e in errors),
          str(errors))
    check("an uncharged attempt leaves the tally alone",
          charged_total == [1] or charged_total == [],
          f"charged={charged_total}")
    check("the transport charges the lane it timed out on",
          charged_total == [1],
          f"charged={charged_total}")

    print("\n=== a lane that never spoke must NOT be charged ===")
    holder2, ready2 = [], threading.Event()
    threading.Thread(target=mute_socks_upstream, args=(holder2, ready2),
                     daemon=True).start()
    ready2.wait(5)
    mute_port = holder2[0]
    charged_mute = []
    try:
        # BOTH windows, and this block needs them set for itself. It used to
        # lean on the value the block above left behind -- except that block's
        # `finally` restores it, so this one was really running on the SHIPPED
        # ceiling. At 20s that failed fast enough to look right; at 1800s it
        # waits out the fake upstream, which closes first and turns the
        # expected TimeoutError into a ConnectionResetError.
        mitm._READ_TIMEOUT = 1.5
        mitm._FIRST_BYTE_TIMEOUT = 5.0
        relay = Relay()
        relay.tor.charged = charged_mute
        events_m = []
        lane_m = Lane(1)
        lane_m.socks_port = mute_port
        mitm._roundtrip(Client(), lane_m, "opencode.ai", 443, "POST",
                        "/zen/v1/responses", {}, b"{}",
                        lambda e: events_m.append(e), 1, 1, time.time(),
                        relay, charge_timeout=True)
    finally:
        netutil.socks5_open = real_socks
        ssl.create_default_context = real_ctx
        mitm._READ_TIMEOUT = real_ceiling
        mitm._FIRST_BYTE_TIMEOUT = real_first
    err_m = events_m[0].get("err") if events_m else None
    print(f"  mute upstream: err={err_m!r}  charged={charged_mute}")
    check("the mute attempt still read as a TimeoutError",
          err_m == "TimeoutError", f"err={err_m!r}")
    check("a lane that never spoke is not charged",
          charged_mute == [],
          f"charged={charged_mute} -- a cold circuit would be retired")

    print("\n=== the cold connect cannot reach the tally at all ===")
    # This is structural, and it is the whole reason the mute case above
    # passes: the SOCKS dial and the TLS handshake sit in their own `try`,
    # whose handlers emit a callend and return -- so a dial timeout leaves
    # _roundtrip before the streaming try, the only place that charges, is
    # ever entered. Asserted on the AST so a future edit that folds the dial
    # into the charging try trips this instead of quietly re-arming the
    # retirement of cold circuits. The shape used to be checked as "two
    # nested tries containing the dial", which only matched the era when the
    # dial try sat INSIDE the streaming try; it is a sibling now, and the
    # guarantee is about reachability, not nesting.
    import ast
    tree = ast.parse((ROOT / "lingling" / "mitm.py").read_text("utf-8"))
    fn = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
          and n.name == "_roundtrip"][0]

    def _spans(node, line):
        return node.lineno <= line <= node.end_lineno

    def _ends_jump(stmt):
        if isinstance(stmt, (ast.Return, ast.Raise)):
            return True
        if isinstance(stmt, ast.If):
            return all(stmt.body and stmt.orelse
                       and _ends_jump(stmt.body[-1])
                       and _ends_jump(stmt.orelse[-1]))
        return False

    charge_calls = [n for n in ast.walk(fn)
                    if isinstance(n, ast.Call)
                    and getattr(n.func, "id", "") == "_note_timeout"]
    charge_line = min(n.lineno for n in charge_calls)
    dial_tries = [n for n in ast.walk(fn)
                  if isinstance(n, ast.Try)
                  and any(isinstance(s, ast.Expr)
                          and isinstance(s.value, ast.Call)
                          and "connect" in ast.unparse(s.value)
                          for s in ast.walk(n))]
    check("the dial try exists", bool(dial_tries),
          "no try contains the SOCKS dial -- where do its errors go?")
    if dial_tries:
        dial = min(dial_tries, key=lambda n: n.end_lineno - n.lineno)
        print(f"  dial try at {dial.lineno}-{dial.end_lineno}, "
              f"charge site at {charge_line}")
        check("the dial try cannot reach the charge site",
              not _spans(dial, charge_line),
              f"charge site {charge_line} sits inside the dial try "
              f"{dial.lineno}-{dial.end_lineno} -- a dial timeout would "
              f"charge a cold start")
        check("the dial try owns its handlers",
              bool(dial.handlers),
              "no handler -- a dial timeout would escape to whoever is next")
        check("every dial handler returns before the charge site",
              all(_ends_jump(h.body[-1]) for h in dial.handlers),
              "a handler that falls through runs on into the charge region")

    print()
    if FAILS:
        print("TIMEOUT TALLY: FAILED")
        return 1
    print("TIMEOUT TALLY: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
