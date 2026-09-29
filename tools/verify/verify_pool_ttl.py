"""An idle tunnel must outlive one round-robin lap, or reuse is zero."""
import socket
import sys
import threading
import time
import time as clock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import mitm  # noqa: E402

FAILS = []

#: the owner's measured same-lane lap, p75
LAP_S = 150.0

_real_monotonic = clock.monotonic
_skew = 0.0


def _now():
    """`time.monotonic` with the test's offset applied."""
    return _real_monotonic() + _skew


clock.monotonic = _now


def check(name, ok, detail=""):
    """Detail is the failure reason, so only show it when it failed."""
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    if not ok:
        FAILS.append(name)


class Lane:
    index = 1
    exit_country = "de"
    exit_ip = "1.2.3.4"
    socks_port = 0
    lock = threading.Lock()
    active = 0


def pair():
    """A connected socket that is open and idle, so `_peer_closed` says no."""
    a, b = socket.socketpair()
    return a, b


def idle_for(pool, seconds):
    """Give the pool a tunnel, wait `seconds`, take it back."""
    global _skew
    a, b = pair()
    lane = Lane()
    _skew = 0.0
    pool.give(lane, a, None)
    _skew = seconds
    got = pool.take(lane)
    _skew = 0.0
    for s in (a, b):
        try:
            s.close()
        except OSError:
            pass
    return got


# --------------------------------------------------------------------------

EVENT = b'data: {"type":"response.output_text.delta","delta":"x"}\n\n'


def chunk(body):
    return b"%x\r\n" % len(body) + body + b"\r\n"


class Upstream:
    """A pooled upstream whose reads come from a canned byte string."""

    def __init__(self, data):
        self.buf = data

    def settimeout(self, t):
        return None

    def sendall(self, data):
        return None

    def makefile(self, mode):
        return self

    def readline(self):
        i = self.buf.find(b"\n")
        if i < 0:
            out, self.buf = self.buf, b""
            return out
        out, self.buf = self.buf[:i + 1], self.buf[i + 1:]
        return out

    def read(self, n):
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def close(self):
        return None


class Pool:
    def __init__(self, up):
        self.up = up

    def take(self, lane):
        return self.up, None

    def give(self, lane, up, uf):
        return None


class Client:
    def sendall(self, data):
        return None

    def settimeout(self, t):
        return None


class Tor:
    def note_timeout(self, lane):
        return ""


def drive(payload):
    """One `_roundtrip`, returning the callend it emitted."""
    relay = type("R", (), {"tor": Tor(), "tunnels": Pool(Upstream(payload))})()
    events = []
    old = mitm._READ_TIMEOUT
    mitm._READ_TIMEOUT = 2.0
    try:
        mitm._roundtrip(Client(), Lane(), "opencode.ai", 443, "POST",
                        "/zen/v1/responses",
                        {"accept": "text/event-stream"}, b'{"model":"m"}',
                        events.append, 1, 1, time.time(), relay,
                        charge_timeout=False)
    finally:
        mitm._READ_TIMEOUT = old
    ends = [e for e in events if e.get("type") == "callend"]
    return ends[-1] if ends else {}


def main():
    print("=== A. the shipped TTL survives a lap ===")
    pool = mitm.TunnelPool()          # shipped default, not an argument
    got = idle_for(pool, LAP_S)
    print(f"  shipped TTL={mitm._KEEPALIVE_S}s  idle={LAP_S}s  "
          f"reused={'yes' if got else 'no'}")
    check("a tunnel idle for a full lap is still usable",
          got is not None,
          f"the tunnel was dropped after {LAP_S}s, but that is only the p75 of "
          f"the owner's same-lane lap -- reuse is 0% on his traffic")
    check("and the shipped TTL is what was tested",
          mitm.TunnelPool().ttl == mitm._KEEPALIVE_S,
          f"ttl={mitm.TunnelPool().ttl} vs _KEEPALIVE_S={mitm._KEEPALIVE_S}")

    print("\n=== B. staleness is still bounded ===")
    pool = mitm.TunnelPool()
    got = idle_for(pool, mitm._KEEPALIVE_S + 1)
    print(f"  idle={mitm._KEEPALIVE_S + 1}s  reused={'yes' if got else 'no'}")
    check("a tunnel past the TTL is dropped",
          got is None,
          "nothing bounds how stale a pooled socket can get")

    print("\n=== C. the log can tell a send stall from a read stall ===")
    head = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
            b"connection: close\r\ntransfer-encoding: chunked\r\n\r\n")
    rec = drive(head + chunk(EVENT) + b"0\r\n\r\n")
    print(f"  success callend: send_s={rec.get('send_s')!r} "
          f"peer_close={rec.get('peer_close')!r}")
    check("a success carries send_s",
          isinstance(rec.get("send_s"), (int, float)),
          f"send_s={rec.get('send_s')!r} -- max_wait_s conflates send and read")
    check("a success records the far end's close",
          rec.get("peer_close") is True,
          f"peer_close={rec.get('peer_close')!r} for a `connection: close` "
          f"response -- whether the far end closes decides if ANY tunnel can "
          f"be pooled")

    bad = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
           b"content-length: not-a-number\r\n\r\n" + EVENT)
    rec = drive(bad)
    print(f"  error callend:   send_s={rec.get('send_s')!r} "
          f"peer_close={rec.get('peer_close')!r} err={rec.get('err')!r}")
    check("an error carries send_s too",
          isinstance(rec.get("send_s"), (int, float)),
          f"send_s={rec.get('send_s')!r} -- the send is what times out")
    check("an error carries peer_close too",
          rec.get("peer_close") is False,
          f"peer_close={rec.get('peer_close')!r} -- a partial key undercounts "
          f"silently and reads as evidence")

    print()
    if FAILS:
        print("POOL TTL: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("POOL TTL: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
