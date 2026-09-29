"""A tunnel the far end asked to close must not go back into the pool."""
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


def chunk(body):
    return b"%x\r\n" % len(body) + body + b"\r\n"


EVENT = b'data: {"type":"response.output_text.delta","delta":"x"}\n\n'


def response(extra_headers=b"", framing="chunked"):
    head = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n" +
            extra_headers)
    if framing == "chunked":
        head += b"transfer-encoding: chunked\r\n"
    else:
        head += b"content-length: %d\r\n" % len(EVENT)
    head += b"\r\n"
    if framing == "chunked":
        return head + chunk(EVENT) + b"0\r\n\r\n"
    return head + EVENT


class Upstream:
    """A pooled upstream whose reads come from a canned byte string."""

    def __init__(self, data):
        self.buf = data
        self.sent = b""

    def settimeout(self, t):
        return None

    def sendall(self, data):
        self.sent += data

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
        self.given = []

    def take(self, lane):
        return self.up, None

    def give(self, lane, up, uf):
        self.given.append(up)


class Client:
    def __init__(self):
        self.got = b""

    def sendall(self, data):
        self.got += data

    def settimeout(self, t):
        return None


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


def drive(payload, framing="chunked"):
    up = Upstream(payload)
    pool = Pool(up)
    relay = type("R", (), {"tor": Tor(), "tunnels": pool})()
    events = []
    old = mitm._READ_TIMEOUT
    mitm._READ_TIMEOUT = 2.0
    try:
        out = mitm._roundtrip(Client(), Lane(), "opencode.ai", 443, "POST",
                              "/zen/v1/responses",
                              {"accept": "text/event-stream"}, b'{"model":"m"}',
                              events.append, 1, 1, time.time(), relay,
                              charge_timeout=False)
    finally:
        mitm._READ_TIMEOUT = old
    return out, events, pool


def main():
    print("=== the far end asks for close ===")
    (out, events, pool) = drive(response(b"connection: close\r\n"))
    print(f"  pooled={len(pool.given)}  err={out[0]!r}  status={out[1]}")
    check("the response still completes", out[0] == "" and out[1] == 200,
          f"err={out[0]!r} status={out[1]}")
    check("and the tunnel is NOT pooled",
          pool.given == [],
          "a connection the far end asked to close went back into the pool; "
          "the next reuse meets an EOF")

    print("\n=== the far end says nothing about close ===")
    (out, events, pool) = drive(response())
    print(f"  pooled={len(pool.given)}  err={out[0]!r}")
    check("the tunnel IS pooled", len(pool.given) == 1,
          "reuse stopped working for ordinary keep-alive responses")

    print("\n=== keep-alive spelled out ===")
    (out, events, pool) = drive(response(b"connection: keep-alive\r\n"))
    print(f"  pooled={len(pool.given)}")
    check("still pooled", len(pool.given) == 1, str(len(pool.given)))

    print("\n=== a malformed Content-Length from the far end ===")
    payload = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
               b"content-length: not-a-number\r\n\r\n" + EVENT)
    raised = None
    try:
        out, events, pool = drive(payload, framing="content-length")
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    print(f"  raised={raised!r}")
    check("it does not escape the handler",
          raised is None,
          f"{type(raised).__name__} escaped _roundtrip -- the MITM thread dies "
          f"and the client hangs on a connection nobody closes")
    if raised is None:
        check("the call fails instead of hanging",
              out[0] == "ValueError",
              f"err={out[0]!r}")

    print()
    if FAILS:
        print("CLOSE HONOURED: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("CLOSE HONOURED: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
