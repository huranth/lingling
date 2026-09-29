"""The SOCKS5 handshake spends ONE window, not one per read."""
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


def stall_server(greet_delay, reply_greeting):
    """Answer the SOCKS5 greeting (maybe late), then never the CONNECT reply."""
    holder = []
    ready = threading.Event()

    def serve():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        holder.append(srv.getsockname()[1])
        ready.set()
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return

            def hold(c=c):
                try:
                    c.settimeout(60)
                    c.recv(3)
                    time.sleep(greet_delay)
                    if reply_greeting:
                        c.sendall(bytes([0x05, 0x00]))
                    c.recv(64)
                    time.sleep(60)      # never the CONNECT reply
                except OSError:
                    pass

            threading.Thread(target=hold, daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    ready.wait(5)
    return holder[0]


class RawCtx:
    """The fake upstream speaks cleartext; skip the TLS wrap."""

    def wrap_socket(self, sock, server_hostname=None):  # noqa: ANN001
        return sock


class Lane:
    index = 1
    exit_country = "de"
    exit_ip = "1.2.3.4"
    lock = threading.Lock()
    active = 0


class Tap:
    def sendall(self, data):
        pass

    def settimeout(self, t):
        pass


def run(port, window):
    real_socks = netutil.socks5_open
    real_ctx = ssl.create_default_context
    old_fb = mitm._FIRST_BYTE_TIMEOUT
    old_rd = mitm._READ_TIMEOUT

    netutil.socks5_open = lambda s, h, p, cred=None: real_socks(s, h, p, cred)
    ssl.create_default_context = lambda *a, **k: RawCtx()
    mitm._FIRST_BYTE_TIMEOUT = window
    mitm._READ_TIMEOUT = window / 2
    lane = Lane()
    lane.socks_port = port
    try:
        t0 = time.time()
        err, _st, _h, _r = mitm._roundtrip(
            Tap(), lane, "opencode.ai", 443, "POST", "/zen/v1/responses",
            {}, b'{"model":"m"}', lambda e: None, 1, 1, t0,
            type("R", (), {"tor": None})())
        return err, time.time() - t0
    finally:
        netutil.socks5_open = real_socks
        ssl.create_default_context = real_ctx
        mitm._FIRST_BYTE_TIMEOUT = old_fb
        mitm._READ_TIMEOUT = old_rd


def main():
    W = 3.0
    print(f"window = {W:.0f}s; the whole handshake must stay inside it\n")

    print("=== greeting answered at once, CONNECT reply never ===")
    err, secs = run(stall_server(0.0, True), W)
    print(f"  err={err!r} wall={secs:.1f}s")
    check("a stalled CONNECT spends one window",
          err == "timed out" and abs(secs - W) < 1.0,
          f"wall={secs:.1f}s err={err!r}")

    print("\n=== greeting answered 1.5s late, CONNECT reply never ===")
    err, secs = run(stall_server(1.5, True), W)
    print(f"  err={err!r} wall={secs:.1f}s")
    print("  the second read gets only the remainder, so the total stays put")
    check("a late greeting does NOT push the total past the window",
          err == "timed out" and secs < W + 0.8,
          f"wall={secs:.1f}s (additive would be ~{W+1.5:.1f}s)")

    print("\n=== greeting never answered ===")
    err, secs = run(stall_server(0.0, False), W)
    print(f"  err={err!r} wall={secs:.1f}s")
    check("a silent greeting spends one window",
          err == "timed out" and abs(secs - W) < 1.0,
          f"wall={secs:.1f}s err={err!r}")

    print("\n=== a prompt handshake still succeeds ===")
    print("  (the bound must not break the working path)")
    ok, secs = fast_handshake(W)
    print(f"  greeting+CONNECT answered at once: ok={ok} wall={secs:.2f}s")
    check("a working handshake is untouched",
          ok and secs < W / 2, f"ok={ok} wall={secs:.2f}s")

    print()
    if FAILS:
        print("COLD CONNECT: FAILED")
        return 1
    print("COLD CONNECT: CONFIRMED")
    return 0


def fast_handshake(window):
    """A well-behaved upstream: answer both reads immediately."""
    holder = []
    ready = threading.Event()

    def serve():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(2)
        holder.append(srv.getsockname()[1])
        ready.set()
        c, _ = srv.accept()
        try:
            c.settimeout(30)
            c.recv(3)
            c.sendall(bytes([0x05, 0x00]))
            c.recv(64)
            c.sendall(bytes([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
            c.recv(65536)
            time.sleep(30)
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    ready.wait(5)
    port = holder[0]

    s = socket.create_connection(("127.0.0.1", port))
    s.settimeout(window)
    t0 = time.time()
    try:
        err = netutil.socks5_open(s, "opencode.ai", 443)
    except OSError as exc:
        err = f"{type(exc).__name__}"
    secs = time.time() - t0
    s.close()
    return err == "", secs


if __name__ == "__main__":
    sys.exit(main())
