"""The send window must clear the uploads that actually happen.

`_SEND_TIMEOUT` shipped at 30s. It was raised to 300s, **reverted to 30s on a
bad reading of the log**, and is now 120s. This pins the value to a
measurement so the next argument is about data rather than mechanism.

What the log says. The owner's request bodies run 4.5 MB and grow (median
4.7 MB on 09-21, 5.4 MB on 09-22 -- the whole conversation going back up on
every turn). Over the 107 successful 200s carrying a body of 1 MB or more:

    send_s   p50 = 10.8s    p90 = 20.8s    max = 29.8s
    send_s > 30s:  0 of 107

Zero above 30, with 13 of 107 in the 20-30s band. **A ceiling is only marginal
if the successes approach it, and these pile up against it.** The truncation at
29.8 is what proves the window is a TOTAL budget rather than a per-wait gap: a
per-wait timeout would let a 40s upload with short waits through, and no such
call exists in the sample. The 10 cuts are then the uploads that needed a
little more, and they are ALL on 4.5-5.6 MB bodies across lanes 1, 2, 4 and 5
-- a size effect, not one bad exit.

Reproduced on this machine (2026-09-22), so the 29.8s is not an outlier:
`upload_probe.py --concurrency 6 --lanes 1` pushed six 4.5 MB uploads through
one lane at once and `send_s` reached **21.5s with 3 of 12 in the 20-30s
band** -- 72% of the old wall on bodies 20% smaller than his current 5.4 MB
median. That is what this shape produces.

The trap this test exists to prevent: `max_wait_s` is NOT the send. It wraps
the whole `sendall` in one `_timed` call but only one chunk of a body read, so
it reaches 257.5s on this log -- impossible for a send once the ceiling is 30s.
Those are reads. Judging the send ceiling by `max_wait_s` is what argued the
window back down to the value that was cutting real traffic.

Case B fails on the old 30s default (`LINGLING_SEND_S=30` reproduces it), and
so does the GATE -- demonstrated 2026-09-22, not assumed:

    LINGLING_SEND_S=30 python tools/verify/verify_audit.py
    -> exit 1, `FAILURES: 1`: verify_send_window passes -- the window keeps
       headroom over every upload that has succeeded

That matters because `verify_audit.py` runs each suite as a SUBPROCESS with
captured output, so this one's banner never appears in a clean run. The only
way to know it is wired in is to break the value and watch the gate fail;
otherwise "AUDIT CLEAN" would be claiming coverage it does not have.
"""
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
