"""Why a late first token does NOT count as a timeout -- and when it does.

The owner saw a lane produce its first token at 25s and asked why that was
not a timeout. The answer is one property of the socket:

  **A socket timeout is PER BLOCKING READ, not a total budget.**

`up.settimeout(_READ_TIMEOUT)` (20s) is armed once, after the request is
written. Every subsequent blocking read then gets its own fresh 20s. So the
ceiling means "this long with *nothing* coming", not "this long for the whole
call". A stream that keeps arriving -- however slowly -- is never cut off,
and the total may run to minutes. That is why the log holds calls with
`first_byte_s = 34.8` and `status = 200`.

The 25s the owner saw was therefore a stream whose BYTES kept arriving while
the first model EVENT was late. That is not silence, and not a timeout.

The only thing that trips the ceiling is a single gap longer than it -- which
is what a genuinely dead lane looks like, and also what a mid-answer stall
looks like.

This drives the shipped `_read_head` and `_roundtrip` against real sockets.
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


HEAD = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
        b"transfer-encoding: chunked\r\n\r\n")
CHUNK = b"data: {\"type\":\"response.output_text.delta\"}\n\n"


def quiet(schedule):
    """A schedule whose hang-up is expected, not a failure."""

    def wrapped(c):
        try:
            schedule(c)
        except OSError:
            pass

    return wrapped


def start_upstream(schedule):
    """A SOCKS5-speaking upstream that runs `schedule` once connected.

    `_roundtrip` dials lane.socks_port itself and only then calls
    socks5_open, so the fake must answer the greeting and the CONNECT
    reply -- it must not dial anything of its own, or the connect fails
    with WinError 10056 and looks like a mystery OSError at 0.0s.
    """
    holder = []
    ready = threading.Event()

    def serve():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        holder.append(srv.getsockname()[1])
        ready.set()
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=quiet(schedule), args=(c,),
                             daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    ready.wait(5)
    return holder[0]


def socks_hello(c):
    """Answer the SOCKS5 greeting and CONNECT, then read the request."""
    c.settimeout(180)
    c.recv(3)
    c.sendall(bytes([0x05, 0x00]))
    c.recv(64)
    c.sendall(bytes([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
    c.recv(65536)


def send_chunk(c, payload=CHUNK):
    c.sendall(b"%x\r\n" % len(payload) + payload + b"\r\n")


def drip_server(gaps):
    """A cleartext server that sends one byte per gap, then goes quiet."""
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
            c, _ = srv.accept()
            for g in gaps:
                time.sleep(g)
                c.sendall(HEAD[:1])
            time.sleep(30.0)
            c.close()

    threading.Thread(target=serve, daemon=True).start()
    ready.wait(5)
    return holder[0]


def read_head_direct(gaps, ceiling):
    """Read exactly as many bytes as the server drips, one read each.

    The socket is armed ONCE and nothing re-arms it. If the total can
    still exceed the ceiling, the timeout is per read, not cumulative.
    """
    port = drip_server(gaps)
    s = socket.create_connection(("127.0.0.1", port))
    s.settimeout(ceiling)
    f = s.makefile("rb")
    t0 = time.time()
    try:
        for _ in range(len(gaps)):
            if not f.read(1):
                break
        survived = True
    except OSError as exc:
        survived = f"{type(exc).__name__}"
    secs = time.time() - t0
    s.close()
    return survived, secs


def run(port, ceiling=20.0, first_byte=30.0):
    """Drive the shipped `_roundtrip` end to end against the fake."""
    real_socks = netutil.socks5_open
    real_ctx = ssl.create_default_context
    old_ceiling = mitm._READ_TIMEOUT
    old_first = mitm._FIRST_BYTE_TIMEOUT

    def fake_socks(sock, host, port_, cred=None):
        return real_socks(sock, host, port_, cred)

    class RawCtx:
        """The fake upstream speaks cleartext; skip the TLS wrap."""

        def wrap_socket(self, sock, server_hostname=None):  # noqa: ANN001
            return sock

    netutil.socks5_open = fake_socks
    ssl.create_default_context = lambda *a, **k: RawCtx()
    mitm._READ_TIMEOUT = ceiling
    mitm._FIRST_BYTE_TIMEOUT = first_byte

    class Lane:
        index = 1
        exit_country = "de"
        exit_ip = "1.2.3.4"
        socks_port = port
        lock = threading.Lock()
        active = 0

    class Tap:
        """Stands in for the client socket; records what reached it."""

        def __init__(self):
            self.got = bytearray()

        def sendall(self, data):
            self.got.extend(data)

        def settimeout(self, t):
            return None

    relay = type("R", (), {"tor": None})()
    tap = Tap()
    events = []
    try:
        t0 = time.time()
        err, status, _h, _r = mitm._roundtrip(
            tap, Lane(), "opencode.ai", 443, "POST", "/zen/v1/responses",
            {}, b'{"model":"m"}', events.append, 1, 1, t0, relay)
        secs = time.time() - t0
    finally:
        netutil.socks5_open = real_socks
        ssl.create_default_context = real_ctx
        mitm._READ_TIMEOUT = old_ceiling
        mitm._FIRST_BYTE_TIMEOUT = old_first
    end = [e for e in events if e.get("type") == "callend"]
    fes = end[-1].get("first_event_s", 0) if end else 0
    fbs = end[-1].get("first_byte_s", 0) if end else 0
    return (err == "" and status == 200), secs, fbs, fes, err


def case_head_trickles(c):
    """The head trickles in over 9s, well past the 5s ceiling used here.

    Every byte arrives sooner than the ceiling apart, so no single read
    ever waits too long -- and the total sails past the ceiling.
    """
    socks_hello(c)
    for i in range(len(HEAD)):
        c.sendall(HEAD[i:i + 1])
        time.sleep(0.25)
    send_chunk(c)
    c.sendall(b"0\r\n\r\n")


def case_event_late(c):
    """Head at once, then a gap longer than the ceiling before event one.

    One uninterrupted silence, so a single read exceeds the ceiling.
    """
    socks_hello(c)
    c.sendall(HEAD)
    time.sleep(9.0)
    send_chunk(c)
    c.sendall(b"0\r\n\r\n")


def case_body_silent(c):
    """Head at once, then nothing ever."""
    socks_hello(c)
    c.sendall(HEAD)
    time.sleep(120.0)


def case_connect_silent(c):
    """Never answer the SOCKS5 handshake at all."""
    time.sleep(120.0)


def main():
    # Short windows so the proof runs in seconds; the ratio is what matters.
    ceiling, first_byte = 5.0, 8.0

    print("=== (A0) per-read, not cumulative: 6 bytes 1.5s apart, "
          "ceiling 2s ===")
    print("  armed once, nothing re-arms; the total is 9s, over the ceiling")
    survived, secs = read_head_direct([1.5] * 6, 2.0)
    print(f"  result: survived={survived}  wall={secs:.1f}s")
    check("a stream under the ceiling per read outlives the total",
          survived is True and secs > 6.0,
          f"survived={survived} wall={secs:.1f}s")

    print("\n=== (A0b) one gap OVER the ceiling still dies ===")
    survived, secs = read_head_direct([3.0], 2.0)
    print(f"  result: survived={survived}  wall={secs:.1f}s")
    check("a single gap over the ceiling is caught",
          survived is not True and abs(secs - 2.0) < 1.0,
          f"survived={survived} wall={secs:.1f}s")

    print("\n=== (A) a head that TRICKLES through `_roundtrip`, ceiling 5s ===")
    p = start_upstream(case_head_trickles)
    ok, secs, fbs, fes, err = run(p, ceiling, first_byte)
    print(f"  result: ok={ok}  wall={secs:.1f}s  first_byte={fbs}s  "
          f"first_event={fes}s  err={err!r}")
    check("a trickling head outlives the ceiling",
          ok and secs > ceiling,
          f"ok={ok} wall={secs:.1f}s err={err!r}")

    print("\n=== (B) head, then a 9s BODY gap before the first event ===")
    p = start_upstream(case_event_late)
    ok, secs, fbs, fes, err = run(p, ceiling, first_byte)
    print(f"  result: ok={ok}  wall={secs:.1f}s  first_byte={fbs}s  "
          f"first_event={fes}s  err={err!r}")
    check("a body gap over the ceiling is caught",
          (not ok) and err == "TimeoutError"
          and abs(secs - ceiling) < 1.5,
          f"ok={ok} wall={secs:.1f}s err={err!r}")

    print("\n=== (C) a body that never speaks again ===")
    p = start_upstream(case_body_silent)
    ok, secs, fbs, fes, err = run(p, ceiling, first_byte)
    print(f"  result: ok={ok}  wall={secs:.1f}s  first_byte={fbs}s  "
          f"err={err!r}")
    check("a silent body is caught at the ceiling",
          (not ok) and abs(secs - ceiling) < 1.5 and err == "TimeoutError",
          f"ok={ok} wall={secs:.1f}s err={err!r}")

    print("\n=== (D) a cold connect that never completes (its own window) ===")
    p = start_upstream(case_connect_silent)
    ok, secs, fbs, fes, err = run(p, ceiling, first_byte)
    print(f"  result: ok={ok}  wall={secs:.1f}s  err={err!r}")
    print("  the cold connect is governed by the FIRST-BYTE window, so its")
    print("  wall is that window -- not the body ceiling")
    check("the cold connect uses its own window, not the body ceiling",
          (not ok) and abs(secs - first_byte) < 1.5
          and err in ("TimeoutError", "timed out"),
          f"ok={ok} wall={secs:.1f}s err={err!r}")

    print()
    if FAILS:
        print("FIRST TOKEN GRACE: FAILED")
        return 1
    print("FIRST TOKEN GRACE: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
