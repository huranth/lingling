"""Per-request MITM for opencode.ai: terminate TLS locally with a throwaway
CA (trusted via NODE_EXTRA_CA_CERTS) so individual model calls become
visible, then re-encrypt through a lane's SOCKS5 tunnel. Blocking-socket
code on daemon threads, kept off the asyncio loop so SSE never stalls it."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from . import netutil
from .lanes import Lane, TorManager

#: Only opencode.ai is unwrapped; everything else stays a blind tunnel.
MITM_HOSTS = ("opencode.ai",)

#: First-byte budget for a fresh circuit (cold lanes are slow to answer).
_FIRST_BYTE_TIMEOUT = 90.0
#: Steady per-read ceiling once the stream is provably alive.
_READ_TIMEOUT = 30.0
#: How long a poisoned or lane-less call parks while the daemon brings
#: up a fresh lane, before the original answer is forwarded instead.
_LANE_WAIT_S = 60.0

_ca_lock = threading.Lock()
_ca_ctx: Dict[Path, "tuple"] = {}  # dir -> (ca_cert, ca_key, ssl.SSLContext cache)


def _ensure_ca(mitm_dir: Path):
    """Create (or load) the local root CA."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    mitm_dir.mkdir(parents=True, exist_ok=True)
    cert_path = mitm_dir / "ca.pem"
    key_path = mitm_dir / "ca-key.pem"
    if cert_path.exists() and key_path.exists():
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(
            key_path.read_bytes(), password=None)
        return cert, key, cert_path

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                         "lingling local proof CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                           critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=False, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return cert, key, cert_path


class CertShop:
    """Mints per-host certs signed by the local CA, cached on disk."""

    def __init__(self, mitm_dir: Path) -> None:
        with _ca_lock:
            self._ca_cert, self._ca_key, self.ca_pem_path = _ensure_ca(mitm_dir)
        self._dir = mitm_dir / "certs"
        self._dir.mkdir(parents=True, exist_ok=True)

    def context_for(self, host: str) -> ssl.SSLContext:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        safe = host.replace("*", "wildcard")
        cert_path = self._dir / f"{safe}.pem"
        key_path = self._dir / f"{safe}-key.pem"
        if not cert_path.exists():
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            now = datetime.datetime.now(datetime.timezone.utc)
            san = x509.SubjectAlternativeName([x509.DNSName(host)])
            cert = (x509.CertificateBuilder()
                    .subject_name(x509.Name([x509.NameAttribute(
                        NameOID.COMMON_NAME, host)]))
                    .issuer_name(self._ca_cert.subject)
                    .public_key(key.public_key())
                    .serial_number(x509.random_serial_number())
                    .not_valid_before(now - datetime.timedelta(days=1))
                    .not_valid_after(now + datetime.timedelta(days=825))
                    .add_extension(san, critical=False)
                    .add_extension(x509.BasicConstraints(ca=False,
                                                         path_length=None),
                                   critical=True)
                    .sign(self._ca_key, hashes.SHA256()))
            cert_path.write_bytes(cert.public_bytes(
                serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert_path), str(key_path))
        return ctx


def _read_head(f) -> Optional[bytes]:
    """Read one HTTP head (request or status line + headers) through CRLFCRLF."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = f.read(1)
        if not chunk:
            return None
        buf += chunk
        if len(buf) > 65536:
            return None
    return buf


def _read_exact(f, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def _model_of(body: bytes) -> str:
    try:
        return str(json.loads(body.decode("utf-8", "replace")).get("model", ""))
    except Exception:  # noqa: BLE001
        return ""


def _body_json(body: bytes):
    """Decoded JSON dict, or None when the body is not one."""
    try:
        obj = json.loads(body.decode("utf-8"))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _blob_holders(node):
    """Yield every dict in decoded JSON that carries an encrypted blob."""
    if isinstance(node, dict):
        if isinstance(node.get("encrypted_content"), str):
            yield node
        for value in node.values():
            yield from _blob_holders(value)
    elif isinstance(node, list):
        for value in node:
            yield from _blob_holders(value)


def _blob_id(blob: str) -> str:
    """Stable fingerprint of one blob. Hashes only: the ciphertext
    itself is never kept or logged."""
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


#: Fingerprints of blobs the provider already rejected under this
#: process. A rejected blob never turns valid again (exits never
#: repeat), so later turns can skip resending them instead of paying
#: a fresh 400 every time. Hashes only, capped, shared by MITM threads.
_stale_lock = threading.Lock()
_stale_blobs: set = set()
_STALE_CAP = 2000
#: Disk sidecar for the memory above, set once at startup. Hashes only,
#: so the file holds nothing sensitive -- just fingerprints.
_stale_file: Optional[Path] = None

#: The lane whose exit issued a session's current reasoning blobs --
#: the provider only validates blobs against the exit that issued
#: them, so the next call tries that lane first and skips the 400.
_owner_lock = threading.Lock()
_owners: Dict[str, "Lane"] = {}
_OWNER_CAP = 64


def prime_stale_memory(path) -> None:
    """Load known-stale fingerprints learned by earlier launches.

    A missing or broken file simply means learning restarts: one taxed
    turn, then the memory refills itself.
    """
    global _stale_file
    _stale_file = Path(path)
    try:
        saved = json.loads(_stale_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(saved, list):
        return
    with _stale_lock:
        for fingerprint in saved:
            if (isinstance(fingerprint, str)
                    and len(_stale_blobs) < _STALE_CAP):
                _stale_blobs.add(fingerprint)


def _save_stale_memory() -> None:
    """Write the memory to its sidecar file, if primed.

    Best-effort: a failed write only costs re-learning next launch.
    Snapshots are complete, so two threads racing a save can't corrupt
    each other -- the last writer simply wins with a full set.
    """
    if _stale_file is None:
        return
    try:
        with _stale_lock:
            snapshot = sorted(_stale_blobs)
        _stale_file.parent.mkdir(parents=True, exist_ok=True)
        _stale_file.write_text(json.dumps(snapshot), encoding="utf-8")
    except OSError:
        pass


def _remember_stale(body: bytes) -> None:
    """File every blob in an as-sent body as known-stale (memory + disk)."""
    obj = _body_json(body)
    if obj is None:
        return
    fresh = [_blob_id(holder["encrypted_content"])
             for holder in _blob_holders(obj)]
    if not fresh:
        return
    with _stale_lock:
        if len(_stale_blobs) > _STALE_CAP:
            _stale_blobs.clear()
        before = len(_stale_blobs)
        _stale_blobs.update(fresh)
        changed = len(_stale_blobs) != before
    if changed:
        _save_stale_memory()


def _own_headers(headers: dict) -> str:
    """Session key for a request: provider affinity ids, else the
    caller's session id, else empty (no owner preference applies)."""
    for k in ("x-session-affinity", "x-session-id", "session-id"):
        v = headers.get(k)
        if isinstance(v, bytes):
            v = v.decode("latin1", "replace")
        if v:
            return str(v)
    return ""


def _remember_owner(session: str, lane: Lane) -> None:
    """Record the lane that just answered a session cleanly. The exit
    that issued the current blobs is the only one that validates them."""
    if not session:
        return
    with _owner_lock:
        if len(_owners) >= _OWNER_CAP:
            _owners.clear()
        _owners[session] = lane


def _owner_of(session: str) -> Optional[Lane]:
    """The lane whose exit issued the session's current blobs, if alive."""
    if not session:
        return None
    with _owner_lock:
        lane = _owners.get(session)
    if lane is None or not lane.healthy or lane.sidelined or lane.healing:
        return None
    return lane


def _without_stale_reasoning(body: bytes) -> Optional[bytes]:
    """Copy of a JSON request body minus stale encrypted reasoning.

    Returns None when the body is not JSON or holds no encrypted blobs,
    so the caller can fall back to today's behavior untouched. Only the
    provider's ciphertext goes -- ids, summaries, history all stay.
    """
    obj = _body_json(body)
    if obj is None:
        return None
    holders = list(_blob_holders(obj))
    if not holders:
        return None
    for holder in holders:
        del holder["encrypted_content"]
    return json.dumps(obj).encode("utf-8")


def _prestrip_known_stale(body: bytes):
    """Remove already-rejected blobs before sending.

    Returns (body_to_send, dropped). dropped 0 means nothing matched --
    the original goes out untouched so valid chains keep working.
    """
    obj = _body_json(body)
    if obj is None:
        return body, 0
    with _stale_lock:
        known = set(_stale_blobs)
    if not known:
        return body, 0
    dropped = 0
    for holder in _blob_holders(obj):
        if _blob_id(holder["encrypted_content"]) in known:
            del holder["encrypted_content"]
            dropped += 1
    if not dropped:
        return body, 0
    return json.dumps(obj).encode("utf-8"), dropped


def _looks_poisoned(reply: bytes) -> bool:
    """True when an upstream error body complains about stale encrypted
    reasoning. Byte-level on purpose: error pages are tiny ASCII JSON."""
    return b"encrypted_content" in reply


def _dst_failure(err: str) -> bool:
    """True when a failed ride is the destination's fault, not the lane's
    (SOCKS replies for a dead host). The site, not the exit, answered, so
    the lane keeps its health -- a page that refuses every exit must not
    drain the pool."""
    return (
        err == "host unreachable"
        or err == "connection refused"
        or err == "network unreachable"
        or err == "TTL expired"
    )


def _wait_lane(relay, emit, seq: int, call_n: int, model: str) -> Optional[Lane]:
    """Block up to the lane-wait budget for any healthy lane. The caller
    polls so a lane that just re-cooked is used the moment it's ready."""
    deadline = time.time() + _LANE_WAIT_S
    told = False
    while True:
        lane = relay.pick_lane(exclude=None)
        if lane is not None:
            return lane
        if time.time() >= deadline:
            return None
        if not told:
            told = True
            emit({
                "type": "lane", "kind": "heal", "t": time.time(),
                "lane": 0, "cc": "", "ip": "",
                "msg": f"holding #{seq}.{call_n} ({model}) while a lane "
                       f"finishes cooking ...",
            })
        time.sleep(0.5)


def _try_heal(client: ssl.SSLSocket, lane: Lane, host: str, port: int,
              method: str, path: str, headers: dict, body: bytes,
              model: str, emit, seq: int, call_n: int, relay) -> bool:
    """Retry a poisoned call, stripped of stale reasoning, on a fresh lane.

    The provider rejected the blobs as not-issued-to-this-caller, so the
    same lane would only re-offend: try the next-fastest lane instead.
    Returns True only when the client received a fresh answer and the
    original 400 must NOT be forwarded; anything else returns False and
    the caller forwards the original 400 untouched."""
    if os.environ.get("LINGLING_NO_HEAL", "").lower() in ("1", "true"):
        return False
    healed_body = _without_stale_reasoning(body)
    if healed_body is None:
        return False
    session = _own_headers(headers)
    # A poison call needs ANOTHER lane; if none is ready the daemon is
    # mid-cook on all of them, so park the call and retry once it's up.
    # The stripped body is safe on ANY exit, so a re-cooked lane counts.
    healed = _wait_lane(relay, emit, seq, call_n, model)
    if healed is None:
        return False
    t0 = time.time()
    emit({
        "type": "lane", "kind": "heal", "t": t0,
        "lane": lane.index, "cc": lane.exit_country, "ip": lane.exit_ip,
        "msg": f"stale reasoning on lane {lane.index} -- retrying "
               f"#{seq}.{call_n} without it, on another lane ...",
    })
    tried = {lane.index}
    for _ in range(4):
        emit({
            "type": "call", "t": time.time(), "n": seq, "c": call_n,
            "lane": healed.index, "cc": healed.exit_country,
            "ip": healed.exit_ip,
            "method": method, "path": path, "model": model, "host": host,
        })
        err, status, _held, retryable = _roundtrip(
            client, healed, host, port, method, path, headers,
            healed_body, emit, seq, call_n, t0)
        if err:
            if retryable:
                tried.add(healed.index)
                healed = relay.pick_model_lane(exclude=tried)
                if healed is None:
                    return False
                continue
            return False
        if status == 429:
            relay.report_burn(healed)
            tried.add(healed.index)
            healed = relay.pick_model_lane(exclude=tried)
            if healed is None:
                return False
            continue
        if retryable:  # partial body reached the client; not ours to fix
            return False
        if 200 <= status < 300:
            _remember_owner(session, healed)
            emit({
                "type": "lane", "kind": "up", "t": time.time(),
                "lane": healed.index, "cc": healed.exit_country,
                "ip": healed.exit_ip,
                "msg": f"healed #{seq}.{call_n} -- stale reasoning dropped, "
                       f"history kept, fresh answer delivered",
            })
            return True
        return False
    return False


def handle_conn(raw: socket.socket, host: str, port: int, seq: int,
                shop: CertShop, manager: TorManager,
                emit: Callable[[Dict], None], relay) -> None:
    """Own one intercepted TLS connection end to end (blocking thread)."""
    try:
        # The socket was dup'd from asyncio -- it arrives non-blocking.
        raw.setblocking(True)
        raw.settimeout(300)  # idle keep-alive ceiling between model calls
        ctx = shop.context_for(host)
        client = ctx.wrap_socket(raw, server_side=True)
    except (ssl.SSLError, OSError):
        try:
            raw.close()
        except OSError:
            pass
        return
    try:
        _serve(client, host, port, seq, manager, emit, relay)
    except (OSError, ssl.SSLError, TimeoutError):
        # Idle keep-alive ceiling or a client that vanished mid-request --
        # the connection is over either way, nothing to report.
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def _serve(client: ssl.SSLSocket, host: str, port: int, seq: int,
           manager: TorManager, emit: Callable[[Dict], None], relay) -> None:
    """HTTP/1.1 keep-alive loop; one lane tunnel per request so consecutive
    calls visibly rotate lanes."""
    cf = client.makefile("rb")
    call_n = 0
    while True:
        head = _read_head(cf)
        if head is None:
            return
        try:
            line = head.split(b"\r\n", 1)[0].decode("latin1")
            method, path, _ = line.split(" ", 2)
        except ValueError:
            return
        headers = {}
        for raw in head.split(b"\r\n")[1:]:
            if not raw or b":" not in raw:
                continue
            k, v = raw.split(b":", 1)
            headers[k.strip().lower().decode("latin1")] = v.strip()

        expect = headers.get("expect", b"")
        if isinstance(expect, str):
            expect = expect.encode("latin1")
        if b"100-continue" in expect.lower():
            client.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")

        body = b""
        cl = headers.get("content-length")
        if cl:
            try:
                body = _read_exact(cf, int(cl))
            except (ValueError, OSError):
                return
        model = _model_of(body) if body and method == "POST" else ""
        call_n += 1

        # Skip blobs this process already saw rejected: resending them
        # buys a certain 400. Fresh (never-failed) blobs stay, so valid
        # reasoning chains keep working.
        send_body = body
        if os.environ.get("LINGLING_NO_HEAL", "").lower() not in ("1", "true"):
            send_body, _prestripped = _prestrip_known_stale(body)

        held = b""
        tried: set = set()
        session = _own_headers(headers)
        lane = _owner_of(session)
        if lane is None and method == "POST" and model:
            # No owner yet (first call or it died): nothing rides yet, so
            # park the call until a lane cooks instead of 502ing.
            lane = _wait_lane(relay, emit, seq, call_n, model)
        while True:
            if lane is None or lane.index in tried:
                lane = relay.pick_model_lane(exclude=tried)
            if lane is None:
                # Every lane just refused us: hand back the last one
                # verbatim rather than a 502.
                if held:
                    client.sendall(held)
                else:
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n"
                                   b"Content-Length: 0\r\n\r\n")
                return
            t0 = time.time()
            emit({
                "type": "call", "t": t0, "n": seq, "c": call_n,
                "lane": lane.index, "cc": lane.exit_country,
                "ip": lane.exit_ip,
                "method": method, "path": path, "model": model, "host": host,
            })
            with lane.lock:
                lane.active += 1
            try:
                err, status, held, retryable = _roundtrip(
                    client, lane, host, port, method, path, headers,
                    send_body, emit, seq, call_n, t0)
            finally:
                with lane.lock:
                    lane.active -= 1
            if err:
                if retryable:
                    # Nothing reached the client -- a free retry. A dead
                    # destination is the site's fault, never the lane's:
                    # the lane keeps its health so dead pages can't drain
                    # the pool.
                    if not _dst_failure(err):
                        relay.report_stall(lane, hard=True)
                    tried.add(lane.index)
                    continue
                return
            if status == 429:
                relay.report_burn(lane)
                tried.add(lane.index)
                continue
            if status == 400 and method == "POST":
                # A 400 is the lane reporting, not the lane failing: the
                # exit refused reasoning issued by ANOTHER lane's exit, so
                # the lane stays in rotation -- pulling it only starves the
                # pool and costs re-cook time.
                poisoned = _looks_poisoned(held)
                if poisoned:
                    # File what we actually sent: the provider just
                    # rejected it, and rejected blobs never turn valid.
                    _remember_stale(send_body)
                    # The healer says nothing when nothing is safe to heal.
                    if _try_heal(
                            client, lane, host, port, method, path, headers,
                            body, model, emit, seq, call_n, relay):
                        break
                # Healer passed or failed: the original answer stands.
                # (held always carries the head here: a status line only
                # exists because one was read from upstream.)
                client.sendall(held)
                break
            if status == 400:
                # Not a model call, so no body to heal: forward untouched.
                client.sendall(held)
            else:
                # Whatever else answered will issue the next turn's blobs:
                # pin the session to this lane so its exits only ever
                # validate what they issued (zero 400s, zero heals).
                _remember_owner(session, lane)
            break
        conn_hdr = headers.get("connection")
        if isinstance(conn_hdr, bytes):
            conn_hdr = conn_hdr.decode("latin1", "replace")
        if "close" in str(conn_hdr or "").lower():
            # The client asked for one shot; don't wait for the next head.
            return


def _roundtrip(client: ssl.SSLSocket, lane: Lane, host: str, port: int,
               method: str, path: str, headers: dict, body: bytes,
               emit, seq: int, call_n: int, t0: float
               ) -> "tuple[str, int, bytes, bool]":
    """Returns (err, status, held, retryable). A 429 is fully buffered into
    ``held`` instead of forwarded, so the caller can retry on a fresh lane;
    a 400 is buffered too (error pages are tiny) so the caller can inspect
    it and decide: heal, then forward. ``retryable`` is True only when
    nothing reached the client yet."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Fresh circuits are slow to deliver their first bytes; a cold lane gets
    # a long first-read budget, then the ceiling tightens to the steady
    # per-read limit once the stream is provably alive.
    sock.settimeout(_FIRST_BYTE_TIMEOUT)
    try:
        sock.connect(("127.0.0.1", lane.socks_port))
        err = netutil.socks5_open(sock, host, port)
        if err:
            return err, 0, b"", True
        up = ssl.create_default_context().wrap_socket(sock,
                                                      server_hostname=host)
    except (ssl.SSLError, OSError) as exc:
        sock.close()
        err = f"{type(exc).__name__}"
        emit({"type": "callend", "t": time.time(), "n": seq, "c": call_n,
              "lane": lane.index, "cc": lane.exit_country, "status": 0,
              "kb": 0, "secs": round(time.time() - t0, 1), "err": err})
        return err, 0, b"", True

    total = 0
    held = bytearray()

    def _send(data: bytes) -> None:
        nonlocal total
        total += len(data)
        if held is not None:
            held.extend(data)
        else:
            client.sendall(data)

    try:
        # Rebuild the request head; force identity-ish framing we understand.
        out_head = f"{method} {path} HTTP/1.1\r\n".encode("latin1")
        skip = {"connection", "keep-alive", "proxy-authenticate",
                "proxy-authorization", "te", "trailer", "transfer-encoding",
                "upgrade", "content-length"}
        for k, v in headers.items():
            if k in skip:
                continue
            vv = v.decode("latin1") if isinstance(v, bytes) else v
            out_head += f"{k}: {vv}\r\n".encode("latin1")
        out_head += f"content-length: {len(body)}\r\n".encode()
        out_head += b"connection: close\r\n\r\n"
        up.sendall(out_head + body)

        uf = up.makefile("rb")
        rhead = _read_head(uf)
        if rhead is None:
            err = "upstream closed"
            emit({"type": "callend", "t": time.time(), "n": seq, "c": call_n,
                  "lane": lane.index, "cc": lane.exit_country, "status": 0,
                  "kb": 0, "secs": round(time.time() - t0, 1), "err": err})
            return err, 0, b"", True
        # Upstream answered: the circuit is alive, tighten the ceiling.
        # (The raw sock's fd is owned by the SSL socket after wrap_socket,
        # so the timeout lives on ``up`` from here on.)
        up.settimeout(_READ_TIMEOUT)
        status = 0
        try:
            status = int(rhead.split(b" ", 2)[1])
        except (IndexError, ValueError):
            pass
        rheaders = {}
        for raw in rhead.split(b"\r\n")[1:]:
            if raw and b":" in raw:
                k, v = raw.split(b":", 1)
                rheaders[k.strip().lower()] = v.strip()

        if status not in (429, 400):
            held = None  # stream straight to the client from here on
        _send(rhead)

        if b"chunked" in rheaders.get(b"transfer-encoding", b""):
            # Stream chunk frames verbatim -- SSE flows through as it lands.
            while True:
                size_line = uf.readline()
                if not size_line:
                    break
                _send(size_line)
                try:
                    size = int(size_line.strip().split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    # Trailer: header lines (usually none), then a bare CRLF.
                    while True:
                        tl = uf.readline()
                        if not tl:
                            break
                        _send(tl)
                        if tl in (b"\r\n", b"\n"):
                            break
                    break
                _send(_read_exact(uf, size + 2))
        elif b"content-length" in rheaders:
            remaining = int(rheaders[b"content-length"])
            while remaining > 0:
                chunk = uf.read(min(65536, remaining))
                if not chunk:
                    break
                _send(chunk)
                remaining -= len(chunk)
        else:
            while True:
                chunk = uf.read(65536)
                if not chunk:
                    break
                _send(chunk)

        emit({"type": "callend", "t": time.time(), "n": seq, "c": call_n,
              "lane": lane.index, "cc": lane.exit_country, "status": status,
              "kb": round(total / 1024, 1),
              "secs": round(time.time() - t0, 1), "err": ""})
        return "", status, bytes(held or b""), False
    except (ssl.SSLError, OSError) as exc:
        err = f"{type(exc).__name__}"
        retryable = held is not None or total == 0
        emit({"type": "callend", "t": time.time(), "n": seq, "c": call_n,
              "lane": lane.index, "cc": lane.exit_country, "status": 0,
              "kb": round(total / 1024, 1),
              "secs": round(time.time() - t0, 1), "err": err})
        return err, 0, b"", retryable
    finally:
        try:
            up.close()
        except Exception:  # noqa: BLE001
            pass
