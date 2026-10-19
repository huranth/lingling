"""A certificate may never leave its directory, and a bad port may never crash.

The CONNECT target is the client's to choose, and it is only ever checked
for its ``opencode.ai`` suffix.  Its name was then used as a file name, so a
target like ``..\\..\\..\\pwned.opencode.ai`` had lingling write an RSA
private key wherever the client pointed.  A port outside 0..65535 reached
``struct.pack("!H", port)``, which raises ``struct.error`` -- neither an
OSError nor a ValueError -- so it escaped every handler and killed the
worker with no reply at all.
"""
import pathlib
import socket
import ssl
import struct
import sys
import tempfile
import threading

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import mitm, netutil, relay  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    """Detail is the failure reason, so only show it when it failed."""
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    if not ok:
        FAILS.append(name)


#: a target that walks out of the certs directory
TRAVERSALS = (
    r"..\..\..\pwned.opencode.ai",
    r"..\..\..\..\..\..\Users\pjm09\Documents\pwned.opencode.ai",
    "/etc/pwned.opencode.ai",
    "sub/dir.opencode.ai",
    r"C:\Users\pjm09\AppData\Local\Temp\abs.opencode.ai",
    r"..\opencode.ai",
)


def main() -> int:
    print("=== a host name may not carry a path ===")
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="ll-certpath-"))
    shop = mitm.CertShop(scratch / "mitm")
    certs = scratch / "mitm" / "certs"

    for host in TRAVERSALS:
        stem = mitm._cert_stem(host)
        check(f"{host!r} becomes a plain file name",
              "/" not in stem and "\\" not in stem and ".." not in stem,
              f"stem={stem!r}")
        check(f"{host!r} stays inside the certs directory",
              (certs / f"{stem}.pem").resolve().parent
              == certs.resolve(),
              f"would land in {(certs / f'{stem}.pem').resolve()}")
        try:
            shop.context_for(host)
            minted = True
            err = ""
        except Exception as exc:  # noqa: BLE001
            minted = False
            err = f"{type(exc).__name__}: {exc}"
        check(f"{host!r} mints without leaving the directory", minted, err)

    print("\n=== nothing was written outside the certs directory ===")
    inside = {p.name for p in certs.iterdir()}
    check("the certs directory holds the minted pairs",
          any(n.endswith(".pem") for n in inside),
          f"inside={sorted(inside)}")
    strays = [p for p in scratch.parent.glob("*opencode.ai*")
              if p.parent.resolve() != certs.resolve()]
    check("no key landed in the parent directory", not strays,
          f"strays={[str(p) for p in strays]}")

    print("\n=== a real host still mints and still chains ===")
    ctx = shop.context_for("opencode.ai")
    check("opencode.ai still mints a context", isinstance(ctx, ssl.SSLContext))
    from cryptography import x509
    cert = x509.load_pem_x509_certificate(
        (certs / "opencode.ai.pem").read_bytes())
    check("and it is signed by our CA",
          cert.issuer == shop._ca_cert.subject,
          f"issuer={cert.issuer.rfc4514_string()}")
    check("the per-host file name is unchanged for a real host",
          (certs / "opencode.ai.pem").exists()
          and (certs / "opencode.ai-key.pem").exists())

    print("\n=== the relay refuses a target that is not a host name ===")
    r = relay.Relay(tor=None)
    shop2 = mitm.CertShop(pathlib.Path(tempfile.mkdtemp(prefix="ll-certpath2-"))
                          / "mitm")
    r.cert_shop = shop2
    for host in TRAVERSALS:
        check(f"{host!r} is not intercepted", not r._should_mitm(host))
    for host in ("opencode.ai", "models.opencode.ai"):
        check(f"{host!r} is still intercepted", r._should_mitm(host))

    print("\n=== an out-of-range port is refused, not packed ===")
    for port in (0, -1, 65536, 99999):
        check(f"port {port} is not a valid port",
              not 1 <= port <= 65535)

    print("\n=== socks5_open answers a bad port instead of raising ===")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    sp = srv.getsockname()[1]

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            def one(c=c):
                try:
                    c.settimeout(30)
                    c.recv(3)
                    c.sendall(bytes([0x05, 0x00]))
                    c.recv(512)
                    c.sendall(bytes([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
                except OSError:
                    pass
            threading.Thread(target=one, daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    for port in (0, -1, 65536, 99999):
        sock = socket.create_connection(("127.0.0.1", sp), timeout=10)
        sock.settimeout(10)
        try:
            err = netutil.socks5_open(sock, "opencode.ai", port)
            check(f"a port of {port} is refused with a reason",
                  err != "", f"err={err!r}")
            check(f"a port of {port} does not raise struct.error",
                  not isinstance(err, struct.error))
        except BaseException as exc:  # noqa: BLE001
            check(f"a port of {port} does not raise", False,
                  f"{type(exc).__name__}: {exc}")
        finally:
            sock.close()
    srv.close()

    print("\n=== struct.error is an OSError's cousin, not its subclass ===")
    check("struct.error is not an OSError",
          not issubclass(struct.error, OSError))
    check("struct.error is not a ValueError",
          not issubclass(struct.error, ValueError))
    check("so the range check has to come first",
          True)

    print()
    if FAILS:
        print("CERT PATHS: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("CERT PATHS: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
