"""Does the 20s ceiling kill a long think?"""

from __future__ import annotations

import socket
import ssl
import sys
import threading
import time

sys.path.insert(0, ".")

from lingling import mitm  # noqa: E402
from lingling import netutil  # noqa: E402


class Tap:
    """A client socket that records what reached it."""

    def __init__(self) -> None:
        self.got = bytearray()

    def sendall(self, data: bytes) -> None:
        self.got.extend(data)

    def close(self) -> None:
        pass


def _serve_slow(first_at: float, gap0: float, think_for: float,
                gap: float) -> tuple[socket.socket, int]:
    """Upstream that pauses, opens the head, pauses, then streams."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run() -> None:
        # A client that hangs up is normal here:
        conn, _ = srv.accept()
        try:
            time.sleep(first_at)                 # think before the head
            conn.sendall(b"HTTP/1.1 200 OK\r\n"
                         b"Content-Type: text/event-stream\r\n"
                         b"Transfer-Encoding: chunked\r\n\r\n")

            def chunk(body: bytes) -> None:
                conn.sendall(b"%x\r\n" % len(body) + body + b"\r\n")

            # framing only, no data: yet
            chunk(b"event: response.created\n\n")
            time.sleep(gap0)                     # first token lands here
            deadline = time.time() + think_for
            while time.time() < deadline:
                chunk(b'data: {"type":"response.reasoning.delta","delta":"x"}\n\n')
                time.sleep(gap)                  # a byte every `gap` seconds
            chunk(b'data: {"type":"response.completed"}\n\n')
            conn.sendall(b"0\r\n\r\n")
        except OSError:
            # client gone
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    threading.Thread(target=run, daemon=True).start()
    return srv, port


def _run_case(first_at: float, gap0: float, think_for: float,
              gap: float, ceiling: float, post: float = 300.0
              ) -> tuple[bool, float, int, int, str]:
    """Drive _roundtrip against the fake upstream."""
    srv, port = _serve_slow(first_at, gap0, think_for, gap)
    old = mitm._READ_TIMEOUT
    old_post = mitm._STREAM_IDLE_TIMEOUT
    mitm._READ_TIMEOUT = ceiling
    mitm._STREAM_IDLE_TIMEOUT = post
    tap = Tap()
    events: list = []
    lane = type("L", (), {
        "index": 1, "exit_country": "de", "exit_ip": "1.2.3.4",
        "socks_port": port, "lock": threading.Lock(), "active": 0,
    })()
    relay = type("R", (), {"tor": None})()

    def fake_socks(sock, host, port_, cred=None):  # noqa: ANN001
        return ""

    class RawCtx:
        """The fake upstream speaks cleartext; skip the TLS wrap."""

        def wrap_socket(self, sock, server_hostname=None):  # noqa: ANN001
            return sock

    real_socks = netutil.socks5_open
    real_ctx = ssl.create_default_context
    netutil.socks5_open = fake_socks
    ssl.create_default_context = lambda *a, **k: RawCtx()
    try:
        t0 = time.time()
        err, status, _held, _retryable = mitm._roundtrip(
            tap, lane, "opencode.ai", 443, "POST", "/zen/v1/responses",
            {}, b'{"model":"m"}', events.append, 1, 1, t0, relay)
        secs = time.time() - t0
    finally:
        mitm._READ_TIMEOUT = old
        mitm._STREAM_IDLE_TIMEOUT = old_post
        netutil.socks5_open = real_socks
        ssl.create_default_context = real_ctx
        srv.close()
    end = [e for e in events if e.get("type") == "callend"]
    first_event_s = int(end[-1].get("first_event_s", 0)) if end else 0
    kb = int(len(tap.got) / 1024)
    return (err == "" and status == 200), secs, kb, first_event_s, err


def main() -> int:
    print("=== the owner's scenario: first token at 19s, then think 60s ===")
    ok, secs, kb, fes, err = _run_case(first_at=1.0, gap0=18.0,
                                       think_for=60.0, gap=1.0, ceiling=20.0)
    print("  head at 1s, first token at 19s, then bytes every 1.0s for 60s")
    print(f"  result: ok={ok}  wall={secs:.1f}s  bytes={kb} KB  "
          f"first_event={fes}s  err={err!r}")
    # The claim is that the ceiling is IDLE,
    survived = secs >= 75 and fes >= 18 and kb >= 2
    print(f"  [{'PASS' if survived else 'FAIL'}] survived all 79s"
          f"  (needed >=75s and >=2KB, got {secs:.1f}s / {kb}KB)")

    print("\n=== the case that SHOULD die: silence past the ceiling ===")
    ok2, secs2, kb2, _, err2 = _run_case(first_at=25.0, gap0=0.5,
                                         think_for=1.0, gap=1.0, ceiling=20.0)
    print("  nothing at all for 25s, ceiling 20s")
    print(f"  result: ok={ok2}  wall={secs2:.1f}s  bytes={kb2} KB  "
          f"err={err2!r}")
    died = (not ok2) and secs2 < 24
    print(f"  [{'PASS' if died else 'FAIL'}] abandoned at the ceiling"
          f"  (needed <24s, got {secs2:.1f}s)")

    print("\n=== a silence past the POST-commit ceiling still cuts it ===")
    # `post` is set below the 25s gap on
    ok3, secs3, kb3, fes3, err3 = _run_case(first_at=1.0, gap0=1.0,
                                            think_for=1.0, gap=25.0,
                                            ceiling=20.0, post=10.0)
    print("  first token at 2s, then a 25s gap; post-commit ceiling 10s")
    print(f"  result: ok={ok3}  wall={secs3:.1f}s  bytes={kb3} KB  "
          f"first_event={fes3}s  err={err3!r}")
    cut = (not ok3) and fes3 >= 1
    print(f"  [{'PASS' if cut else 'FAIL'}] the post-commit silence was caught"
          f"  (needed first_event>0, got {fes3}s)")

    print("\n=== but a gap UNDER the post-commit ceiling is a think-pause ===")
    # The owner's actual failure: a committed stream that
    ok4, secs4, kb4, fes4, err4 = _run_case(first_at=1.0, gap0=1.0,
                                            think_for=1.0, gap=8.0,
                                            ceiling=20.0, post=10.0)
    print("  first token at 2s, then an 8s gap; post-commit ceiling 10s")
    print(f"  result: ok={ok4}  wall={secs4:.1f}s  bytes={kb4} KB  "
          f"first_event={fes4}s  err={err4!r}")
    paused = fes4 >= 1 and (err4 != "TimeoutError")
    print(f"  [{'PASS' if paused else 'FAIL'}] the pause was tolerated"
          f"  (err={err4!r}, first_event={fes4}s)")

    print()
    print("=" * 46)
    good = survived and died and cut and paused
    print("IDLE CEILING: " + ("CONFIRMED" if good else "FAILED"))
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
