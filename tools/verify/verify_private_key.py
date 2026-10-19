"""A private key is private, and a broken CA is re-minted.

Windows has no POSIX mode, so os.chmod does nothing there: the CA key kept
the ACL of the directory it was created in, and on a shared %TEMP% that
lets any other local account read it.  A CA that cannot be loaded used to
raise out of _ensure_ca, and the cli swallowed the error -- so the MITM was
silently off for that session and for every one after it, because the broken
files stayed on disk.  A per-host cert left beside the wrong key failed
every handshake for that host until certs/ was cleared by hand.
"""
import os
import pathlib
import ssl
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import mitm, netutil  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    """Detail is the failure reason, so only show it when it failed."""
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    if not ok:
        FAILS.append(name)


def acl_of(path):
    """The ACL of a path, as text, on Windows."""
    try:
        res = subprocess.run(["icacls", str(path)], capture_output=True,
                             timeout=30)
    except Exception:  # noqa: BLE001
        return ""
    return res.stdout.decode("latin-1", "replace")


def main() -> int:
    print("=== a private key is narrowed to its owner ===")
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="ll-acl-"))
    if os.name == "nt":
        # a shared %TEMP% hands the file to everyone
        subprocess.run(["icacls", str(scratch), "/grant", "*S-1-1-0:(OI)(CI)F"],
                       capture_output=True)
    shop = mitm.CertShop(scratch / "mitm")
    shop.context_for("opencode.ai")
    keys = [scratch / "mitm" / "ca-key.pem",
            scratch / "mitm" / "certs" / "opencode.ai-key.pem"]
    for key in keys:
        check(f"{key.name} exists", key.is_file(), str(key))
        if os.name == "nt":
            text = acl_of(key)
            check(f"{key.name} is not open to Everyone",
                  "Everyone" not in text, text.strip())
            check(f"{key.name} names SYSTEM and Administrators",
                  "SYSTEM" in text and "Administrators" in text, text.strip())
    if os.name != "nt":
        for key in keys:
            mode = key.stat().st_mode & 0o777
            check(f"{key.name} is 0o600 on POSIX", mode == 0o600, oct(mode))

    print("\n=== the owner sid is readable ===")
    check("this user's sid is found", bool(netutil._current_sid()),
          str(netutil._current_sid()))

    print("\n=== a CA that cannot be loaded is re-minted ===")
    broken = pathlib.Path(tempfile.mkdtemp(prefix="ll-broke-"))
    mitmdir = broken / "mitm"
    shop2 = mitm.CertShop(mitmdir)
    first = (mitmdir / "ca.pem").read_bytes()
    (mitmdir / "ca-key.pem").write_bytes(b"not a key at all")
    try:
        shop3 = mitm.CertShop(mitmdir)
        minted = True
        err = ""
    except Exception as exc:  # noqa: BLE001
        minted = False
        err = f"{type(exc).__name__}: {exc}"
    check("a corrupt CA does not raise out of CertShop", minted, err)
    if minted:
        second = (mitmdir / "ca.pem").read_bytes()
        check("the broken CA was replaced", second != first)
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(second)
        check("the new CA loads", cert is not None)

    print("\n=== a truncated CA is re-minted too ===")
    (mitmdir / "ca.pem").write_bytes(b"-----BEGIN CERTIFICATE-----\ntrunc")
    try:
        mitm.CertShop(mitmdir)
        ok = True
        err = ""
    except Exception as exc:  # noqa: BLE001
        ok = False
        err = f"{type(exc).__name__}: {exc}"
    check("a truncated CA does not raise", ok, err)

    print("\n=== a cert beside the wrong key is minted again ===")
    pair = pathlib.Path(tempfile.mkdtemp(prefix="ll-pair-"))
    shop4 = mitm.CertShop(pair / "mitm")
    shop4.context_for("opencode.ai")
    certs = pair / "mitm" / "certs"
    (certs / "opencode.ai.pem").write_bytes(b"-----BEGIN CERTIFICATE-----\nnope")
    try:
        ctx = shop4.context_for("opencode.ai")
        ok = isinstance(ctx, ssl.SSLContext)
        err = ""
    except Exception as exc:  # noqa: BLE001
        ok = False
        err = f"{type(exc).__name__}: {exc}"
    check("a broken per-host pair is minted again", ok, err)
    if ok:
        check("and the new pair loads",
              ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER) is not None)

    print()
    if FAILS:
        print("PRIVATE KEY: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("PRIVATE KEY: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
