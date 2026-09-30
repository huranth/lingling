"""Offline proof of what a `callend` event reports about one model call."""
import io
import json
import os
import ssl
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lingling import mitm, proof  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (("   " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


class Slow:
    """Hands out one byte per read and counts the reads."""

    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.reads = 0

    def read(self, n=1):
        self.reads += 1
        ch = self.data[self.pos:self.pos + n]
        self.pos += len(ch)
        return ch


print("\n=== A. _read_head stamps byte one, not head completion ===")
f = Slow(b"HTTP/1.1 200 OK\r\nX: 1\r\n\r\nBODY")
seen = []
head = mitm._read_head(f, on_first=lambda: seen.append(f.reads))
check("head read whole", head == b"HTTP/1.1 200 OK\r\nX: 1\r\n\r\n", repr(head))
check("stamped exactly once", len(seen) == 1, f"calls={len(seen)}")
check("stamped on the FIRST read", seen == [1], f"reads_at_stamp={seen}")
check("head itself took many reads", f.reads > 5, f"reads={f.reads}")

print("\n=== B. no stamp when nothing ever arrives ===")
seen2 = []
check("empty stream returns None",
      mitm._read_head(Slow(b""), on_first=lambda: seen2.append(1)) is None)
check("never stamped", seen2 == [], f"calls={len(seen2)}")

print("\n=== C. bare call still works (callback is optional) ===")
head3 = mitm._read_head(Slow(b"GET / HTTP/1.1\r\n\r\n"))
check("no-callback call reads head", head3 == b"GET / HTTP/1.1\r\n\r\n",
      repr(head3))

print("\n=== D. _lat offsets ===")
check("both reached", mitm._lat(100.0, 100.5, 101.25)
      == {"first_byte_s": 0.5, "first_event_s": 1.25},
      str(mitm._lat(100.0, 100.5, 101.25)))
check("neither reached -> 0, not null",
      mitm._lat(100.0, None, None)
      == {"first_byte_s": 0, "first_event_s": 0},
      str(mitm._lat(100.0, None, None)))
check("byte reached, event not",
      mitm._lat(100.0, 100.25, None)
      == {"first_byte_s": 0.25, "first_event_s": 0},
      str(mitm._lat(100.0, 100.25, None)))


class Clock:
    """Ticks per call so offsets cannot round away to zero."""
    now = 1000.0

    @staticmethod
    def time():
        Clock.now += 0.05
        return Clock.now

    @staticmethod
    def monotonic():
        return Clock.now


class FakeSock:
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


class DeadSock(FakeSock):
    def connect(self, _a):
        raise OSError("no route to lane")


class BoomReader:
    """Delivers a head, then dies the way a cold Tor circuit does."""

    def __init__(self, head, err):
        self.buf = io.BytesIO(head)
        self.err = err

    def read(self, n=1):
        got = self.buf.read(n)
        if got:
            return got
        raise self.err

    def readline(self):
        got = self.buf.readline()
        if got:
            return got
        raise self.err


class BoomSock(FakeSock):
    def __init__(self, response, err=None):
        super().__init__(b"")
        self._boom = BoomReader(response, err or ConnectionResetError("cut"))

    def makefile(self, _m):
        return self._boom


class FakeCtx:
    def wrap_socket(self, sock, server_hostname=None):
        return sock


class FakeClient:
    def __init__(self):
        self.got = b""

    def sendall(self, d):
        self.got += d


class ReqClient:
    """A client socket carrying one canned request, for driving `_serve`."""

    def __init__(self, head):
        self._r = io.BytesIO(head)
        self.got = b""

    def makefile(self, _m):
        return self._r

    def sendall(self, d):
        self.got += d


class FakeTor:
    """Stands in for TorManager's one recorder."""

    def __init__(self):
        self.limited_calls = []

    def note_limited(self, lane, retry_after=0.0):
        self.limited_calls.append((lane.index, retry_after))
        lane.limited_until = mitm.time.time() + (retry_after or 600.0)


class FakeRelay:
    def __init__(self):
        self.refused = []
        self.dials = []
        self.tunnels = None
        self.tor = FakeTor()

    def report_refused(self, lane, status):
        self.refused.append(status)


class FakePool:
    """A TunnelPool stand-in: hands out one prepared tunnel, then nothing."""

    def __init__(self, entry=None):
        self.entry = entry
        self.given = []

    def take(self, lane):
        entry, self.entry = self.entry, None
        return entry

    def give(self, lane, up, fil):
        self.given.append((up, fil))


class FakeLane:
    index = 3
    exit_country = "de"
    exit_ip = "1.2.3.4"
    socks_port = 9050


def chunk(data):
    return b"%x\r\n" % len(data) + data + b"\r\n"


SSE = [
    b'event: response.created\ndata: {"type":"response.created"}\n\n',
    (b'event: response.output_item.added\ndata: '
     b'{"type":"response.output_item.added","item":{"type":"reasoning"}}\n\n'),
    b'event: response.output_text.delta\ndata: '
    b'{"type":"response.output_text.delta"}\n\n',
    b'event: response.completed\ndata: {"type":"response.completed"}\n\n',
]
RESP = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
        b"transfer-encoding: chunked\r\n\r\n"
        + b"".join(chunk(s) for s in SSE) + b"0\r\n\r\n")

#: the live 323.8 KB class: opencode's own static fetch, not a model call
_BIG = b'{"k":"v"},' * 40000
API_JSON = (b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
            + b"content-length: %d\r\n\r\n" % len(_BIG) + _BIG)

#: a plain small 200, also not a stream
PLAIN = (b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
         b"content-length: 9\r\n\r\n" + b'{"x":"y"}')

#: a stream that opens, ends cleanly, and never carries the model
SSE_EMPTY = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
             b"transfer-encoding: chunked\r\n\r\n"
             + chunk(b'event: ping\ndata: {}\n\n') + b"0\r\n\r\n")

#: a 429 buffers, so nothing is ever relayed or sniffed. The retry-after is
BUSY = (b"HTTP/1.1 429 Too Many Requests\r\ncontent-type: text/plain\r\n"
        b"retry-after: 12373\r\ncontent-length: 4\r\n\r\nbusy")

_real = (mitm.time, mitm.netutil.socks5_open, mitm.ssl.create_default_context,
         mitm.socket.socket)


def drive(response, sock_cls=FakeSock, t0=1000.0, pool=None, lane=None,
          socks_err=None):
    """Run one real _roundtrip against a canned upstream."""
    events = []
    client = FakeClient()
    relay = FakeRelay()
    relay.tunnels = pool
    lane = lane or FakeLane()

    def dial(*_a, **_k):
        sock = sock_cls(response)
        relay.dials.append(sock)
        return sock

    mitm.time = Clock
    mitm.netutil.socks5_open = lambda sock, host, port, cred=None: socks_err
    mitm.ssl.create_default_context = lambda: FakeCtx()
    mitm.socket.socket = dial
    try:
        out = mitm._roundtrip(
            client, lane, "opencode.ai", 443, "POST",
            "/zen/v1/responses", {"accept": "text/event-stream"},
            b'{"model":"m"}', events.append, 2, 1, t0, relay)
    finally:
        (mitm.time, mitm.netutil.socks5_open,
         mitm.ssl.create_default_context, mitm.socket.socket) = _real
    return out, events, client, relay


print("\n=== E. a real 200 stream carries both stamps ===")
(out, events, client, relay) = drive(RESP)
err, status, held, retryable = out
end = events[-1]
check("status 200", status == 200 and end["status"] == 200, f"status={status}")
check("no error", err == "" and end["err"] == "", f"err={err!r}")
check("no verdict field on a 200", "ghost" not in end, str(sorted(end)))
check("not cut", end["cut"] is False)
check("body reached the client", b"response.completed" in client.got,
      f"{len(client.got)} bytes")
check("first_byte_s present and > 0", end.get("first_byte_s", 0) > 0,
      str(end.get("first_byte_s")))
check("first_event_s present and > 0", end.get("first_event_s", 0) > 0,
      str(end.get("first_event_s")))
check("event lands at or after the first byte",
      end["first_event_s"] >= end["first_byte_s"],
      f'{end["first_byte_s"]}s -> {end["first_event_s"]}s')
check("both land inside the total",
      end["first_event_s"] <= end["secs"],
      f'{end["first_event_s"]}s <= {end["secs"]}s')
check("a healthy roundtrip blames no lane", relay.refused == [],
      str(relay.refused))

print("\n=== F. the first SSE event is stamped, whatever it is called ===")
THINK_ONLY = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
              b"transfer-encoding: chunked\r\n\r\n"
              + chunk(b'event: response.output_item.added\ndata: '
                      b'{"type":"reasoning"}\n\n')
              + b"0\r\n\r\n")
(out2, events2, _c2, _r2) = drive(THINK_ONLY)
check("reasoning frame stamped the event",
      events2[-1].get("first_event_s", 0) > 0,
      str(events2[-1].get("first_event_s")))
# Reasoning-only is how a multi-block model thinks. It
check("a reasoning-only 200 is NOT judged",
      "ghost" not in events2[-1], str(sorted(events2[-1])))
check("it ended cleanly, so not cut", events2[-1]["cut"] is False)
check("and it blames no lane", _r2.refused == [], str(_r2.refused))

# The marker used to be the literal `"type":"reasoning"`,
SPACED = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
          b"transfer-encoding: chunked\r\n\r\n"
          + chunk(b'event: response.output_item.added\ndata: '
                  b'{"type": "reasoning", "id": "r1"}\n\n')
          + chunk(b'event: response.output_text.delta\ndata: '
                  b'{"type": "response.output_text.delta"}\n\n')
          + b"0\r\n\r\n")
(_o9, events9, _c9, _r9) = drive(SPACED)
check("a spaced event is stamped too",
      events9[-1].get("first_event_s", 0) > 0,
      str(events9[-1].get("first_event_s")))
check("and it lands at or after the first byte",
      events9[-1]["first_event_s"] >= events9[-1]["first_byte_s"],
      f'{events9[-1]["first_byte_s"]}s -> {events9[-1]["first_event_s"]}s')

print("\n=== G. dead upstream: keys still present as 0 ===")
(out3, events3, _c3, _r3) = drive(b"")
check("reported as upstream closed",
      events3[-1]["err"] == "upstream closed", repr(events3[-1]["err"]))
check("keys present as 0, not missing",
      events3[-1].get("first_byte_s") == 0
      and events3[-1].get("first_event_s") == 0, str(events3[-1]))

print("\n=== H. lane connect failure: keys still present as 0 ===")
(out4, events4, _c4, _r4) = drive(b"", sock_cls=DeadSock)
check("reported as OSError", events4[-1]["err"] == "OSError",
      repr(events4[-1]["err"]))
check("keys present as 0, not missing",
      events4[-1].get("first_byte_s") == 0
      and events4[-1].get("first_event_s") == 0, str(events4[-1]))

print("\n=== H2. a SOCKS5 refusal still reports an outcome ===")
# It used to return without emitting, so the
(_o14, events14, _c14, _r14) = drive(b"", socks_err="connection refused")
check("the refusal is logged, not swallowed",
      len(events14) == 1 and events14[-1]["err"] == "connection refused",
      str(events14[-1] if events14 else "nothing emitted"))
check("and it is not dressed up as a success",
      events14[-1]["status"] == 0 and events14[-1]["kb"] == 0,
      str(events14[-1]["status"]))

print("\n=== I. plain traffic is never judged as a model stream ===")
# This proxy also carries opencode's own fetches. GET
(_o5, events5, _c5, relay5) = drive(PLAIN)
check("a non-stream 200 carries no verdict", "ghost" not in events5[-1],
      str(sorted(events5[-1])))
check("and it is not reported as cut", events5[-1]["cut"] is False)
check("no model event was stamped", events5[-1]["first_event_s"] == 0,
      str(events5[-1]["first_event_s"]))
check("and the exit is not blamed", relay5.refused == [],
      str(relay5.refused))

(_o12, events12, _c12, relay12) = drive(API_JSON)
check("a ~350 KB static fetch carries no verdict either",
      "ghost" not in events12[-1], str(sorted(events12[-1])))
check("it still relays the whole body", events12[-1]["kb"] > 300,
      str(events12[-1]["kb"]))
check("and it blames nobody", relay12.refused == [], str(relay12.refused))

print("\n=== J. an empty stream is reported, and blames no lane ===")
# An empty stream used to pull the lane.
(_o6, events6, _c6, relay6) = drive(SSE_EMPTY)
check("an empty stream carries no verdict", "ghost" not in events6[-1],
      str(sorted(events6[-1])))
check("clean end, so not a cut", events6[-1]["cut"] is False)
check("and no lane is refused for it", relay6.refused == [],
      str(relay6.refused))

print("\n=== K. a 429 blames nothing here, and carries the server's reset ===")
(_o7, events7, _c7, relay7) = drive(BUSY)
check("429 carries no verdict", "ghost" not in events7[-1],
      str(sorted(events7[-1])))
check("429 not flagged cut", events7[-1]["cut"] is False)
check("429 blames no lane here", relay7.refused == [],
      str(relay7.refused))
check("429 records the far end's retry-after",
      events7[-1]["retry_after"] == 12373, str(events7[-1]["retry_after"]))

print("\n=== K2. the limit is per EXIT, so it is recorded on the lane ===")
# Live proof: identical probes through six exits in
lane_k = FakeLane()
lane_k.limited_until = 0.0
(_o13, events13, _c13, _r13) = drive(BUSY, lane=lane_k)
# drive() runs on the fake Clock, so the
check("the lane is marked limited", lane_k.limited_until > 0,
      str(lane_k.limited_until))
check("for at least the retry-after it was told",
      lane_k.limited_until >= 12373, f"limited_until={lane_k.limited_until}")
check("the event carries it too", events13[-1]["retry_after"] == 12373,
      str(events13[-1]["retry_after"]))

print("\n=== L. a pooled tunnel is reused and asked to stay open ===")# The SOCKS5 CONNECT plus TLS handshake is ~1.3s of Tor round trips (measured
# 546ms + 723ms warm), so a reused tunnel
seeded = FakeSock(RESP)
pool = FakePool((seeded, seeded._r))
(out8, events8, _c8, relay8) = drive(RESP, pool=pool)
check("no new tunnel was dialled", relay8.dials == [], str(relay8.dials))
check("the pooled tunnel carried the request",
      b"POST /zen/v1/responses" in seeded.sent, repr(seeded.sent[:44]))
check("the upstream was asked to keep it open",
      b"connection: keep-alive" in seeded.sent, repr(seeded.sent[:160]))
check("the response still parsed", events8[-1]["status"] == 200,
      str(events8[-1]["status"]))
check("and it is reported as reused", events8[-1]["reused"] is True)
check("the tunnel went back to the pool", len(pool.given) == 1,
      str(len(pool.given)))
check("and it is the very same socket",
      bool(pool.given) and pool.given[0][0] is seeded)

print("\n=== M. with no pool the tunnel is dialled and closed ===")
(_o9, events9, _c9, relay9) = drive(RESP)
check("exactly one dial", len(relay9.dials) == 1, str(len(relay9.dials)))
check("the upstream was told to close",
      b"connection: close" in relay9.dials[0].sent,
      repr(relay9.dials[0].sent[:160]))
check("the response parsed", events9[-1]["status"] == 200)
check("and it is not reported as reused", events9[-1]["reused"] is False)

print("\n=== N. an EOF-framed response is never pooled ===")
# Neither content-length nor chunked means the far end
EOF_FRAMED = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n\r\n"
              + b'data: {"type":"response.output_text.delta"}\n\n')
pool2 = FakePool()
(_o10, events10, _c10, _r10) = drive(EOF_FRAMED, pool=pool2)
check("the request still worked", events10[-1]["status"] == 200,
      str(events10[-1]["status"]))
check("but nothing was pooled", pool2.given == [], str(pool2.given))

print("\n=== O. a 429 is never pooled ===")
pool3 = FakePool()
(_o11, events11, _c11, _r11) = drive(BUSY, pool=pool3)
check("429 reported", events11[-1]["status"] == 429,
      str(events11[-1]["status"]))
check("429 left nothing in the pool", pool3.given == [], str(pool3.given))

print("\n=== P. TunnelPool drops what it must ===")
lane = FakeLane()
keep = mitm.TunnelPool(ttl=10.0)
sock_a = FakeSock(b"")
real_closed = mitm._peer_closed
try:
    mitm._peer_closed = lambda s: False
    keep.give(lane, sock_a, None)
    got = keep.take(lane)
    check("a live tunnel comes back", got is not None and got[0] is sock_a,
          str(got))
    check("and the pool is then empty", keep.take(lane) is None)

    keep.give(lane, sock_a, None)
    mitm._peer_closed = lambda s: True
    check("a tunnel the peer already closed is dropped",
          keep.take(lane) is None)

    mitm._peer_closed = lambda s: False
    keep.give(lane, sock_a, None)
    lane.exit_ip = "9.9.9.9"
    check("an exit change invalidates it", keep.take(lane) is None)

    stale = mitm.TunnelPool(ttl=-1.0)
    stale.give(lane, FakeSock(b""), None)
    check("a tunnel past its TTL is dropped", stale.take(lane) is None)
finally:
    mitm._peer_closed = real_closed

print("\n=== P2. the pool cannot grow without bound ===")
# Only `take` used to remove entries, so a
pool = mitm.TunnelPool(ttl=30.0)
lane = FakeLane()
lane.exit_ip = "10.0.0.1"
first = FakeSock(b"")
pool.give(lane, first, None)
lane.exit_ip = "10.0.0.2"          # re-pinned: the old tunnel is useless
second = FakeSock(b"")
pool.give(lane, second, None)
check("a tunnel from the old exit is closed", first.closed)
check("and it is dropped from the pool",
      len(pool._idle[lane.index]) == 1, str(len(pool._idle[lane.index])))
check("the current exit's tunnel is kept",
      pool._idle[lane.index][0][2] is second)

# A negative ttl expires everything regardless of the
old = mitm.TunnelPool(ttl=-1.0)
lane2 = FakeLane()
lane2.exit_ip = "10.0.0.3"
stale = FakeSock(b"")
old.give(lane2, stale, None)
old.give(lane2, FakeSock(b""), None)
check("an expired tunnel is closed on the next give", stale.closed)

print("\n=== Q. _peer_closed reads a real socket correctly ===")
import socket as _socket
left, right = _socket.socketpair()
try:
    check("a live peer is not closed", mitm._peer_closed(left) is False)
    right.close()
    check("a hung-up peer reads as closed", mitm._peer_closed(left) is True)
finally:
    left.close()

print("\n=== R. proof pane rendering ===")
os.environ.pop("NO_COLOR", None)  # the colour assertions need colour on
check("legacy event: payload only",
      proof._lat_bits({"kb": 12.5, "secs": 3.4}) == "12.5 KB in 3.4s",
      proof._lat_bits({"kb": 12.5, "secs": 3.4}))
check("stamped event shows both",
      proof._lat_bits({"kb": 12.5, "secs": 3.4, "first_byte_s": 0.7,
                       "first_event_s": 1.1})
      == "12.5 KB in 3.4s  first 0.7s  event 1.1s",
      proof._lat_bits({"kb": 12.5, "secs": 3.4, "first_byte_s": 0.7,
                       "first_event_s": 1.1}))
check("zeros are not rendered as fact",
      proof._lat_bits({"kb": 0, "secs": 0, "first_byte_s": 0,
                       "first_event_s": 0}) == "0 KB in 0s",
      proof._lat_bits({"kb": 0, "secs": 0, "first_byte_s": 0,
                       "first_event_s": 0}))
check("a reused tunnel says so",
      proof._lat_bits({"kb": 12.5, "secs": 3.4, "first_byte_s": 0.7,
                       "first_event_s": 1.1, "reused": True})
      .endswith("reused tunnel"),
      proof._lat_bits({"kb": 12.5, "secs": 3.4, "first_byte_s": 0.7,
                       "first_event_s": 1.1, "reused": True}))
check("a dialled tunnel does not",
      "reused" not in proof._lat_bits({"kb": 12.5, "secs": 3.4,
                                       "reused": False}))
line = proof._render(events[-1])
check("full callend line builds", "first" in line and "event" in line, line)
# The word is gone from the vocabulary, not
check("no line ever says GHOST",
      all("GHOST" not in proof._render(e) for e in events + events2 + events5
          + events6 + events7),
      " ".join(proof._render(e) for e in events6))
check("a reasoning-only 200 renders as a plain 200",
      "no model content" not in proof._render(events2[-1]),
      proof._render(events2[-1]))
check("a plain 200 renders as a plain 200",
      "GHOST" not in proof._render(events5[-1]),
      proof._render(events5[-1]))
check("an empty stream renders without a verdict",
      "no model content" not in proof._render(events6[-1]),
      proof._render(events6[-1]))
line429 = proof._render(events7[-1])
check("a 429 renders as a step, not a failure",
      "moving lanes" in line429 and "failed" not in line429, line429)
check("a 429 is amber, not red",
      "\x1b[33m429" in line429, repr(line429[:40]))

print("\n=== V. a transient upstream error buffers for a retry ===")
# A 503 is the far end being unwell,
UNAVAILABLE = (b"HTTP/1.1 503 Service Unavailable\r\n"
               b"content-type: text/plain\r\ncontent-length: 3\r\n\r\n503")
(_o15, events15, client15, _r15) = drive(UNAVAILABLE)
check("a 503 is buffered, not streamed", client15.got == b"",
      repr(client15.got[:40]))
check("and it is reported", events15[-1]["status"] == 503,
      str(events15[-1]["status"]))
check("429 is retryable too", 429 in mitm._RETRYABLE)
check("a plain 500 is not -- it may be a real error",
      500 not in mitm._RETRYABLE)

print("\n=== W. a head with no body is retried, not committed ===")
# The response head used to go to the
_HEAD_ONLY = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
              b"transfer-encoding: chunked\r\n\r\n")
(out16, events16, client16, _) = drive(
    _HEAD_ONLY, sock_cls=lambda resp: BoomSock(resp, ssl.SSLEOFError("cut")))
check("nothing reached the client", client16.got == b"",
      repr(client16.got[:48]))
check("the death is reported, not hidden", events16[-1]["status"] == 0,
      str(events16[-1]["status"]))
check("and it names the error", events16[-1]["err"] == "SSLEOFError",
      str(events16[-1]["err"]))
check("the attempt is retryable", out16[3] is True, f"retryable={out16[3]}")

# The case the log actually shows: the far
_PREAMBLE = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
             b"transfer-encoding: chunked\r\n\r\n"
             + chunk(b"event: response.created\n"))
(out18, events18, client18, _) = drive(
    _PREAMBLE, sock_cls=lambda resp: BoomSock(resp, ssl.SSLEOFError("cut")))
check("the preamble alone is not a commitment", client18.got == b"",
      repr(client18.got[:48]))
check("so it retries", out18[3] is True, f"retryable={out18[3]}")

# And the boundary: the first real model event
_MODEL_EVENT = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
                b"transfer-encoding: chunked\r\n\r\n"
                + chunk(b'event: response.output_text.delta\ndata: '
                        b'{"type":"response.output_text.delta"}\n\n'))
(out19, events19, client19, _) = drive(
    _MODEL_EVENT, sock_cls=lambda resp: BoomSock(resp, ssl.SSLEOFError("cut")))
check("a model event does commit", client19.got != b"",
      repr(client19.got[:48]))
check("and that attempt is not retried", out19[3] is False,
      f"retryable={out19[3]}")
check("the head went out with that event",
      client19.got.startswith(b"HTTP/1.1 200"), repr(client19.got[:24]))

# And the buffer the hold introduces must be
_FLOOD = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
          b"transfer-encoding: chunked\r\n\r\n"
          + b"".join(chunk(b": padding comment\n") for _ in range(6000))
          + b"0\r\n\r\n")
(out20, events20, client20, _) = drive(_FLOOD)
check("framing alone does not buffer forever",
      len(client20.got) > mitm._PRE_COMMIT_MAX,
      f"client got {len(client20.got)} bytes")
check("and the stream still ended cleanly", events20[-1].get("cut") is False,
      str(events20[-1].get("cut")))

print("\n=== U. the crypto stack is checked at startup ===")
# The crypto import is lazy, so a gutted
check("crypto_available() is True on a working install",
      mitm.crypto_available())
_cli_src = open(os.path.join(os.path.dirname(mitm.__file__), "cli.py"),
                encoding="utf-8").read()
check("and startup acts on it", "crypto_available" in _cli_src)

print("\n=== S. the log carries a session marker ===")
# `seq` restarts at 1 every run, so without
import re as _re

src = open(os.path.join(os.path.dirname(proof.__file__), "cli.py"),
           encoding="utf-8").read()
check("cli emits a start event with a session id",
      '"type": "start"' in src and '"session": os.urandom' in src)
line_start = proof._render({"type": "start", "t": 1000.0, "session": "abc123",
                            "lanes": 6, "countries": ["de", "nl"]})
check("the pane marks the session", "abc123" in line_start
      and "6 lanes" in line_start, line_start)
check("and it is not mistaken for a request line",
      not _re.search(r"#\d+\.\d+", line_start), line_start)

print("\n=== T. the log writer keeps the file open ===")
# The old emitter opened and closed the log
with tempfile.TemporaryDirectory() as tmp:
    log = Path(tmp) / "proof.log"
    emit = proof.make_emitter(log)
    for i in range(50):
        emit({"type": "start", "t": 1000.0 + i, "session": f"s{i}",
              "lanes": 6, "countries": ["de"]})
    # readable while the emitter still holds the handle
    lines = log.read_text(encoding="utf-8").splitlines()
    check("every event landed", len(lines) == 50, str(len(lines)))
    check("and each is whole JSON",
          all(json.loads(ln)["session"] == f"s{i}"
              for i, ln in enumerate(lines)), "truncated or merged")
    emit.close()
    log.unlink()      # fails while the handle is held
    check("closing really releases the file", not log.exists())

print("\n" + "=" * 46)
if FAIL:
    print(f"FAILURES: {len(FAIL)}")
    for x in FAIL:
        print("  - " + x)
    sys.exit(1)
print("ALL CALL OUTCOME CHECKS PASS")
