"""A committed stream must not be cut for pausing to think.

The owner's report:

    01:14:38  #3.1 failed (TimeoutError) 62.1 KB in 184.1s  first 13.84s

The stream had committed -- `first_event_s` 14.16s, bytes on the wire -- and
then went quiet. The relay cut it, the attempt was not retryable (the client
had already seen bytes), so `handle_conn` returned and the client's connection
closed mid-answer. That is the failure: not a failed request, a TRUNCATED one.

The ceiling was firing on the natural pause of a reasoning model. Measured
over the log, the 11 committed stalls delivered 48-62 KB at 0.3-1.8 KB/s, and
healthy streams send their first event within 4.28s of the head -- so a gap
after commit is a think-pause, not a dead socket.

The fix splits the ceiling at the moment of commit:

    before commit  _READ_TIMEOUT (20s)       retry is free, fail fast
    after commit   _STREAM_IDLE_TIMEOUT      no retry possible, so cutting
                   (1800s)                   only destroys the answer

A genuinely closed peer is caught by EOF either way, so the generous
post-commit window costs nothing on the failure that actually happens.

This drives the shipped `_roundtrip` against real sockets. It fails on the old
code: with one ceiling for both phases, case A dies where it must survive.
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
EVENT = b'data: {"type":"response.reasoning.delta","delta":"xxxx"}\n\n'


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


def run(port, pre=2.0, post=8.0):
    """Drive the shipped `_roundtrip`; returns (err, secs, kb, record)."""
    real_ctx = ssl.create_default_context
    old_pre = mitm._READ_TIMEOUT
    old_post = mitm._STREAM_IDLE_TIMEOUT
    old_first = mitm._FIRST_BYTE_TIMEOUT

    class RawCtx:
        """The fake upstream speaks cleartext; skip the TLS wrap."""

        def wrap_socket(self, sock, server_hostname=None):  # noqa: ANN001
            return sock

    ssl.create_default_context = lambda *a, **k: RawCtx()
    mitm._READ_TIMEOUT = pre
    mitm._STREAM_IDLE_TIMEOUT = post
    mitm._FIRST_BYTE_TIMEOUT = 8.0

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

    class Tor:
        def note_timeout(self, lane):
            return ""

    tap = Tap()
    events = []
    try:
        t0 = time.time()
        err, status, _h, _r = mitm._roundtrip(
            tap, Lane(), "opencode.ai", 443, "POST", "/zen/v1/responses",
            {}, b'{"model":"m"}', events.append, 1, 1, t0,
            type("R", (), {"tor": Tor()})())
        secs = time.time() - t0
    finally:
        ssl.create_default_context = real_ctx
        mitm._READ_TIMEOUT = old_pre
        mitm._STREAM_IDLE_TIMEOUT = old_post
        mitm._FIRST_BYTE_TIMEOUT = old_first
    end = [e for e in events if e.get("type") == "callend"]
    rec = end[-1] if end else {}
    return err, secs, len(tap.got), rec


def main():
    # Short windows so the proof runs in seconds; the RATIO is the claim.
    pre, post = 2.0, 8.0
    gap = 5.0                      # > pre, < post -- the owner's shape

    print("=== (A) committed, then pauses to think ===")
    print(f"  commit, then a {gap:.0f}s gap; pre-commit ceiling {pre}s, "
          f"post-commit {post}s")

    def case_pauses(c):
        socks_hello(c)
        c.sendall(HEAD)
        chunk(c, EVENT)            # commit
        time.sleep(gap)            # think-pause
        chunk(c, EVENT)            # resumes
        chunk(c, b'data: {"type":"response.completed"}\n\n')
        c.sendall(b"0\r\n\r\n")

    err, secs, sent, rec = run(start_upstream(case_pauses), pre, post)
    print(f"  result: err={err!r} wall={secs:.1f}s bytes={sent} "
          f"max_wait_s={rec.get('max_wait_s')}")
    check("a committed stream survives a gap longer than the pre-commit ceiling",
          err == "" and secs > gap,
          f"err={err!r} wall={secs:.1f}s -- the answer was truncated")
    check("the answer reached the client intact",
          sent > len(HEAD) + 2 * len(EVENT),
          f"only {sent} bytes reached the client")
    check("the longest wait is reported, so a ceiling can be set from data",
          isinstance(rec.get("max_wait_s"), (int, float))
          and rec.get("max_wait_s", 0) >= gap * 0.8,
          f"max_wait_s={rec.get('max_wait_s')!r} for a {gap:.0f}s gap")

    print("\n=== (B) nothing at all before commit still fails fast ===")
    print(f"  no bytes ever; pre-commit ceiling {pre}s")

    def case_mute(c):
        socks_hello(c)
        time.sleep(60)

    err, secs, sent, rec = run(start_upstream(case_mute), pre, post)
    print(f"  result: err={err!r} wall={secs:.1f}s bytes={sent}")
    check("a pre-commit stall still dies at the pre-commit ceiling",
          err == "TimeoutError" and secs < post,
          f"err={err!r} wall={secs:.1f}s -- should have failed fast, not "
          f"waited out the post-commit window")

    print("\n=== (C) a peer that closes is caught by EOF, not by the ceiling ===")
    print("  commit, then the far end hangs up")

    def case_closes(c):
        socks_hello(c)
        c.sendall(HEAD)
        chunk(c, EVENT)
        time.sleep(0.3)
        c.close()                  # real close, not silence

    err, secs, sent, rec = run(start_upstream(case_closes), pre, post)
    print(f"  result: err={err!r} wall={secs:.1f}s status={rec.get('status')}")
    check("a closed peer is not mistaken for a stall",
          err != "TimeoutError" and secs < post,
          f"err={err!r} wall={secs:.1f}s -- EOF should end it at once")
    # The head was a 200 and bytes reached the client, so the record must say
    # 200. It used to hardcode 0 on every error path, which counted a
    # truncated answer as a total miss -- 151 such calls on the real log.
    check("a truncated 200 is recorded as 200, not as a failure",
          rec.get("status") == 200,
          f"status={rec.get('status')!r} -- a delivered 200 counted as a miss")

    print("\n=== (D) the window is a ceiling, not a total budget ===")
    print(f"  many small gaps, total far past the {post}s window")

    def case_dribbles(c):
        socks_hello(c)
        c.sendall(HEAD)
        chunk(c, EVENT)
        for _ in range(10):
            time.sleep(1.0)        # each gap under post
            chunk(c, EVENT)
        c.sendall(b"0\r\n\r\n")

    err, secs, sent, rec = run(start_upstream(case_dribbles), pre, post)
    print(f"  result: err={err!r} wall={secs:.1f}s bytes={sent} "
          f"max_wait_s={rec.get('max_wait_s')}")
    check("total duration past the window does not end the stream",
          err == "" and secs > post,
          f"err={err!r} wall={secs:.1f}s -- treated as a budget")

    print()
    if FAILS:
        print("COMMITTED STREAM SURVIVES: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("COMMITTED STREAM SURVIVES: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
