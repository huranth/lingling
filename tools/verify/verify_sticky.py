"""The sticky router: one round elects a favorite, the favorite rides alone."""
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import health, lanes, mitm  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


HEAD = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
        b"transfer-encoding: chunked\r\n\r\n")
CHUNK = b"data: {\"type\":\"response.output_text.delta\"}\n\n"


class FakeLane:
    def __init__(self, index, cc):
        self.index = index
        self.exit_country = cc
        self.exit_ip = f"1.2.3.{index}"
        self.socks_port = 0
        self.healthy = True
        self.healing = False
        self.wanted = True
        self.asked = False
        self.limited_until = 0.0
        self.last_used_at = 0
        self.last_real_at = 0.0
        self.process = None
        self.lock = threading.Lock()
        self.active = 0


class FakeTor:
    """Five healthy lanes and a real StickyState on top."""

    def __init__(self, emit=None):
        ccs = ["nl", "at", "fr", "nz", "ca"]
        self.lanes = [FakeLane(i + 1, ccs[i]) for i in range(5)]
        self.sticky = lanes.StickyState(self, emit=emit)
        self.slow = []
        self.refusals = []

    def healthy_lanes(self):
        return [l for l in self.lanes if l.healthy and not l.healing]

    def note_slow_exit(self, lane, elapsed=0.0):
        self.slow.append((lane.index, elapsed))
        return f"lane {lane.index} first event {elapsed:.0f}s -- moved"

    def note_timeout(self, lane):
        return ""

    def note_ssl_error(self, lane):
        return ""


class SpyDaemon(health.HealthDaemon):
    """reachable() is counted, never really dialed."""

    def __init__(self, tor):
        super().__init__(tor, event=lambda e: None, log=lambda *a, **k: None)
        self.probed = []

    def reachable(self, lane, probe_timeout=1.0):
        self.probed.append(lane.index)
        return 200


def elect(tor, samples):
    """Cycle every lane once, then hand in the samples; a fast one sticks."""
    st = tor.sticky
    picked = []
    for _ in range(5):
        picked.append(st.next_lane().index)
    for idx, secs in samples.items():
        st.record(idx, secs)
    return picked


# ---- mitm-level harness (cleartext fake upstream) ----

def start_upstream(delay):
    """SOCKS5 upstream: head at once, first event after `delay` seconds."""
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
            threading.Thread(target=schedule, args=(c, delay),
                             daemon=True).start()

    def schedule(c, delay):
        try:
            c.settimeout(120)
            c.recv(3)
            c.sendall(bytes([0x05, 0x00]))
            c.recv(64)
            c.sendall(bytes([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
            c.recv(65536)
            c.sendall(HEAD)
            time.sleep(delay)
            c.sendall(b"%x\r\n" % len(CHUNK) + CHUNK + b"\r\n")
            c.sendall(b"0\r\n\r\n")
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    ready.wait(5)
    return holder[0]


JSON_BODY = b'{"models":[]}'
JSON_HEAD = (b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
             + b"content-length: %d\r\nconnection: close\r\n\r\n"
             % len(JSON_BODY))


def start_json_upstream(delay):
    """SOCKS5 upstream: a plain JSON body, never an SSE event."""
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
            threading.Thread(target=schedule, args=(c, delay),
                             daemon=True).start()

    def schedule(c, delay):
        try:
            c.settimeout(120)
            c.recv(3)
            c.sendall(bytes([0x05, 0x00]))
            c.recv(64)
            c.sendall(bytes([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
            c.recv(65536)
            time.sleep(delay)
            c.sendall(JSON_HEAD + JSON_BODY)
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    ready.wait(5)
    return holder[0]


def roundtrip_on(port, lane, relay, method="POST", path="/zen/v1/responses",
                 host="opencode.ai", body=b'{"model":"m"}'):
    """Drive the shipped `_roundtrip` against the fake upstream."""
    real_ctx = ssl.create_default_context

    class RawCtx:
        def wrap_socket(self, sock, server_hostname=None):  # noqa: ANN001
            return sock

    ssl.create_default_context = lambda *a, **k: RawCtx()

    class Tap:
        def __init__(self):
            self.got = bytearray()

        def sendall(self, data):
            self.got.extend(data)

        def settimeout(self, t):
            return None

    try:
        err, status, _h, _r = mitm._roundtrip(
            Tap(), lane, host, 443, method, path,
            {}, body, lambda e: None, 1, 1, time.time(), relay)
    finally:
        ssl.create_default_context = real_ctx
    return err, status


def main():
    print("=== (A) the round: five requests, one per lane, in order ===")
    events = []
    tor = FakeTor(emit=events.append)
    picked = elect(tor, {1: 9.0, 2: 4.5, 3: 12.0, 4: 7.0, 5: 8.0})
    print(f"  round order: {picked}  winner: {tor.sticky.winner}")
    check("the round visits every lane once, in lane order",
          picked == [1, 2, 3, 4, 5], f"{picked}")
    check("the lowest-time lane wins the election",
          tor.sticky.winner == 2 and tor.sticky.stuck_on(2),
          f"winner={tor.sticky.winner}")
    after = [tor.sticky.next_lane().index for _ in range(3)]
    check("every later request rides the winner alone",
          after == [2, 2, 2], f"{after}")
    check("the other lanes are frozen while stuck",
          tor.sticky.frozen(1) and tor.sticky.frozen(5)
          and not tor.sticky.frozen(2),
          f"frozen(1)={tor.sticky.frozen(1)} frozen(2)={tor.sticky.frozen(2)}")
    kinds = [e.get("kind") for e in events if e.get("type") == "lane"]
    check("the election speaks in the proof pane",
          "sticky" in kinds, f"{kinds}")

    print("\n=== (B) a verdict on the favorite wakes the others ===")
    tor.sticky.on_verdict(1)   # not the winner
    check("a verdict on another lane changes nothing",
          tor.sticky.stuck_on(2), f"winner={tor.sticky.winner}")
    tor.sticky.on_verdict(2)   # the winner
    back = [tor.sticky.next_lane().index for _ in range(5)]
    check("after the winner falls, a fresh round starts over",
          back == [1, 2, 3, 4, 5] and tor.sticky.winner is None, f"{back}")

    print("\n=== (C) a limited favorite is dropped without a verdict ===")
    tor = FakeTor()
    elect(tor, {1: 9.0, 2: 4.5, 3: 12.0, 4: 7.0, 5: 8.0})
    tor.lanes[1].limited_until = time.time() + 600
    lane = tor.sticky.next_lane()
    check("a limited winner is not ridden",
          lane.index != 2 and not tor.sticky.stuck_on(2),
          f"got lane {lane.index}")

    print("\n=== (D) the release ceiling: 15s on the stuck lane, not 20 ===")
    # D1: the event crosses the release ceiling -- the full drop fires early
    tor = FakeTor()
    relay = type("R", (), {"tor": tor})()
    elect(tor, {1: 1.0, 2: 9.0, 3: 9.0, 4: 9.0, 5: 9.0})
    tor.sticky.release_s = 2.0
    port = start_upstream(2.5)
    tor.lanes[0].socks_port = port
    err, status = roundtrip_on(port, tor.lanes[0], relay)
    fired = tor.slow and tor.slow[0][0] == 1 and tor.slow[0][1] >= 2.0
    check("a 2.5s event on the stuck lane (ceiling 2s) takes the verdict",
          fired and status == 200 and err == "",
          f"slow={tor.slow} status={status} err={err!r}")
    check("the drop wakes the others for a new round",
          not tor.sticky.stuck_on(1) and tor.sticky.winner is None,
          f"winner={tor.sticky.winner}")
    # D2: under the release ceiling, nothing fires -- the stick holds
    tor = FakeTor()
    relay = type("R", (), {"tor": tor})()
    elect(tor, {1: 1.0, 2: 9.0, 3: 9.0, 4: 9.0, 5: 9.0})
    tor.sticky.release_s = 5.0
    port = start_upstream(2.5)
    tor.lanes[0].socks_port = port
    err, status = roundtrip_on(port, tor.lanes[0], relay)
    check("a 2.5s event under a 5s release ceiling keeps the stick",
          not tor.slow and tor.sticky.stuck_on(1) and status == 200,
          f"slow={tor.slow} stuck={tor.sticky.stuck_on(1)}")

    print("\n=== (E) frozen lanes are not probed; the favorite is ===")
    tor = FakeTor()
    daemon = SpyDaemon(tor)
    for lane in tor.lanes:
        lane.process = type("P", (), {"poll": lambda self: None})()
    daemon.check_once()
    first_sweep = list(daemon.probed)
    elect(tor, {1: 9.0, 2: 9.0, 3: 1.0, 4: 9.0, 5: 9.0})
    for lane in tor.lanes:
        lane.asked = False
    daemon.probed.clear()
    daemon.check_once()
    stuck_sweep = list(daemon.probed)
    check("a free round probes every lane",
          first_sweep == [1, 2, 3, 4, 5], f"{first_sweep}")
    check("while stuck, only the favorite is probed -- the rest idle",
          stuck_sweep == [3], f"{stuck_sweep}")

    print("\n=== (F) a lane that fails mid-round is skipped, not waited for ===")
    # F1: the verdict lands after the survivors' times -- production order
    tor = FakeTor()
    for _ in range(5):
        tor.sticky.next_lane()
    for idx, secs in {1: 6.0, 2: 3.0, 3: 11.0, 5: 8.0}.items():
        tor.sticky.record(idx, secs)
    # lane 4's 429: note_limited marks it, the verdict reaches the router
    tor.lanes[3].limited_until = time.time() + 600
    tor.sticky.on_verdict(4)
    check("the election ran without lane 4 -- failed is tried",
          tor.sticky.winner == 2 and tor.sticky.stuck_on(2),
          f"winner={tor.sticky.winner}")
    after = [tor.sticky.next_lane().index for _ in range(3)]
    check("the stick covers only the survivors",
          after == [2, 2, 2], f"{after}")
    tor.sticky.on_verdict(2)
    round2 = tor.sticky.next_lane().index
    check("a re-cooking lane gets no round slot until it is back",
          round2 != 4, f"round2 lane {round2}")
    # F2: the rebuild lands before the last survivor's time
    tor = FakeTor()
    for _ in range(5):
        tor.sticky.next_lane()
    tor.lanes[3].healing = True   # lane 4 is already re-cooking
    for idx, secs in {1: 6.0, 2: 3.0, 3: 11.0, 5: 8.0}.items():
        tor.sticky.record(idx, secs)
    check("the election fires at the last sample, no lane 4 needed",
          tor.sticky.winner == 2 and tor.sticky.stuck_on(2),
          f"winner={tor.sticky.winner}")

    print("\n=== (G) every lane over the threshold: routing never sticks ===")
    tor = FakeTor()
    seen, picked = [], []
    for _ in range(5):
        idx = tor.sticky.next_lane().index
        picked.append(idx)
        tor.sticky.record(idx, 5.0 + idx)   # every lane is 6..10s
        seen.append(tor.sticky.winner)
    check("no lane sticks when every lane is over the threshold",
          seen == [None, None, None, None, None], f"{seen}")
    check("routing cycles the lanes in order",
          picked == [1, 2, 3, 4, 5], f"{picked}")
    after = [tor.sticky.next_lane().index for _ in range(3)]
    check("the next requests keep routing, not sticking",
          after == [1, 2, 3], f"{after}")

    print("\n=== (H) the first fast lane wins, the rest are not consulted ===")
    tor = FakeTor()
    for _ in range(5):
        tor.sticky.next_lane()
    tor.sticky.record(1, 8.0)   # over the threshold: passed over
    tor.sticky.record(2, 4.0)   # under it: this one wins
    tor.sticky.record(3, 1.0)   # faster, but the ride already started
    check("the first lane under the threshold wins, not the lowest",
          tor.sticky.winner == 2 and tor.sticky.stuck_on(2),
          f"winner={tor.sticky.winner}")

    print("\n=== (I) only a model call may elect a lane ===")
    # opencode itself fetches `models.opencode.ai/api.json` the moment it
    # starts. It is not a model call, so it must never win the round -- the
    # proof pane used to announce "lane 1 answered in 1.4s" before the user
    # typed anything, because a fast metadata fetch was counted as an answer.
    events = []
    tor = FakeTor(emit=events.append)
    relay = type("R", (), {"tor": tor})()
    port = start_json_upstream(0.2)
    tor.lanes[0].socks_port = port
    err, status = roundtrip_on(port, tor.lanes[0], relay,
                               method="GET", path="/api.json",
                               host="models.opencode.ai", body=b"")
    check("a background metadata GET is served",
          err == "" and status == 200, f"err={err!r} status={status}")
    check("a non-model request never elects a lane",
          tor.sticky.winner is None, f"winner={tor.sticky.winner}")
    check("and it says nothing in the proof pane",
          not any(e.get("kind") == "sticky" for e in events), f"{events}")
    # the positive control: the same fast timing on a model call still elects
    tor = FakeTor()
    port = start_upstream(0.2)
    tor.lanes[0].socks_port = port
    err, status = roundtrip_on(port, tor.lanes[0],
                               type("R", (), {"tor": tor})())
    check("a fast model call still wins the round",
          err == "" and status == 200 and tor.sticky.winner == 1,
          f"winner={tor.sticky.winner} err={err!r} status={status}")

    print()
    if FAILS:
        print("STICKY ROUTER: FAILED")
        return 1
    print("STICKY ROUTER: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
