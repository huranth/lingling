"""The relay answers `Expect: 100-continue` itself, so it must not forward it.

`handle_conn` reads the client's headers and, if they carry a 100-continue
expectation, sends the client a local `HTTP/1.1 100 Continue` so it will go
ahead and send its body. The body is then read and the request is re-issued
upstream.

But `expect` was never in the outbound `skip` set, so the header went upstream
too -- while the body was sent immediately. That is a protocol violation:
RFC 7231 5.1.1 says a proxy that responds to the expectation itself must not
pass it on. The upstream is then entitled to refuse the request WITHOUT reading
the body, and the provider's own words for that are

    [invalid_request_error] Invalid upload request

-- an upload path bailing before it ingests anything, which is what the owner
saw on one request that took 100 seconds to be refused.

This drives the shipped `_roundtrip` with a client that sends the expectation
and captures the bytes the relay puts on the wire. It fails on the old code,
where `expect: 100-continue` appears in the forwarded head.
"""
import sys
import threading
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


class Wire:
    """An upstream socket that records what the relay sends it."""

    def __init__(self):
        self.sent = b""

    def settimeout(self, t):
        return None

    def sendall(self, data):
        self.sent += data

    def close(self):
        return None

    def makefile(self, mode):
        return self


class MuteReader:
    """A file object whose read raises at once, so the attempt ends there."""

    def read(self, n):
        raise TimeoutError("timed out")


class FakePool:
    def __init__(self, up, uf):
        self.up, self.uf = up, uf

    def take(self, lane):
        return self.up, self.uf

    def give(self, lane, up, uf):
        return None


def main():
    print("=== the relay forwards the client's Expect: 100-continue ===")

    wire = Wire()
    pool = FakePool(wire, MuteReader())

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
        """The client: records the local 100 Continue the relay sends it."""

        def __init__(self):
            self.got = b""

        def sendall(self, data):
            self.got += data

        def settimeout(self, t):
            return None

    client = Tap()
    headers = {"host": "opencode.ai", "content-type": "application/json",
               "expect": "100-continue", "accept": "text/event-stream"}
    old = mitm._READ_TIMEOUT
    mitm._READ_TIMEOUT = 1.0
    try:
        mitm._roundtrip(client, Lane(), "opencode.ai", 443, "POST",
                        "/zen/v1/responses", headers, b'{"model":"m"}',
                        lambda e: None, 1, 1, __import__("time").time(),
                        relay, charge_timeout=False)
    finally:
        mitm._READ_TIMEOUT = old

    head = wire.sent.split(b"\r\n\r\n")[0].decode("latin1")
    print("  the head the relay put on the wire:")
    for line in head.splitlines():
        print("     " + line)

    # NOTE: the local `100 Continue` to the CLIENT is sent by `handle_conn`, one
    # level up -- this function only re-issues the request. Asserting it here
    # was wrong and failed. What matters at THIS level is that the header does
    # not ride along upstream, which is the violation.
    check("and it did NOT forward the expectation upstream",
          "expect" not in head.lower(),
          "the upstream is told to expect a 100-continue it will never get, "
          "and may refuse the request without reading the body")
    check("the body is still framed correctly",
          "content-length: 13" in head.lower(),
          f"content-length missing or wrong (body is 13 bytes): {head!r}")
    check("the rest of the headers still go through",
          "content-type" in head.lower() and "accept" in head.lower(),
          "stripping expect took other headers with it")

    print()
    if FAILS:
        print("EXPECT HEADER: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("EXPECT HEADER: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
