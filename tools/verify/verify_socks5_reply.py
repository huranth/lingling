"""The SOCKS5 CONNECT reply is variable length, and must be read exactly."""
import socket
import struct
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import netutil  # noqa: E402

FAILS = []

MARKER = b"MARKER-AFTER-THE-HANDSHAKE"


def check(name, ok, detail=""):
    """Detail is the failure reason, so only show it when it failed."""
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    if not ok:
        FAILS.append(name)


def socks_server(atyp):
    """A one-shot SOCKS5 server that replies with `atyp`, then a marker."""
    holder = []
    ready = threading.Event()

    def serve():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        holder.append(srv.getsockname()[1])
        ready.set()
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=_serve_one, args=(c, atyp),
                             daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    ready.wait(5)
    return holder[0]


def _serve_one(c, atyp):
    try:
        c.settimeout(30)
        c.recv(3)                       # greeting
        c.sendall(bytes([0x05, 0x00]))
        c.recv(512)                     # CONNECT request
        if atyp == 0x01:
            addr = socket.inet_aton("0.0.0.0")
        elif atyp == 0x03:
            addr = bytes([9]) + b"localhost"
        else:
            addr = b"\x00" * 16
        c.sendall(bytes([0x05, 0x00, 0x00, atyp]) + addr + struct.pack("!H", 0))
        c.sendall(MARKER)
    except OSError:
        pass


def run(atyp):
    """Drive socks5_open against a server answering with `atyp`."""
    port = socks_server(atyp)
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.settimeout(10)
    try:
        err = netutil.socks5_open(sock, "opencode.ai", 443)
        sock.settimeout(3)
        try:
            nxt = sock.recv(len(MARKER) + 32)
        except OSError as exc:
            nxt = b"<%s>" % type(exc).__name__.encode()
    finally:
        sock.close()
    return err, nxt


def main():
    names = {0x01: "IPv4", 0x03: "domain", 0x04: "IPv6"}

    for atyp in (0x01, 0x03, 0x04):
        label = names[atyp]
        err, nxt = run(atyp)
        print(f"=== ATYP=0x{atyp:02x} ({label}) ===")
        print(f"  socks5_open -> {err!r}   next bytes -> {nxt!r}")
        check(f"{label}: the handshake succeeds",
              err == "", f"err={err!r}")
        check(f"{label}: the socket is clean afterwards",
              nxt == MARKER,
              f"got {nxt!r} -- {len(nxt) - len(MARKER)} byte(s) of reply "
              f"were left in the buffer and the TLS handshake would read them")
        print()

    print()
    if FAILS:
        print("SOCKS5 REPLY LENGTH: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("SOCKS5 REPLY LENGTH: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
