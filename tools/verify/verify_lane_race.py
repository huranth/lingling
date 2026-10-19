"""The race at the whole pool: claim, cut, one verdict, sticky."""
import io
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


BODY = b'{"model":"muse-spark-1.3-contributor-free"}'
REQ = (b"POST /zen/v1/responses HTTP/1.1\r\nhost: opencode.ai\r\n"
       b"content-type: application/json\r\n"
       b"content-length: %d\r\n\r\n" % len(BODY)) + BODY
HEAD = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
        b"transfer-encoding: chunked\r\n\r\n")
LIMITED = b'{"error":"exit is limited, come back"}'
RETRY = (b"HTTP/1.1 429 Too Many Requests\r\n"
         b"content-type: application/json\r\nretry-after: 120\r\n"
         b"content-length: %d\r\nconnection: close\r\n\r\n" % len(LIMITED))

#: The free tier's gate, as an exit answers it: the refusal a real build saw.
GATE = (b'{"error":"FreeTierError","message":"OpenCode\'s free tier can '
        b'only be used from within OpenCode."}')
GATED = (b"HTTP/1.1 403 Forbidden\r\ncontent-type: application/json\r\n"
         b"content-length: %d\r\nconnection: close\r\n\r\n" % len(GATE))

#: The far end's own 500, which must not beat a slower 200 either.
BROKEN = b'{"error":"the far end is unwell"}'
ERROR = (b"HTTP/1.1 500 Internal Server Error\r\n"
         b"content-type: application/json\r\n"
         b"content-length: %d\r\nconnection: close\r\n\r\n" % len(BROKEN))


def chunk(marker: bytes) -> bytes:
    """One SSE event, tagged with the lane that wrote it."""
    body = b"data: " + marker + b"\n\n"
    return b"%x\r\n" % len(body) + body + b"\r\n"


class Lane:
    """A lane the way ``_serve`` sees it."""

    def __init__(self, index: int) -> None:
        self.index = index
        self.exit_country = "de"
        self.exit_ip = f"1.2.3.{index}"
        self.exit_fingerprint = f"{index:040X}"
        self.socks_port = 0
        self.healthy = True
        self.healing = False
        self.wanted = True
        self.asked = True
        self.limited_until = 0.0
        self.last_used_at = index
        self.last_real_at = 0.0
        self.process = None
        self.lock = threading.Lock()
        self.active = 0
        self.streams = {}

    def track_stream(self, sock, progress, hangup=None) -> None:
        with self.lock:
            self.streams[id(sock)] = (sock, progress, None)

    def drop_stream(self, sock) -> None:
        with self.lock:
            self.streams.pop(id(sock), None)


class Tor:
    """Every verdict a race could charge, recorded instead."""

    def __init__(self, lanes) -> None:
        self.lanes = lanes
        self.sticky = None
        self.moved = []
        self.timeouts = []
        self.ssl = []
        self.slows = []
        self.scored = []

    def healthy_lanes(self):
        return [l for l in self.lanes if l.healthy and not l.healing]

    def note_limited(self, lane, retry_after=0.0, reason="rate",
                     fingerprint=""):
        """The far end's bench, and the deadline the product hands back."""
        lane.limited_until = time.time() + (retry_after or 600)
        self.moved.append(lane.index)
        return lane.limited_until

    def note_timeout(self, lane):
        self.timeouts.append(lane.index)
        return ""

    def note_ssl_error(self, lane):
        self.ssl.append(lane.index)
        return ""

    def note_slow_exit(self, lane, elapsed=0.0):
        self.slows.append((lane.index, elapsed))
        return ""

    def note_result(self, country, status):
        self.scored.append((country, status))


class Relay:
    """The slice of the product's relay that ``_serve`` talks to."""

    def __init__(self, tor: Tor) -> None:
        self.tor = tor
        self.tunnels = None
        self.refused = []

    def pick_lane(self, exclude=None):
        for lane in self.tor.lanes:
            if not exclude or lane.index not in exclude:
                return lane
        return None

    def pick_lanes(self, exclude=None):
        return [lane for lane in self.tor.lanes
                if not exclude or lane.index not in exclude]

    def pin_lanes(self, width):
        """The session's wave: the same few lanes, held steady.

        The free tier refuses a client it sees arrive from half a dozen
        addresses for the same session, so the session's set of addresses
        has to stay small.
        """
        return self.pick_lanes()[:width]

    def report_refused(self, lane, status, fingerprint=""):
        self.refused.append((lane.index, status))

    def any_unlimited(self, exclude):
        now = time.time()
        return any(l.index not in exclude and l.limited_until <= now
                   for l in self.tor.lanes)


class Client:
    """One request in, every byte out kept."""

    def __init__(self) -> None:
        self.got = bytearray()

    def makefile(self, _mode):
        return io.BytesIO(REQ)

    def sendall(self, data):
        self.got.extend(data)

    def settimeout(self, _t):
        return None


class Upstream:
    """A fake far end on its own port: what it sent, and what happened to it."""

    def __init__(self, spec) -> None:
        self.spec = spec
        self.dials = 0
        self.closed = 0
        self.cut = 0
        self.port = self._start()

    def _start(self):
        holder, ready = [], threading.Event()

        def run():
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
                threading.Thread(target=self._one, args=(c,),
                                 daemon=True).start()

        threading.Thread(target=run, daemon=True).start()
        ready.wait(5)
        return holder[0]

    def gone(self) -> bool:
        """True when this far end watched the client walk away."""
        return bool(self.closed or self.cut)

    def _exact(self, c, want: int) -> bytes:
        """Read exactly this many bytes, so nothing is left unread."""
        buf = b""
        while len(buf) < want:
            got = c.recv(want - len(buf))
            if not got:
                break
            buf += got
        return buf

    def _socks(self, c) -> None:
        """The handshake, byte for byte, the way tor answers it."""
        greet = self._exact(c, 3)
        self._exact(c, greet[2] if len(greet) > 2 else 0)
        c.sendall(bytes([0x05, 0x00]))
        head = self._exact(c, 4)
        atyp = head[3] if len(head) > 3 else 0
        if atyp == 1:
            self._exact(c, 4)
        elif atyp == 4:
            self._exact(c, 16)
        elif atyp == 3:
            ln = self._exact(c, 1)
            self._exact(c, ln[0] if ln else 0)
        self._exact(c, 2)
        c.sendall(bytes([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))

    def _request(self, c) -> bytes:
        """One request head, then its body, with nothing left in the buffer."""
        buf = b""
        while b"\r\n\r\n" not in buf:
            one = c.recv(1)
            if not one:
                return b""
            buf += one
        want = 0
        for line in buf.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                try:
                    want = int(line.split(b":", 1)[1])
                except ValueError:
                    want = 0
        return buf + self._exact(c, want)

    def _one(self, c):
        """One request, answered the way tor would have handed it over."""
        try:
            c.settimeout(20)
            self.dials += 1
            self._socks(c)
            self._request(c)
            kind, delay, marker = self.spec
            time.sleep(delay)
            if kind == "429":
                c.sendall(RETRY + LIMITED)
                return
            if kind == "403":
                c.sendall(GATED + GATE)
                return
            if kind == "500":
                c.sendall(ERROR + BROKEN)
                return
            c.sendall(HEAD)
            c.sendall(chunk(marker))
            c.sendall(b"0\r\n\r\n")
            # hand
            c.settimeout(4.0)
            if c.recv(1) == b"":
                self.closed += 1
        except OSError:
            self.cut += 1
        finally:
            try:
                c.close()
            except OSError:
                pass


class RawCtx:
    """No TLS between the test and its own upstream."""

    def wrap_socket(self, sock, server_hostname=None):  # noqa: ANN001
        return sock


class DyingStream:
    """A far end's stream: what it served, then a cut in the middle."""

    def __init__(self, served: bytes) -> None:
        self._buf = io.BytesIO(served)
        self._end = len(served)

    def _live(self) -> None:
        if self._buf.tell() >= self._end:
            raise ssl.SSLEOFError("the exit cut the stream")

    def read(self, size=-1):
        self._live()
        return self._buf.read(size)

    def readline(self, *a):
        self._live()
        return self._buf.readline(*a)


class FailClient:
    """A client that takes the head, then goes away mid-body."""

    def __init__(self, fail_at: int = 2) -> None:
        self.got = bytearray()
        self.writes = 0
        self._fail_at = fail_at

    def sendall(self, data):
        self.writes += 1
        if self.writes >= self._fail_at:
            raise ssl.SSLEOFError("the client went away")
        self.got.extend(data)

    def settimeout(self, _t):
        return None


class DyingUpstream:
    """A dialled upstream whose exit cuts the stream mid-body."""

    def __init__(self, served: bytes) -> None:
        self._served = served
        self.cuts = 0
        self.closed = 0

    def settimeout(self, _t):
        return None

    def connect(self, _a):
        return None

    def sendall(self, _d):
        return None

    def makefile(self, _m):
        return DyingStream(self._served)

    def shutdown(self, _how):
        self.cuts += 1

    def close(self):
        self.closed += 1


def serve(specs, benched=()):
    """Drive one real request through the shipped ``_serve``."""
    tor = Tor([Lane(i + 1) for i in range(len(specs))])
    relay = Relay(tor)
    client = Client()
    events = []
    ups = []
    for lane, spec in zip(tor.lanes, specs):
        up = Upstream(spec)
        ups.append(up)
        lane.socks_port = up.port
    for index in benched:
        tor.lanes[index - 1].limited_until = time.time() + 600
    real_ctx = ssl.create_default_context
    ssl.create_default_context = lambda *a, **k: RawCtx()
    try:
        mitm._serve(client, "opencode.ai", 443, 1, events.append, relay)
    finally:
        ssl.create_default_context = real_ctx
    return client, events, tor, relay, ups


def main():
    """Mechanics at the whole pool first, then the shipped default."""
    shipped_width = mitm._RACE_WIDTH
    #: sections 1-12
    mitm._RACE_WIDTH = 5
    print("=== 1. one call, every lane: the first answer takes the client ===")
    # owner's complaint
    specs = [("sse", 2.0, b"lane-1"), ("sse", 1.5, b"lane-2"),
             ("sse", 0.1, b"lane-3"), ("sse", 1.8, b"lane-4"),
             ("sse", 0.4, b"lane-5")]
    client, events, tor, relay, ups = serve(specs)
    calls = [e for e in events if e.get("type") == "call"]
    ends = [e for e in events if e.get("type") == "callend"]
    print(f"  dialled={[u.dials for u in ups]}  ends={len(ends)}")
    check("every lane was dialled", [u.dials for u in ups] == [1] * 5,
          str([u.dials for u in ups]))
    check("one call line names all five lanes",
          len(calls) == 1 and calls[0].get("lanes") == [1, 2, 3, 4, 5],
          str([c.get("lanes") for c in calls]))
    check("the quickest lane's answer is what the client got",
          b"lane-3" in client.got, repr(bytes(client.got[:60])))
    check("and it is the only answer in there",
          bytes(client.got).count(b"data: ") == 1,
          repr(bytes(client.got[:120])))
    check("only the winner speaks in the pane", len(ends) == 1, str(len(ends)))
    check("and its line names the lane that answered",
          bool(ends) and ends[0].get("lane") == 3
          and ends[0].get("status") == 200,
          str(ends[:1]))
    check("no loser was charged a verdict",
          not tor.timeouts and not tor.ssl and not tor.slows,
          f"timeouts={tor.timeouts} ssl={tor.ssl} slows={tor.slows}")
    check("no lane is left marked busy",
          [l.active for l in tor.lanes] == [0] * 5,
          str([l.active for l in tor.lanes]))
    check("and only the answer was scored", tor.scored == [("de", 200)],
          str(tor.scored))
    time.sleep(2.2)
    print(f"  closed={[u.closed for u in ups]} cut={[u.cut for u in ups]}")
    check("the slow lanes watched the client walk away",
          all(ups[i].gone() for i in (0, 1, 3)),
          f"gone={[ups[i].gone() for i in (0, 1, 3)]}")

    print("\n=== 2. a 429 on one lane is moved; the round still answers ===")
    specs = [("429", 0.05, b""), ("sse", 1.2, b"lane-2"),
             ("sse", 0.9, b"lane-3"), ("sse", 1.5, b"lane-4"),
             ("429", 0.05, b"")]
    client, events, tor, relay, ups = serve(specs)
    ends = [e for e in events if e.get("type") == "callend"]
    check("both burnt lanes were refused, once each",
          sorted(relay.refused) == [(1, 429), (5, 429)], str(relay.refused))
    check("and both were benched from the pool",
          sorted(tor.moved) == [1, 5], str(tor.moved))
    check("their lines name the limit", sorted(e["status"] for e in ends)
          == [200, 429, 429], str([e["status"] for e in ends]))
    check("the client still got an answer", b"lane-3" in client.got,
          repr(bytes(client.got[:60])))
    check("and never saw the 429", b"429" not in client.got,
          repr(bytes(client.got[:60])))

    print("\n=== 3. a fully burnt pool hands the 429 over, one round only ===")
    specs = [("429", 0.05, b"")] * 5
    client, events, tor, relay, ups = serve(specs)
    check("every lane was moved exactly once",
          sorted(tor.moved) == [1, 2, 3, 4, 5], str(tor.moved))
    check("no lane was dialled twice",
          [u.dials for u in ups] == [1] * 5, str([u.dials for u in ups]))
    check("the client got the far end's own 429",
          client.got.startswith(b"HTTP/1.1 429"), repr(bytes(client.got[:40])))
    check("with the retry-after it carried",
          b"retry-after: 120" in client.got, repr(bytes(client.got[:120])))
    check("no invented 502", b"502" not in client.got,
          repr(bytes(client.got[:40])))

    print("\n=== 4. a benched lane is not raced ===")
    specs = [("sse", 0.3, b"lane-1"), ("sse", 9.0, b"lane-2"),
             ("sse", 0.2, b"lane-3"), ("sse", 0.5, b"lane-4"),
             ("sse", 0.6, b"lane-5")]
    client, events, tor, relay, ups = serve(specs, benched=(2,))
    calls = [e for e in events if e.get("type") == "call"]
    check("the burnt lane was left out of the race",
          calls and calls[0].get("lanes") == [1, 3, 4, 5],
          str([c.get("lanes") for c in calls]))
    check("and nothing was dialled on it", ups[1].dials == 0,
          str(ups[1].dials))
    check("the call still answered", b"lane-3" in client.got,
          repr(bytes(client.got[:60])))

    print("\n=== 5. the first-event ceiling still fires, on the winner only ===")
    real_slow = mitm._SLOW_EXIT_S
    mitm._SLOW_EXIT_S = 0.3
    try:
        specs = [("sse", 2.0, b"lane-1"), ("sse", 0.5, b"lane-2"),
                 ("sse", 1.0, b"lane-3"), ("sse", 0.6, b"lane-4"),
                 ("sse", 1.4, b"lane-5")]
        client, events, tor, relay, ups = serve(specs)
    finally:
        mitm._SLOW_EXIT_S = real_slow
    print(f"  slows={tor.slows} timeouts={tor.timeouts} ssl={tor.ssl}")
    check("the winner's slow first event took the verdict",
          [idx for idx, _secs in tor.slows] == [2], str(tor.slows))
    check("the lanes it beat took none",
          not tor.timeouts and not tor.ssl,
          f"timeouts={tor.timeouts} ssl={tor.ssl}")
    check("and the client got that lane's answer", b"lane-2" in client.got,
          repr(bytes(client.got[:60])))

    print("\n=== 6. a dead heat still has exactly one winner ===")
    # Two lanes
    lanes = [Lane(i + 1) for i in range(5)]
    copies = [mitm._Copy(lane) for lane in lanes]
    race = mitm._Race(copies)
    gate = threading.Barrier(len(copies))
    wins = []
    answered = []
    lock = threading.Lock()

    def challenge(copy):
        gate.wait()
        got_it = copy.claim()
        with lock:
            if got_it:
                wins.append(copy.lane.index)
                answered.append(copy.lane.index)

    threads = [threading.Thread(target=challenge, args=(c,), daemon=True)
               for c in copies]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("exactly one copy took the client", len(wins) == 1, str(wins))
    check("and exactly one lane answered", answered == wins, str(answered))
    check("the race agrees it was that one",
          race.winner is copies[wins[0] - 1] if wins else False,
          f"winner={getattr(race.winner, 'lane', None)}")
    check("the losers all lost", all(not c.claim() for c in copies
                                    if c is not race.winner), "a loser claimed")

    print("\n=== 7. a dead heat on the wire still streams one answer ===")
    specs = [("sse", 0.3, b"lane-1"), ("sse", 0.3, b"lane-2"),
             ("sse", 0.3, b"lane-3"), ("sse", 0.3, b"lane-4"),
             ("sse", 0.3, b"lane-5")]
    client, events, tor, relay, ups = serve(specs)
    ends = [e for e in events if e.get("type") == "callend"]
    marks = [bytes(client.got).count(f"lane-{i}".encode())
             for i in range(1, 6)]
    print(f"  marks per lane={marks} ends={len(ends)}")
    check("one answer in the client's stream, not two",
          sum(marks) == 1, str(marks))
    check("the body is whole -- one start, one terminator",
          bytes(client.got).startswith(b"HTTP/1.1 200 OK")
          and bytes(client.got).endswith(b"0\r\n\r\n"),
          repr(bytes(client.got[-20:])))
    check("only the lane that claimed spoke", len(ends) == 1, str(len(ends)))
    check("every copy was still tried", [u.dials for u in ups] == [1] * 5,
          str([u.dials for u in ups]))

    print("\n=== 8. a winner with no record still owns the call ===")
    # copy's own
    lane = Lane(1)
    copy = mitm._Copy(lane)
    race = mitm._Race([copy])
    check("nothing has claimed yet", race.winner is None, "already claimed")
    lost = mitm._tally(race, [], Relay(Tor([lane])))
    check("an unanswered race is not committed", lost.committed is False,
          str(lost.committed))
    copy2 = mitm._Copy(Lane(2))
    race2 = mitm._Race([copy2])
    race2.claim(copy2)
    orphan = mitm._tally(race2, [], Relay(Tor([Lane(2)])))
    check("a claimed race is committed with no record at all",
          orphan.committed is True, str(orphan.committed))

    print("\n=== 9. a beaten copy never dials, and never speaks ===")
    lane = Lane(1)
    copy = mitm._Copy(lane)
    tor = Tor([lane])
    mitm._Race([copy])
    copy.beat()
    events = []
    dials = []
    real_socket = mitm.socket.socket
    mitm.socket.socket = lambda *a, **k: dials.append(1) or real_socket(*a, **k)
    try:
        out = mitm._roundtrip(Client(), lane, "opencode.ai", 443, "POST",
                              "/zen/v1/responses", {}, BODY, events.append,
                              1, 1, time.time(), Relay(tor),
                              charge_timeout=True, copy=copy)
    finally:
        mitm.socket.socket = real_socket
    check("no upstream was opened", dials == [], str(dials))
    check("nothing was said in the pane", events == [], str(events))
    check("and it reads as no verdict at all",
          out == ("", 0, b"", False), str(out))

    print("\n=== 10. a winner whose stream is cut still charges its own lane ===")
    # losers
    def drive(index: int, first: bool, served: bytes, join=None, client=None):
        """One copy of one race: `join` shares a race that has no winner."""
        if join is None:
            race = mitm._Race([mitm._Copy(l) for l in [Lane(i + 1)
                                                        for i in range(5)]])
            tor = Tor([c.lane for c in race.copies])
            mine = race.copies[index - 1]
            race.claim(mine)
        else:
            race, tor = join
            mine = race.copies[index - 1]
        up = DyingUpstream(served)
        lanes = [c.lane for c in race.copies]
        events = []
        real_socket = mitm.socket.socket
        real_open = mitm.netutil.socks5_open
        real_ctx = mitm.ssl.create_default_context
        mitm.socket.socket = lambda *a, **k: up
        mitm.netutil.socks5_open = lambda *a, **k: ""
        mitm.ssl.create_default_context = lambda *a, **k: RawCtx()
        try:
            out = mitm._roundtrip(client or Client(), lanes[index - 1],
                                  "opencode.ai",
                                  443, "POST", "/zen/v1/responses", {}, BODY,
                                  events.append, 1, 1, time.time(), Relay(tor),
                                  charge_timeout=first, copy=mine)
        finally:
            mitm.socket.socket = real_socket
            mitm.netutil.socks5_open = real_open
            mitm.ssl.create_default_context = real_ctx
        return out, tor, race, [e for e in events if e.get("type") == "callend"]

    mid = HEAD + chunk(b"lane-3")
    out, tor, race, ends = drive(3, first=False, served=mid)
    print(f"  mid-stream cut: ssl={tor.ssl} ends={ends}")
    check("the winning lane is charged, though it was not the first copy",
          tor.ssl == [3], f"ssl={tor.ssl}")
    check("exactly one verdict for the call",
          len(tor.ssl) + len(tor.timeouts) + len(tor.slows) == 1,
          f"ssl={tor.ssl} timeouts={tor.timeouts} slows={tor.slows}")
    check("the answer had already reached the client",
          ends and ends[0].get("status") == 200
          and ends[0].get("err") == "SSLEOFError",
          str(ends[:1]))
    check("a cut after the answer started is not replayed, so one request "
          "keeps one answer",
          out[0] == "SSLEOFError" and out[3] is False
          and ends[0].get("client_kb", 0) > 0,
          f"out={out[:1] + out[3:]} client_kb={ends[0].get('client_kb')}")

    out, tor, race, ends = drive(1, first=True, served=mid)
    check("the first copy is charged exactly once, as before",
          tor.ssl == [1], f"ssl={tor.ssl}")

    print("\n=== 11. a cold pool still charges exactly one lane ===")
    # gate
    race = mitm._Race([mitm._Copy(Lane(i + 1)) for i in range(5)])
    tor = Tor([c.lane for c in race.copies])
    _out, tor, _race, _ends = drive(1, first=True, served=b"", join=(race, tor))
    check("the first copy's death is charged", tor.ssl == [1], str(tor.ssl))
    _out, tor, _race, _ends = drive(4, first=False, served=b"", join=(race, tor))
    check("a later copy's death is not -- one verdict for the call",
          tor.ssl == [1], f"ssl={tor.ssl}")

    print("\n=== 12. a client that goes away is not the exit's fault ===")
    # fix lets
    served = HEAD + chunk(b"lane-2") + chunk(b"more")
    out, tor, _race, ends = drive(2, first=False, served=served,
                                  client=FailClient(fail_at=2))
    print(f"  client cut: err={out[0]!r} ssl={tor.ssl} ends={ends}")
    check("the lane is not charged for the client going away",
          tor.ssl == [] and tor.timeouts == [] and tor.slows == [],
          f"ssl={tor.ssl} timeouts={tor.timeouts}")
    check("and the call is attributed to the client, not the exit",
          bool(ends) and ends[0].get("stalled") == "client"
          and ends[0].get("status") == 200
          and ends[0].get("client_kb", 0) > 0,
          str(ends[:1]))

    print("\n=== 13. the shipped wave is five exits, not the whole fleet ===")
    #: Live proof, 2026-10-18: one client, one evening.  Every wave of five
    #: answered 200; the ten-lane session that replaced it was refused
    #: ``403 FreeTierError`` on every copy of its very first call, and the
    #: refusal then stuck to the client -- 50 refusals in ninety seconds.  Ten
    #: lanes are still worth having (they spread the day's allowance); ten
    #: copies of one call are what the gate reads as abuse.
    check("the shipped wave is capped at five", shipped_width == 5,
          f"_RACE_WIDTH={shipped_width}")
    specs = [("sse", 0.2, b"lane-fast")] + [("sse", 9.0, b"lane-slow")] * 9
    client, events, tor, relay, ups = serve(specs)
    calls = [e for e in events if e.get("type") == "call"]
    raced = calls[0].get("lanes") if calls else []
    dials = [u.dials for u in ups]
    print(f"  raced={raced}  dials={dials}")
    check("a ten-lane fleet is raced five at a time",
          raced == [1, 2, 3, 4, 5], str(raced))
    check("and the other five are never dialled for that call",
          dials == [1] * 5 + [0] * 5, str(dials))
    check("the quickest lane's answer is what the client got",
          b"lane-fast" in client.got, repr(bytes(client.got[:60])))
    check("with exactly one answer in its stream",
          bytes(client.got).count(b"data: ") == 1,
          repr(bytes(client.got[:120])))

    print("\n=== 14. the knob narrows a wave, and a refused one still widens ===")
    was = mitm._RACE_WIDTH
    try:
        mitm._RACE_WIDTH = 2
        specs = [("429", 0.05, b""), ("429", 0.05, b""),
                 ("sse", 0.2, b"lane-fast"), ("sse", 9.0, b"lane-slow")]
        client, events, tor, relay, ups = serve(specs)
    finally:
        mitm._RACE_WIDTH = was
    calls = [e for e in events if e.get("type") == "call"]
    waves = [c.get("lanes") or [] for c in calls]
    dials = [u.dials for u in ups]
    print(f"  width=2  waves={waves}  dials={dials}")
    check("no wave is wider than the knob",
          bool(waves) and all(len(w) <= 2 for w in waves), str(waves))
    check("no lane is raced twice across the waves",
          len({i for w in waves for i in w}) == sum(len(w) for w in waves),
          str(waves))
    check("a refused wave is followed by the next lanes",
          len(waves) >= 2, str(waves))
    check("so the call reaches a lane the first wave never touched",
          any(i > 2 for w in waves for i in w), str(waves))
    check("and every lane was dialled at most once",
          all(d <= 1 for d in dials), str(dials))
    check("the refused lanes were moved off", sorted(tor.moved) == [1, 2],
          str(tor.moved))
    check("the client got the fresh lane's answer",
          b"lane-fast" in client.got, repr(bytes(client.got[:60])))
    check("with exactly one answer in its stream",
          bytes(client.got).count(b"data: ") == 1,
          repr(bytes(client.got[:120])))

    print("\n=== 15. a gated exit answers first, and still does not take "
          "the call ===")
    #: The gate refuses in a blink -- refusing is cheaper than answering -- so
    #: the lane it refused wins every race it is in unless the refusal is held
    #: back.  A build must never see it while another lane can serve.
    specs = [("403", 0.05, b""), ("sse", 1.0, b"lane-2"),
             ("sse", 1.5, b"lane-3"), ("403", 0.05, b"")]
    client, events, tor, relay, ups = serve(specs)
    ends = [e for e in events if e.get("type") == "callend"]
    print(f"  statuses={[e.get('status') for e in ends]}")
    check("the lane that could serve is what the client got",
          b"lane-2" in client.got, repr(bytes(client.got[:60])))
    check("and the gate's refusal never reached it",
          b"FreeTierError" not in client.got, repr(bytes(client.got[:60])))
    check("one answer in its stream, not two",
          bytes(client.got).count(b"data: ") == 1,
          repr(bytes(client.got[:120])))
    check("the gated lanes are still reported in the pane",
          sorted(e.get("status") for e in ends) == [200, 403, 403],
          str([e.get("status") for e in ends]))
    said = [e.get("msg", "") for e in events if e.get("kind") == "limited"]
    benched = [tor.lanes[i - 1].limited_until > time.time() for i in (1, 4)]
    print(f"  benched={benched} said={said}")
    check("the gated exits are sat out, so no call races them again",
          all(benched) and not relay.refused,
          f"benched={benched} refused={relay.refused}")
    check("and the pane says which exits the gate closed",
          len(said) == 2 and all("403" in m for m in said), str(said))

    print("\n=== 16. a pool where every exit is gated still hands the "
          "refusal over ===")
    specs = [("403", 0.05, b"")] * 4
    client, events, tor, relay, ups = serve(specs)
    check("the client got the gate's own 403",
          bytes(client.got).startswith(b"HTTP/1.1 403"),
          repr(bytes(client.got[:40])))
    check("one refusal, not one per lane",
          bytes(client.got).count(b"FreeTierError") == 1,
          repr(bytes(client.got[:200])))
    check("no lane was dialled twice", [u.dials for u in ups] == [1] * 4,
          str([u.dials for u in ups]))
    check("every gated exit is sat out",
          all(l.limited_until > time.time() for l in tor.lanes),
          str([l.limited_until for l in tor.lanes]))

    print("\n=== 16b. one gated wave ends the call; the rest of the fleet "
          "is not tried ===")
    #: The refusal says the client will not be credited through ANY address,
    #: so paying for the same wall on the other five exits only deepens it.
    specs = [("403", 0.05, b"")] * 10
    client, events, tor, relay, ups = serve(specs)
    dials = [u.dials for u in ups]
    print(f"  dials={dials}")
    check("the client got the gate's own 403",
          bytes(client.got).startswith(b"HTTP/1.1 403"),
          repr(bytes(client.got[:40])))
    check("exactly one wave was sent, five exits of ten",
          dials == [1] * 5 + [0] * 5, str(dials))
    check("and the refusal is handed over once",
          bytes(client.got).count(b"FreeTierError") == 1,
          repr(bytes(client.got[:200])))

    print("\n=== 16c. a gated exit sits out minutes, not the rest of the "
          "free-tier day ===")
    from lingling import bench  # noqa: E402
    now = time.time()
    gate = bench.bench_span(bench.GATE_LIMIT, 0.0, now)
    day = bench.next_free_reset(now) - now
    print(f"  gate bench={gate:.0f}s  day={day:.0f}s")
    check("the gate's bench is bounded to minutes",
          0 < gate <= 3600, f"{gate:.0f}s")
    check("and it is not the free-tier day",
          gate < day - 60, f"gate={gate:.0f}s day={day:.0f}s")
    check("a real limit still gets its own span",
          bench.bench_span(bench.FREE_LIMIT, 0.0, now) == day,
          f"{bench.bench_span(bench.FREE_LIMIT, 0.0, now):.0f}s")

    print("\n=== 17. only a 200 takes the call: the fastest answer is not "
          "the best one ===")
    specs = [("500", 0.05, b""), ("sse", 1.0, b"lane-2"),
             ("sse", 1.4, b"lane-3")]
    client, events, tor, relay, ups = serve(specs)
    check("a 500 that answered in a blink did not take the call",
          b"lane-2" in client.got, repr(bytes(client.got[:60])))
    check("and the far end's error never reached the client",
          b"HTTP/1.1 500" not in client.got, repr(bytes(client.got[:40])))
    check("the quickest of the two 200s is the one that won -- the rule "
          "the whole thing is for",
          b"lane-2" in client.got and b"lane-3" not in client.got,
          repr(bytes(client.got[:80])))

    specs = [("500", 0.05, b""), ("500", 0.05, b"")]
    client, events, tor, relay, ups = serve(specs)
    check("a pool with no 200 at all hands the far end's own 500 over",
          bytes(client.got).startswith(b"HTTP/1.1 500"),
          repr(bytes(client.got[:40])))
    check("once, verbatim, not one per lane",
          bytes(client.got).count(b"the far end is unwell") == 1,
          repr(bytes(client.got[:200])))
    check("and no lane was dialled twice", [u.dials for u in ups] == [1, 1],
          str([u.dials for u in ups]))

    print("\n=== 18. one closed exit is announced once, however many calls "
          "are in flight ===")
    #: The live pane showed this: a dozen identical "sitting it out" lines in
    #: one second, one per copy of the same exit already in the air.  The pane
    #: says a fact, so one closed exit is one line.
    tor = Tor([Lane(1), Lane(2)])
    relay = Relay(tor)
    #: a fingerprint no earlier section has spoken for, and a gate slow enough
    #: that all six calls are in the air before it answers.
    tor.lanes[0].exit_fingerprint = "GATE18" * 6 + "18"
    ups18 = [Upstream(("403", 0.4, b"")), Upstream(("sse", 0.6, b"lane-2"))]
    for lane, up in zip(tor.lanes, ups18):
        lane.socks_port = up.port
    runs, said, lock = [], [], threading.Lock()
    ready = threading.Barrier(6)

    def one() -> None:
        client, events = Client(), []
        ready.wait(30)
        mitm._serve(client, "opencode.ai", 443, 1, events.append, relay)
        with lock:
            runs.append(client)
            said.extend(e.get("msg", "") for e in events
                        if e.get("kind") == "limited")

    real_ctx = ssl.create_default_context
    ssl.create_default_context = lambda *a, **k: RawCtx()
    try:
        threads = [threading.Thread(target=one) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
    finally:
        ssl.create_default_context = real_ctx
    print(f"  calls={len(runs)}  announced={len(said)}  {said[:2]}")
    check("every call still got the lane that could serve",
          len(runs) == 6 and all(b"lane-2" in c.got for c in runs),
          f"calls={len(runs)}")
    check("the closed exit is announced once, not once per call",
          len(said) == 1, str(said))

    print("\n=== 19. a compressed refusal is not printed as mojibake ===")
    #: The live pane carried two hundred characters of it on every gated call.
    check("a body that is not text says nothing",
          mitm._note_of(b"\x44\xd9\x39\x4d\xff\xfe") == "",
          repr(mitm._note_of(b"\x44\xd9\x39\x4d\xff\xfe")))
    check("a readable one is still one line",
          mitm._note_of(b'{"error":\n  "FreeTierError"}')
          == '{"error": "FreeTierError"}',
          repr(mitm._note_of(b'{"error":\n  "FreeTierError"}')))

    print()
    if FAILS:
        print("LANE RACE: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("LANE RACE: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
