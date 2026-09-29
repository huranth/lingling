"""A request body that did not arrive whole must not be forwarded.

`_read_exact` returns whatever it managed to read when the peer stops early.
The caller used to take that short body and forward it -- with a matching
`Content-Length`, so the far end receives a complete-but-truncated request.
An upload path that tries to ingest that is entitled to refuse it, and the
provider's words for exactly that refusal are

    [invalid_request_error] Invalid upload request

which is a 400 the owner saw on a request that took 100 seconds to fail. It
burns a lane and a slice of the free tier on something that cannot succeed.

The client is the thing that closed, so nobody is listening by then. Dropping
the request is the only honest outcome.

This drives the shipped `_read_body`, and also shows what the old inline logic
did with the same input, so the regression is on the record rather than implied.
"""
import sys
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


class Reader:
    """A request-body reader that yields `n` bytes and then stops."""

    def __init__(self, data):
        self.data = data

    def read(self, n):
        out, self.data = self.data[:n], self.data[n:]
        return out


class Boom:
    def read(self, n):
        raise TimeoutError("timed out")


def old_logic(cf, headers):
    """What the caller used to do, verbatim, for comparison."""
    body = b""
    cl = headers.get("content-length")
    if cl:
        try:
            body = mitm._read_exact(cf, int(cl))
        except (ValueError, OSError):
            return None
    return body


def main():
    print("=== a body that arrives whole ===")
    got = mitm._read_body(Reader(b'{"model":"m"}'), {"content-length": "13"})
    check("it is returned", got == b'{"model":"m"}', repr(got))

    print("\n=== a request with no body at all ===")
    got = mitm._read_body(Reader(b""), {})
    check("it is an empty body, not a failure", got == b"", repr(got))

    print("\n=== the client closes mid-body ===")
    # 13 bytes promised, 5 delivered
    short = Reader(b'{"mod')
    got = mitm._read_body(short, {"content-length": "13"})
    was = old_logic(Reader(b'{"mod'), {"content-length": "13"})
    print(f"  now: {got!r}   before: {was!r}")
    check("the truncated request is dropped",
          got is None,
          f"got {got!r} -- a partial body would be forwarded upstream")
    check("and the old logic really did forward it",
          was == b'{"mod',
          f"old logic returned {was!r}; if that is not a partial body this "
          f"test proves nothing")

    print("\n=== a body that errors while being read ===")
    got = mitm._read_body(Boom(), {"content-length": "13"})
    check("it is dropped too", got is None, repr(got))

    print("\n=== a nonsense Content-Length ===")
    got = mitm._read_body(Reader(b"x"), {"content-length": "not-a-number"})
    check("it is dropped", got is None, repr(got))

    print()
    if FAILS:
        print("SHORT BODY: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("SHORT BODY: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
