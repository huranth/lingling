"""The 503/504 session, proven offline."""
import io
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import mitm  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    if not ok:
        FAILS.append(name)


# --------------------------------------------------------------------------

BODY = b'{"model":"muse-spark-1.3-contributor-free"}'
REQ = (b"POST /zen/v1/responses HTTP/1.1\r\n"
       b"host: opencode.ai\r\ncontent-type: application/json\r\n"
       b"content-length: %d\r\n\r\n" % len(BODY) + BODY)

S503 = (b"HTTP/1.1 503 Service Unavailable\r\n"
        b"content-type: application/json\r\ncontent-length: 38\r\n\r\n"
        b'{"error":"edge is unwell, retry soon"}')
S504 = (b"HTTP/1.1 504 Gateway Timeout\r\n"
        b"content-type: application/json\r\ncontent-length: 26\r\n\r\n"
        b'{"error":"edge timed out"}')
S429 = (b"HTTP/1.1 429 Too Many Requests\r\n"
        b"content-type: application/json\r\nretry-after: 12373\r\n"
        b"content-length: 38\r\n\r\n"
        b'{"error":"exit is limited, come back"}')
S200 = (b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
        b"content-length: 9\r\n\r\n" + b'{"x":"y"}')

SSE_200 = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
           b"transfer-encoding: chunked\r\n\r\n"
           + b'%x\r\n' % len(b'data: {"type":"response.completed"}\n\n')
           + b'data: {"type":"response.completed"}\n\n' + b"0\r\n\r\n")


def chunk(data):
    return b"%x\r\n" % len(data) + data + b"\r\n"


class FakeSock:
    """A dialled upstream delivering one canned response."""

    def __init__(self, response):
        self._r = io.BytesIO(response)
        self.sent = b""
        self.closed = False

    def settimeout(self, _t):
        pass

    def connect(self, _a):
        pass

    def sendall(self, d):
        self.sent += d

    def makefile(self, _m):
        return self._r

    def close(self):
        self.closed = True


class BoomSock(FakeSock):
    """Delivers nothing; the TLS layer dies the way a cold circuit does."""

    def __init__(self):
        super().__init__(b"")
        import ssl as _ssl
        self._err = _ssl.SSLEOFError("cut")

    def makefile(self, _m):
        import ssl as _ssl

        class Boom:
            def read(_s, _n):
                raise self._err

            def readline(_s):
                raise self._err

        return Boom()


class FakeCtx:
    def wrap_socket(self, sock, server_hostname=None):
        return sock


class ServeClient:
    """A client socket carrying one canned request, for driving `_serve`."""

    def __init__(self, head=REQ):
        self._r = io.BytesIO(head)
        self.got = b""

    def makefile(self, _m):
        return self._r

    def sendall(self, d):
        self.got += d


class RoundtripClient:
    def __init__(self):
        self.got = b""

    def sendall(self, d):
        self.got += d

    def settimeout(self, _t):
        pass


class FakeTor:
    def __init__(self):
        self.limited = []

    def note_limited(self, lane, retry_after=0.0):
        self.limited.append((lane.index, retry_after))
        lane.limited_until = mitm.time.time() + (retry_after or 600.0)

    def note_result(self, country, status):
        pass

    def note_timeout(self, lane):
        return ""


class FakeLane:
    def __init__(self, index):
        self.index = index
        self.exit_country = "de"
        self.exit_ip = f"1.2.3.{index}"
        self.socks_port = 9050 + index
        self.lock = threading.Lock()
        self.active = 0
        self.limited_until = 0.0


class FakeRelay:
    def __init__(self, responses, n_lanes=3):
        self.responses = list(responses)
        self.dials = []
        self.refused = []
        self.tor = FakeTor()
        self.tunnels = None
        self.lanes = [FakeLane(i + 1) for i in range(n_lanes)]

    def pick_lane(self, exclude=None):
        for lane in self.lanes:
            if not exclude or lane.index not in exclude:
                return lane
        return None

    def report_refused(self, lane, status):
        self.refused.append((lane.index, status))

    def any_unlimited(self, exclude):
        # every lane is limited, so the pool is
        return False


_real = (mitm.time, mitm.netutil.socks5_open, mitm.ssl.create_default_context,
         mitm.socket.socket)


def dial_from(responses, relay):
    def dial(*_a, **_k):
        r = responses.pop(0) if responses else None
        sock = BoomSock() if r is None else FakeSock(r)
        relay.dials.append(sock)
        return sock
    return dial


def drive_roundtrip(response, pool=None):
    """One real `_roundtrip` against a canned upstream."""
    events = []
    client = RoundtripClient()
    relay = FakeRelay([])
    relay.tunnels = pool
    lane = FakeLane(3)
    mitm.netutil.socks5_open = lambda sock, host, port, cred=None: None
    mitm.ssl.create_default_context = lambda: FakeCtx()
    mitm.socket.socket = dial_from([response], relay)
    try:
        out = mitm._roundtrip(
            client, lane, "opencode.ai", 443, "POST", "/zen/v1/responses",
            {"accept": "text/event-stream"}, BODY, events.append, 2, 1,
            mitm.time.time(), relay)
    finally:
        _restore()
    return out, events, client, relay


def serve(responses, n_lanes=3):
    """One real `_serve` request against canned upstream responses."""
    events = []
    client = ServeClient()
    relay = FakeRelay(responses, n_lanes)
    mitm.netutil.socks5_open = lambda sock, host, port, cred=None: None
    mitm.ssl.create_default_context = lambda: FakeCtx()
    mitm.socket.socket = dial_from(relay.responses, relay)
    try:
        mitm._serve(client, "opencode.ai", 443, 7, events.append, relay)
    finally:
        _restore()
    return client, events, relay


def _restore():
    (mitm.time, mitm.netutil.socks5_open,
     mitm.ssl.create_default_context, mitm.socket.socket) = _real


print("\n=== A. a held 503 carries its head ===")
(out, events, _c, _r) = drive_roundtrip(S503)
err, status, held, retryable = out
check("503 reported", status == 503 and err == "", f"{err!r} {status}")
check("not retryable at this level", retryable is False, str(retryable))
check("the held buffer starts with the status line",
      held.startswith(b"HTTP/1.1 503"), repr(held[:40]))
check("and carries the body after the head",
      b"edge is unwell" in held, repr(held[:80]))
check("the callend names the far end's error",
      events[-1].get("note") == '{"error":"edge is unwell, retry soon"}',
      repr(events[-1].get("note")))
check("nothing reached the client", _c.got == b"", repr(_c.got[:40]))

print("\n=== B. a held 504 and a held 429 do the same ===")
(out504, events504, _c, _r) = drive_roundtrip(S504)
check("504 held with its head",
      out504[2].startswith(b"HTTP/1.1 504"), repr(out504[2][:40]))
check("the 504 callend names the error",
      events504[-1].get("note") == '{"error":"edge timed out"}',
      repr(events504[-1].get("note")))
(out429, events429, _c, _r) = drive_roundtrip(S429)
check("429 held with its head",
      out429[2].startswith(b"HTTP/1.1 429"), repr(out429[2][:40]))
check("the 429 callend names the error",
      events429[-1].get("note") == '{"error":"exit is limited, come back"}',
      repr(events429[-1].get("note")))

print("\n=== C. a 200 carries the note key, empty ===")
(out200, events200, _c, _r) = drive_roundtrip(SSE_200)
check("200 streams as before", out200[1] == 200, str(out200[1]))
check("the note key is present and empty", events200[-1].get("note") == "",
      repr(events200[-1].get("note")))

print("\n=== D. two 5xx attempts, then the far end's own 503 is delivered ===")
(client, events, relay) = serve([S503, S503], n_lanes=5)
ends = [e for e in events if e.get("type") == "callend"]
check("it stopped at two attempts", len(relay.dials) == 2,
      str(len(relay.dials)))
check("both attempts were 503", [e["status"] for e in ends] == [503, 503],
      str([e["status"] for e in ends]))
check("the client got the far end's own 503, head first",
      client.got.startswith(b"HTTP/1.1 503"), repr(client.got[:40]))
check("with the body after it", b"edge is unwell" in client.got,
      repr(client.got[:80]))
check("no invented 502 anywhere", b"502" not in client.got,
      repr(client.got[:40]))

print("\n=== E. a 5xx then a 200 is invisible to the client ===")
(client, events, relay) = serve([S503, SSE_200], n_lanes=3)
check("two attempts", len(relay.dials) == 2, str(len(relay.dials)))
check("the client got the 200", client.got.startswith(b"HTTP/1.1 200"),
      repr(client.got[:40]))

print("\n=== F. a transport error between 5xx attempts keeps the held 503 ===")
(client, events, relay) = serve([S503, None, SSE_200], n_lanes=4)
ends = [e for e in events if e.get("type") == "callend"]
check("three attempts ran", len(relay.dials) == 3, str(len(relay.dials)))
check("the middle attempt died in transport",
      ends[1]["status"] == 0 and ends[1]["err"] != "" and ends[1]["cut"],
      repr(ends[1]))
check("the client still got the 200",
      client.got.startswith(b"HTTP/1.1 200"), repr(client.got[:40]))

print("\n=== G. a 429 pool exhausted delivers the 429, not a 502 ===")
(client, events, relay) = serve([S429], n_lanes=2)
check("the client got the far end's own 429",
      client.got.startswith(b"HTTP/1.1 429"), repr(client.got[:40]))
check("with the retry-after it carried",
      b"retry-after: 12373" in client.got, repr(client.got[:120]))
check("no invented 502", b"502" not in client.got, repr(client.got[:40]))

print("\n=== H. a plain 500 still reaches the client untouched ===")
S500 = (b"HTTP/1.1 500 Internal Server Error\r\n"
        b"content-type: application/json\r\ncontent-length: 9\r\n\r\n"
        b'{"e":"x"}')
(client, events, relay) = serve([S500], n_lanes=3)
check("one attempt only", len(relay.dials) == 1, str(len(relay.dials)))
check("the 500 went straight out",
      client.got.startswith(b"HTTP/1.1 500"), repr(client.got[:40]))
check("its body reached the client verbatim", b'{"e":"x"}' in client.got,
      repr(client.got[:80]))
check("no note needed -- nothing was thrown away",
      events[-1].get("note") == "", repr(events[-1].get("note")))


print()
if FAILS:
    print("5XX DELIVERY: FAILED")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("5XX DELIVERY: CONFIRMED")
return_code = 0
sys.exit(return_code)
