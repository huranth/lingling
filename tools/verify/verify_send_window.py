"""The send window must clear the uploads that actually happen."""
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import mitm  # noqa: E402

FAILS = []

#: highest `send_s` seen on a successful >=1MB upload, over 107 calls
MEASURED_MAX_UPLOAD_S = 29.8
#: how much headroom the shipped window must keep over it
HEADROOM = 3.0


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    if not ok:
        FAILS.append(name)


HEAD = (b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
        b"transfer-encoding: chunked\r\n\r\n")


class BlockingSock:
    """An upstream whose send blocks for `block_for`, then times out."""

    def __init__(self, block_for):
        self.calls = []
        self.block_for = block_for

    def settimeout(self, t):
        self.calls.append(("settimeout", t))

    def sendall(self, data):
        self.calls.append(("sendall", len(data)))
        time.sleep(self.block_for)
        raise TimeoutError("timed out")

    def close(self):
        return None

    def makefile(self, mode):
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


def drive(sock, body):
    """One `_roundtrip` over `sock`, returning the callend it emitted."""
    relay = type("R", (), {"tor": Tor(), "tunnels": Pool(sock)})()
    events = []
    mitm._roundtrip(Client(), Lane(), "opencode.ai", 443, "POST",
                    "/zen/v1/responses", {}, body, events.append, 1, 1,
                    time.time(), relay, charge_timeout=False)
    ends = [e for e in events if e.get("type") == "callend"]
    return ends[-1] if ends else {}


def main():
    print(f"shipped _SEND_TIMEOUT = {mitm._SEND_TIMEOUT}s")

    print("\n=== A. the window is armed, and the stall is attributed ===")
    sock = BlockingSock(block_for=0.3)
    rec = drive(sock, b'{"model":"m"}')
    print(f"  err={rec.get('err')!r} stalled={rec.get('stalled')!r} "
          f"send_s={rec.get('send_s')!r}")
    check("the send arms the send window",
          ("settimeout", mitm._SEND_TIMEOUT) in sock.calls,
          f"armed {[c for c in sock.calls if c[0] == 'settimeout']}")
    check("a send that times out is reported as a TimeoutError",
          rec.get("err") == "TimeoutError", f"err={rec.get('err')!r}")
    check("and it is charged to the upstream, not the client",
          rec.get("stalled") == "upstream", f"stalled={rec.get('stalled')!r}")

    print("\n=== B. the shipped window clears the measured uploads ===")
    need = MEASURED_MAX_UPLOAD_S * HEADROOM
    print(f"  highest successful upload send_s = {MEASURED_MAX_UPLOAD_S}s "
          f"(n=107);  needed >= {need:.1f}s")
    check("the window keeps headroom over every upload that has succeeded",
          mitm._SEND_TIMEOUT >= need,
          f"_SEND_TIMEOUT={mitm._SEND_TIMEOUT}s against a measured max of "
          f"{MEASURED_MAX_UPLOAD_S}s -- the successes are pressed against "
          f"this ceiling, so uploads that need slightly more are cut")

    print("\n=== C. send_s is what answers this, and it is on every callend ===")
    check("a failed send still records its own duration",
          isinstance(rec.get("send_s"), (int, float)) and rec["send_s"] >= 0.25,
          f"send_s={rec.get('send_s')!r} -- without it the upload duration is "
          f"invisible and max_wait_s gets read as the send instead")
    check("max_wait_s is NOT a substitute for it",
          (rec.get("max_wait_s") or 0) >= 0.25,
          f"max_wait_s={rec.get('max_wait_s')!r}")

    print()
    if FAILS:
        print("SEND WINDOW: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("SEND WINDOW: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
