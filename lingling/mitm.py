"""Per-request MITM for opencode.ai: a local CA terminates TLS so each model
call is visible, then re-encrypts through a lane's SOCKS5 tunnel.
Blocking sockets on daemon threads, off the asyncio loop."""

from __future__ import annotations

import datetime
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

#: Hosts unwrapped; the rest tunnel blind.
MITM_HOSTS = ("opencode.ai",)

#: Cold-circuit first-byte budget.
_FIRST_BYTE_TIMEOUT = 90.0
#: Mid-stream per-read ceiling (LINGLING_STREAM_TIMEOUT).
_READ_TIMEOUT = float(os.environ.get("LINGLING_STREAM_TIMEOUT", "45"))
#: Streams per lane before calls queue up (LINGLING_LANE_CONCURRENCY).
_LANE_CAP = int(os.environ.get("LINGLING_LANE_CONCURRENCY", "2"))
#: Wait budget for free capacity or a cooking lane (LINGLING_LANE_WAIT).
_LANE_WAIT = float(os.environ.get("LINGLING_LANE_WAIT", "90"))

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


def _force_connection_close(head: bytes) -> bytes:
    """Always advertise ``Connection: close``: safest contract for a proxy
    that may drop the tunnel anytime (no pooled-socket resets)."""
    lines = head.split(b"\r\n")
    out = [lines[0]]
    replaced = False
    i = 1
    while i < len(lines) and lines[i]:
        ln = lines[i]
        if ln[:10].lower() == b"connection" and b":" in ln:
            out.append(b"Connection: close")
            replaced = True
        else:
            out.append(ln)
        i += 1
    if not replaced:
        out.append(b"Connection: close")
    out.extend(lines[i:])  # terminator verbatim
    return b"\r\n".join(out)


def _grab_lane(relay, tried: set):
    """Capacity-aware pick; waits rather than stacking one exit."""
    deadline = time.time() + _LANE_WAIT
    while True:
        lane = relay.pick_lane(exclude=tried, max_active=_LANE_CAP)
        if lane is not None:
            return lane
        if time.time() >= deadline:
            return relay.pick_lane(exclude=tried)  # last resort
        time.sleep(0.25)


def handle_conn(raw: socket.socket, host: str, port: int, seq: int,
                shop: CertShop, manager: TorManager,
                emit: Callable[[Dict], None], relay) -> None:
    """Own one intercepted TLS connection end to end (blocking thread)."""
    try:
        # dup'd socket arrives non-blocking.
        raw.setblocking(True)
        raw.settimeout(300)  # idle ceiling
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
        # idle ceiling or vanished client.
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def _serve(client: ssl.SSLSocket, host: str, port: int, seq: int,
           manager: TorManager, emit: Callable[[Dict], None], relay) -> None:
    """Serve model calls on one client TLS connection; a lane per call."""
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

        held = b""
        tried = set()
        while True:
            # spread, never stampede.
            lane = _grab_lane(relay, tried)
            if lane is None:
                # nothing left: last body wins.
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
                    body, emit, seq, call_n, t0, relay)
            finally:
                with lane.lock:
                    lane.active -= 1
            if err:
                # nothing reached the client: free retry.
                relay.report_stall(lane, hard=retryable)
                if retryable:
                    tried.add(lane.index)
                    continue
                return
            if status == 429:
                relay.report_burn(lane)
                tried.add(lane.index)
                continue
            break


def _roundtrip(client: ssl.SSLSocket, lane: Lane, host: str, port: int,
               method: str, path: str, headers: dict, body: bytes,
               emit, seq: int, call_n: int, t0: float, relay: "object"
               ) -> "tuple[str, int, bytes, bool]":
    """One upstream attempt; 429s buffer into ``held`` for a lane retry."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # cold lane: long first-byte budget.
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
    clean_end = False       # terminator seen
    saw_content = False     # real output seen
    saw_terminal = False    # terminal event seen
    _tail = b""             # split-read guard

    def _send(data: bytes) -> None:
        nonlocal total, saw_content, saw_terminal, _tail
        total += len(data)
        if held is not None:
            held.extend(data)
            return
        # catch ghost streams.
        window = _tail + data
        saw_content = (saw_content or b"output_text.delta" in window
                       or b"function_call" in window)
        saw_terminal = (saw_terminal or b"response.completed" in window
                        or b"response.incomplete" in window
                        or b"response.failed" in window)
        _tail = window[-64:]
        client.sendall(data)

    try:
        # rebuild head, identity framing.
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
        # stream alive: tighten ceiling.
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

        if status != 429:
            held = None  # stream to client now
        _send(_force_connection_close(rhead))

        if b"chunked" in rheaders.get(b"transfer-encoding", b""):
            # forward frames verbatim.
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
                    # trailer, then blank line.
                    while True:
                        tl = uf.readline()
                        if not tl:
                            break
                        _send(tl)
                        if tl in (b"\r\n", b"\n"):
                            break
                    clean_end = True
                    break
                chunk = _read_exact(uf, size + 2)
                _send(chunk)
                if len(chunk) < size + 2:
                    break  # died mid-chunk
        elif b"content-length" in rheaders:
            remaining = int(rheaders[b"content-length"])
            while remaining > 0:
                chunk = uf.read(min(65536, remaining))
                if not chunk:
                    break
                _send(chunk)
                remaining -= len(chunk)
            clean_end = remaining <= 0
        else:
            while True:
                chunk = uf.read(65536)
                if not chunk:
                    break
                _send(chunk)
            clean_end = True  # EOF is the end.

        streamed = held is None
        is_sse = b"text/event-stream" in rheaders.get(b"content-type", b"")
        ghost = (status == 200 and streamed and is_sse
                 and not saw_content and not saw_terminal)
        emit({"type": "callend", "t": time.time(), "n": seq, "c": call_n,
              "lane": lane.index, "cc": lane.exit_country, "status": status,
              "kb": round(total / 1024, 1),
              "secs": round(time.time() - t0, 1), "err": "",
              "cut": not clean_end, "ghost": ghost})
        if streamed and (ghost or not clean_end):
            # poison: pull it.
            relay.report_stall(
                lane,
                why=("kept serving empty streams" if ghost
                     else "kept cutting streams mid-body"))
        return "", status, bytes(held or b""), False
    except (ssl.SSLError, OSError) as exc:
        err = f"{type(exc).__name__}"
        retryable = held is not None or total == 0
        emit({"type": "callend", "t": time.time(), "n": seq, "c": call_n,
              "lane": lane.index, "cc": lane.exit_country, "status": 0,
              "kb": round(total / 1024, 1),
              "secs": round(time.time() - t0, 1), "err": err,
              "cut": True})
        return err, 0, b"", retryable
    finally:
        try:
            up.close()
        except Exception:  # noqa: BLE001
            pass
