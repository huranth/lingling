"""The per-lane timeout tally has to see every timeout."""
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
    """Speak the SOCKS5 handshake, then never say anything more."""
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
    """Answer the SOCKS5 greeting and CONNECT, then say ONE byte and stop."""
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


def stall_socks_upstream(port_holder, ready):
    """Accept the TCP connection, then never answer the SOCKS5 greeting."""
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
        threading.Thread(target=_stall, args=(c,), daemon=True).start()


def _stall(c):
    """Read the greeting, then silence -- the dial never completes."""
    try:
        c.settimeout(30)
        c.recv(64)
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

    # the transport points at lane.socks_port, so make the
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
        # BOTH windows, and this block needs them set
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

    print("\n=== a dial that times out IS charged, so the lane can repin ===")
    holder3, ready3 = [], threading.Event()
    threading.Thread(target=stall_socks_upstream, args=(holder3, ready3),
                     daemon=True).start()
    ready3.wait(5)
    stall_port = holder3[0]
    charged_dial = []
    try:
        # One short window, so the stalled greeting is a SOCKS timeout.
        mitm._FIRST_BYTE_TIMEOUT = 0.5
        relay = Relay()
        relay.tor.charged = charged_dial
        events_d = []
        lane_d = Lane(1)
        lane_d.socks_port = stall_port
        mitm._roundtrip(Client(), lane_d, "opencode.ai", 443, "POST",
                        "/zen/v1/responses", {}, b"{}",
                        lambda e: events_d.append(e), 1, 1, time.time(),
                        relay, charge_timeout=True)
    finally:
        netutil.socks5_open = real_socks
        ssl.create_default_context = real_ctx
        mitm._READ_TIMEOUT = real_ceiling
        mitm._FIRST_BYTE_TIMEOUT = real_first
    err_d = events_d[0].get("err") if events_d else None
    print(f"  stalled dial: err={err_d!r}  charged={charged_dial}")
    check("a stalled dial reads as a SOCKS timeout",
          err_d == "timed out", f"err={err_d!r}")
    check("a dial timeout is charged to the lane",
          charged_dial == [1],
          f"charged={charged_dial} -- an uncharged dial leaves lane 2 stuck")

    print("\n=== a refused dial is NOT charged, so a cold start survives ===")
    # A lane whose port is closed is down, not limited; the health daemon
    # owns that, so the tally must stay clear.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    charged_refused = []
    try:
        mitm._FIRST_BYTE_TIMEOUT = real_first
        relay = Relay()
        relay.tor.charged = charged_refused
        events_r = []
        lane_r = Lane(1)
        lane_r.socks_port = dead_port
        mitm._roundtrip(Client(), lane_r, "opencode.ai", 443, "POST",
                        "/zen/v1/responses", {}, b"{}",
                        lambda e: events_r.append(e), 1, 1, time.time(),
                        relay, charge_timeout=True)
    finally:
        netutil.socks5_open = real_socks
        ssl.create_default_context = real_ctx
        mitm._READ_TIMEOUT = real_ceiling
        mitm._FIRST_BYTE_TIMEOUT = real_first
    check("a refused dial is not charged",
          charged_refused == [],
          f"charged={charged_refused} -- only a timeout means a dead exit")

    print()
    if FAILS:
        print("TIMEOUT TALLY: FAILED")
        return 1
    print("TIMEOUT TALLY: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
