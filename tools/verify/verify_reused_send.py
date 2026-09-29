"""A reused tunnel must have its window armed BEFORE it is used.

Found in the log, and only visible because the wait fields were logged:

    TimeoutError  secs=20.0  reused=True  max_wait_s=0.0

A 20-second timeout with `max_wait_s` of 0 means no READ ever waited -- the
wait was a SEND. The cause is ordering: `up.sendall(out_head + body)` ran
BEFORE `up.settimeout(_READ_TIMEOUT)`, so on a pooled socket it used whatever
timeout the previous use happened to leave behind.

That value is not benign. `_flush` arms `_STREAM_IDLE_TIMEOUT` (300s) the
moment a stream commits, and the socket is then handed back to the pool -- so
a reused tunnel could block for five minutes on a send, or fail at 20s,
depending on which phase ran last. Either way it was an accident of history
rather than a decision.

WHY THIS TEST RECORDS CALL ORDER RATHER THAN BLOCKING A REAL SOCKET. The
obvious test -- fill the buffers so `sendall` blocks -- does not work here: a
socketpair on this platform absorbs a 4 MB send in 0.00s, so there is no block
to observe. (Measured: a LOOP of 64 KB sends blocks after ~450 KB, a single
large `sendall` does not.) So the invariant is asserted directly: the socket
must be told its window before it is asked to send anything.

It fails on the old code, where `sendall` is the first call the socket sees.
"""
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


class RecordingSock:
    """A pooled upstream socket that records the order of what it is asked."""

    def __init__(self, block_for=0.3):
        self.calls = []
        self.block_for = block_for

    def settimeout(self, t):
        self.calls.append(("settimeout", t))

    def sendall(self, data):
        self.calls.append(("sendall", len(data)))
        if self.block_for:
            time.sleep(self.block_for)   # a send that has to wait
            raise TimeoutError("timed out")

    def close(self):
        self.calls.append(("close", None))

    def makefile(self, mode):
        return None


class FakePool:
    """Hands back one prepared tunnel, so the reuse path is what runs."""

    def __init__(self, up, uf):
        self.up, self.uf = up, uf

    def take(self, lane):
        return self.up, self.uf

    def give(self, lane, up, uf):
        return None


def main():
    print("=== a pooled socket that carries a stale timeout ===")

    sock = RecordingSock(block_for=0.3)
    pool = FakePool(sock, None)

    class Lane:
        index = 1
        exit_country = "de"
        exit_ip = "1.2.3.4"
        socks_port = 0
        lock = threading.Lock()
        active = 0

    class Tor:
        def note_timeout(self, lane):
            return ""

    relay = type("R", (), {"tor": Tor(), "tunnels": pool})()

    class Tap:
        def sendall(self, data):
            return None

        def settimeout(self, t):
            return None

    events = []
    old = mitm._READ_TIMEOUT
    mitm._READ_TIMEOUT = 2.0
    try:
        t0 = time.time()
        err, _status, _h, _r = mitm._roundtrip(
            Tap(), Lane(), "opencode.ai", 443, "POST", "/zen/v1/responses",
            {}, b'{"model":"m"}', events.append, 1, 1, t0, relay,
            charge_timeout=False)
        secs = time.time() - t0
    finally:
        mitm._READ_TIMEOUT = old

    print(f"  calls the socket saw: {sock.calls}")
    print(f"  result: err={err!r} wall={secs:.2f}s")

    kinds = [c[0] for c in sock.calls]
    check("the socket was used at all", "sendall" in kinds, str(kinds))
    check("its window is armed BEFORE it is asked to send",
          "settimeout" in kinds
          and kinds.index("settimeout") < kinds.index("sendall"),
          f"order was {kinds} -- the send inherited a stale timeout")
    # The send arms its OWN window, not the read window. All three used to be
    # one constant, which is now 1800s -- so a stuck send would have hung for
    # half an hour. Compared against `_SEND_TIMEOUT` only: this test lowers
    # `_READ_TIMEOUT` to make the ceiling testable, so the two are not
    # comparable here. The shipped values are dial 30s, send 30s, read 1800s.
    # The send is NOT a total budget for the upload: a socket timeout on
    # `sendall` is per wait-for-writable, so 30s means 30s with zero bytes of
    # progress. Raising it to 300s was tried and reverted -- it cannot turn a
    # stalled circuit into a success, only delay the same failure tenfold.
    check("the send arms its own window, not the read window",
          ("settimeout", mitm._SEND_TIMEOUT) in sock.calls,
          f"armed {[c for c in sock.calls if c[0] == 'settimeout']} "
          f"-- expected {mitm._SEND_TIMEOUT}s")

    end = [e for e in events if e.get("type") == "callend"]
    rec = end[-1] if end else {}
    check("the blocked send is visible in max_wait_s",
          (rec.get("max_wait_s") or 0) >= 0.25,
          f"max_wait_s={rec.get('max_wait_s')!r} -- the wait was invisible")
    check("the stall is attributed to the upstream",
          rec.get("stalled") == "upstream",
          f"stalled={rec.get('stalled')!r}")

    print()
    if FAILS:
        print("REUSED SEND WINDOW: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("REUSED SEND WINDOW: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
