"""A client that stops reading must not get its lane retired.

Found in a soak, and only visible because two fields were logged side by side:

    TimeoutError  /zen/v1/responses  kb 47.6  secs 302.8  max_wait_s 2.2

302.8 seconds, and the longest upstream read was 2.2s. No read was anywhere
near the post-commit ceiling of the day, so the timeout could not have come
from the
upstream socket. It came from `client.sendall`: the client socket carries its
own 300s timeout (`handle_conn`), a client that stops reading makes the send
block for that whole window, and the resulting TimeoutError was then charged
to the LANE.

That is the one thing this codebase must never do -- invent a failure state
for a healthy lane. A stalled reader says nothing about the relay it rode.

The fix records WHICH SIDE blocked (`last_op`) and charges only when it was
the upstream. `client_wait_s` is logged apart from `max_wait_s` so the two can
never be confused again.

This drives the shipped `_roundtrip` with a client that never reads, and
asserts the lane is not charged. It fails on the old code, where the charge
was gated only on `err.endswith("TimeoutError")`.
"""
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import mitm  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    """Detail is the failure reason, so only show it when it failed."""
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    if not ok:
        FAILS.append(name)


HEAD = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
        b"transfer-encoding: chunked\r\n\r\n")
EVENT = b'data: {"type":"response.output_text.delta","delta":"x"}\n\n'


def quiet(fn):
    """A schedule whose hang-up is expected, not a failure."""

    def wrapped(c):
        try:
            fn(c)
        except OSError:
            pass

    return wrapped


def start_upstream(schedule):
    """A SOCKS5-speaking upstream that runs `schedule` once connected."""
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


def chunk(c, body):
    c.sendall(b"%x\r\n" % len(body) + body + b"\r\n")


class StalledClient:
    """A client socket that accepts the first bytes, then never reads again.

    The first send has to succeed or nothing commits, so the budget is spent
    on the SECOND send -- which is exactly what a client that stops consuming
    mid-stream does to the relay.
    """

    def __init__(self, budget=2000, stall_for=30.0):
        self.budget = budget
        self.stall_for = stall_for
        self.got = 0
        self.peak = 0.0
        self.sends = 0

    def sendall(self, data):
        self.sends += 1
        if self.got < self.budget:
            self.got += len(data)
            return
        # the client has stopped reading: block like a full window would
        t = time.monotonic()
        time.sleep(self.stall_for)
        waited = time.monotonic() - t
        if waited > self.peak:
            self.peak = waited
        raise TimeoutError("timed out")

    def settimeout(self, t):
        return None


def run(port, client, upstream_sets_timeout=True):
    """Drive `_roundtrip`; returns (err, record, charged)."""
    real_ctx = ssl.create_default_context
    old_post = mitm._STREAM_IDLE_TIMEOUT
    old_pre = mitm._READ_TIMEOUT

    class RawCtx:
        """The fake upstream speaks cleartext; skip the TLS wrap."""

        def wrap_socket(self, sock, server_hostname=None):  # noqa: ANN001
            return sock

    ssl.create_default_context = lambda *a, **k: RawCtx()
    mitm._STREAM_IDLE_TIMEOUT = 3.0
    # BOTH windows, explicitly. This used to leave the pre-commit one at its
    # shipped value, which was 20s and made the mute-upstream case fail in
    # about 20s. That value is now 1800s -- deliberately, because the head
    # read is the model's time-to-first-token -- so a test that leans on it
    # hangs instead of failing, which is the worst way for a test to break.
    mitm._READ_TIMEOUT = 3.0

    class Lane:
        index = 1
        exit_country = "de"
        exit_ip = "1.2.3.4"
        socks_port = port
        lock = threading.Lock()
        active = 0

    charged = []

    class Tor:
        def note_timeout(self, lane):
            charged.append(lane.index)
            return ""

    events = []
    try:
        t0 = time.time()
        err, _status, _h, _r = mitm._roundtrip(
            client, Lane(), "opencode.ai", 443, "POST", "/zen/v1/responses",
            {}, b'{"model":"m"}', events.append, 1, 1, t0,
            type("R", (), {"tor": Tor()})(), charge_timeout=True)
    finally:
        ssl.create_default_context = real_ctx
        mitm._STREAM_IDLE_TIMEOUT = old_post
        mitm._READ_TIMEOUT = old_pre
    end = [e for e in events if e.get("type") == "callend"]
    return err, (end[-1] if end else {}), charged


def main():
    print("=== a client that stops reading mid-stream ===")

    def case_streams_then_idles(c):
        socks_hello(c)
        c.sendall(HEAD)
        # commit, then keep sending so the relay has to push to the client
        for _ in range(200):
            chunk(c, EVENT)
        c.sendall(b"0\r\n\r\n")

    client = StalledClient(budget=2000, stall_for=5.0)
    err, rec, charged = run(start_upstream(case_streams_then_idles), client)
    print(f"  err={err!r}  sends={client.sends}  "
          f"max_wait_s={rec.get('max_wait_s')}  "
          f"client_wait_s={rec.get('client_wait_s')}  "
          f"stalled={rec.get('stalled')!r}")
    check("the stall is reported as a TimeoutError",
          err == "TimeoutError", f"err={err!r}")
    check("the stall is attributed to the CLIENT side",
          rec.get("stalled") == "client",
          f"stalled={rec.get('stalled')!r}")
    check("the client wait is logged apart from the upstream read",
          (rec.get("client_wait_s") or 0) > (rec.get("max_wait_s") or 0),
          f"client_wait_s={rec.get('client_wait_s')} vs "
          f"max_wait_s={rec.get('max_wait_s')}")
    # `client_kb` is what the CLIENT received, against `kb` which is what the
    # far end sent. It exists to answer the one question the pane's
    # `200 cut (SSLEOFError) [client stalled]` line cannot: did the client
    # leave with the whole answer, or half of it? 166 of those exist in the log
    # with `client_wait_s` of 0.0 -- the write never waited, so the client had
    # already gone -- and `cut=True` cannot be read either way without this.
    # A field that answers that has to be exact, so it is checked against the
    # client's own count. Fails on the old code, where the key is absent.
    check("client_kb records what the client actually received",
          abs((rec.get("client_kb") or -1) - client.got / 1024) < 1.0,
          f"client_kb={rec.get('client_kb')!r} against the client's own count "
          f"of {client.got / 1024:.1f} KB")
    check("the LANE IS NOT CHARGED for the client's stall",
          charged == [],
          f"charged={charged} -- a healthy lane would be retired")
    # The error path runs AFTER the head was parsed, so the record must carry
    # the real status. It used to hardcode 0, which counted a delivered 200 as
    # a total miss -- 151 such calls on the real log, all of them truncated
    # answers that looked like connection failures.
    check("the error path reports the status the head carried",
          rec.get("status") == 200,
          f"status={rec.get('status')!r} -- a delivered 200 counted as a miss")

    print("\n=== but a genuine upstream stall still charges it ===")

    def case_upstream_mute(c):
        socks_hello(c)
        # never send anything: the upstream is the one stalling
        time.sleep(60)

    class FineClient:
        """A client that reads everything."""

        def sendall(self, data):
            return None

        def settimeout(self, t):
            return None

    err2, rec2, charged2 = run(start_upstream(case_upstream_mute), FineClient())
    print(f"  err={err2!r}  stalled={rec2.get('stalled')!r}  "
          f"charged={charged2}")
    check("an upstream stall is attributed to the upstream",
          rec2.get("stalled") == "upstream",
          f"stalled={rec2.get('stalled')!r}")
    check("an upstream stall still charges the lane",
          charged2 == [1],
          f"charged={charged2} -- a mute lane would escape the tally")

    print()
    if FAILS:
        print("CLIENT STALL IS NOT A LANE FAULT: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("CLIENT STALL IS NOT A LANE FAULT: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
