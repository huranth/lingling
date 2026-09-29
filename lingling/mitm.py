"""Per-request MITM for opencode.ai: a local CA terminates TLS so each model
call is visible, then re-encrypts through a lane's SOCKS5 tunnel.
Blocking sockets on daemon threads, off the asyncio loop."""

from __future__ import annotations

import datetime
import json
import os
import select
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import netutil
from .lanes import Lane

#: unwrapped hosts
MITM_HOSTS = ("opencode.ai",)

#: the dial
_FIRST_BYTE_TIMEOUT = float(os.environ.get("LINGLING_FIRST_BYTE_S", "30"))
#: the send
_SEND_TIMEOUT = float(os.environ.get("LINGLING_SEND_S", "120"))
#: pre-commit idle
_READ_TIMEOUT = float(os.environ.get("LINGLING_STREAM_TIMEOUT", "1800"))
#: post-commit idle
_STREAM_IDLE_TIMEOUT = float(os.environ.get("LINGLING_STREAM_IDLE_S", "1800"))
#: first event
_FIRST_EVENT = b"data:"
#: idle tunnel
_KEEPALIVE_S = float(os.environ.get("LINGLING_KEEPALIVE_S", "600"))

#: retryable
_RETRYABLE = frozenset({429, 502, 503, 504})
#: 5xx attempts
_5XX_ATTEMPTS = 2
#: note bytes
_NOTE_BYTES = 512

#: cut capture
_CAPTURE = os.environ.get("LINGLING_CAPTURE", "")
#: capture bytes
_CAPTURE_MAX = int(os.environ.get("LINGLING_CAPTURE_BYTES", "8192"))

#: pre-commit cap
_PRE_COMMIT_MAX = 65536

_ca_lock = threading.Lock()
_ca_ctx: Dict[Path, "tuple"] = {}  #: ca cache


def crypto_available() -> bool:
    """True when the crypto stack the MITM needs actually imports.

    `cryptography` is imported inside the functions that use it, so a broken
    install only fails when the first request arrives. A gutted package -- no
    `__init__.py`, orphaned dist-info, pip still reporting it installed -- got
    all the way through boot and a live soak before anything complained. This
    lets startup say so instead."""
    try:
        from cryptography import x509  # noqa: F401
        from cryptography.hazmat.primitives import hashes  # noqa: F401
        from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: F401
        return True
    except ImportError:
        return False


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


def _read_head(f, on_first=None, sock=None, ceiling=None) -> Optional[bytes]:
    """Read one HTTP head (request or status line + headers) through CRLFCRLF.

    ``on_first`` fires once, on byte one. That is the only honest place to
    stamp a first-byte latency: the head completes far later than it starts.

    ``sock``/``ceiling`` hold the deadline at ``ceiling``. A socket timeout is
    per blocking read, so it is already an idle ceiling -- "this long with
    nothing coming", not a total budget for the head. Re-arming on each byte
    only guarantees that whatever the caller left the socket at, the ceiling
    is what applies. The model thinks before its first token, and a fixed total
    budget was killing streams that were merely slow to start -- 67 timeouts
    over 24h, all of them 0 bytes at 30s or 45s, on lanes that answered 200
    either side.

    Silence is deliberately NOT read as a lane having gone. The upstream holds
    the head until the model produces its first token -- measured, median
    0.03s between the head and the first event -- so a silent-but-open
    connection is a model thinking, and the ceiling is generous for exactly
    that reason. EOF is the signal that a lane has actually gone, and it
    arrives at once. See ``_roundtrip``."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = f.read(1)
        if not chunk:
            return None
        if on_first is not None:
            # first arrival
            on_first()
            on_first = None
        if sock is not None and ceiling is not None:
            # arm again
            sock.settimeout(ceiling)
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


def _read_body(cf, headers) -> Optional[bytes]:
    """The client's request body, or None if it did not arrive whole.

    `_read_exact` returns whatever it got when the peer stops early, so a
    client that closes mid-body used to have its PARTIAL body forwarded -- with
    a matching `Content-Length`, so the far end sees a complete-but-truncated
    request and may answer `400 [invalid_request_error] Invalid upload request`.
    That burns a lane and a slice of quota on a request that can never succeed.

    Nobody is listening by then either: the client is the thing that closed.
    So the honest move is to drop the request rather than half-send it.

    A body that arrived whole, and a request with no body at all, both pass."""
    cl = headers.get("content-length")
    if not cl:
        return b""
    try:
        want = int(cl)
        body = _read_exact(cf, want)
    except (ValueError, OSError):
        return None
    if len(body) != want:
        return None
    return body


def _model_of(body: bytes) -> str:
    """The model, reasoning effort, and output cap of one request body.

    Rides along so the model path costs no extra parse. The effort matters
    because the owner's report is that the stalls only happen on hard tasks,
    and until this was logged that could not be checked at all -- the body
    was never recorded, so the correlation had no data behind it. Measured
    over the log, a healthy stream sends its first event within 4.28s of its
    head (p50 0.24s, n=1049), so a stall after that point is a different
    animal from one before it, and effort is the variable that would explain
    why."""
    try:
        head = json.loads(body.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return ""
    model = str(head.get("model", ""))
    reasoning = head.get("reasoning") or {}
    effort = reasoning.get("effort", "") if isinstance(reasoning, dict) else ""
    effort = effort or head.get("reasoning_effort", "")
    cap = head.get("max_output_tokens", "")
    if effort or cap:
        return f"{model} effort={effort or '-'} cap={cap or '-'}"
    return model


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


def _dump_cut(seq: int, call_n: int, lane, err: str, cap: bytes,
              total: int) -> None:
    """Write a cut stream's bytes, so the dying point can be read.

    A committed stream that dies delivers a fixed ~1 KB every time and it is
    the same offset across every lane, so the bytes say what the last complete
    SSE event before death was. Nothing else in the log records content, and a
    hand-rolled probe cannot reproduce it -- the free tier gates on the client,
    so only real opencode traffic produces this shape. Off unless
    ``LINGLING_CAPTURE`` names a directory."""
    if not _CAPTURE:
        return
    try:
        out = Path(_CAPTURE)
        out.mkdir(parents=True, exist_ok=True)
        stem = f"n{seq}-c{call_n}-lane{lane.index}-{int(time.time())}"
        (out / f"{stem}.bin").write_bytes(cap)
        (out / f"{stem}.txt").write_text(
            f"err={err}\nlane={lane.index} cc={lane.exit_country} "
            f"ip={lane.exit_ip}\nkb={total / 1024:.1f}\n"
            f"captured={len(cap)}\n", encoding="utf-8")
    except OSError:
        pass


def _grab_lane(relay, tried: set):
    """The least-loaded lane we have not tried yet.

    No cap and no waiting. A lane that is healthy carries the request -- it
    may be carrying others, and that is fine, because the alternative was to
    hold traffic while capacity sat idle and to print "at cap" about a limit
    we invented rather than about anything the lane was doing."""
    return relay.pick_lane(exclude=tried)


def _note_timeout(relay, lane, emit, seq: int, call_n: int) -> None:
    """Charge a timeout to its lane and demolish it on the third.

    Everything that reaches here has already spoken. The cold connect -- the
    SOCKS dial and the TLS handshake -- sits in its own ``try`` above and
    returns before this is ever called, so a TimeoutError raised by the dial
    is structurally unchargeable. Measured over the log, that matters: 103 of
    the 113 ``TimeoutError``s carried ``first_byte_s == 0``, the far end never
    opened its mouth, on a cold circuit -- and 30 of 30 sampled still served a
    200 within 15 minutes. The 11 that did speak first delivered 48-62 KB
    before going quiet, larger bodies than most 200s, so that is a stall worth
    counting. A cold start costs no budget; a mute lane still loses its third
    strike.

    The counter is per lane and only a 200 clears it -- other lanes succeeding
    in between does not, which is the whole signal: a lane timing out while
    its neighbours return 200 is a bad circuit, not a busy pool. The rebuild
    runs off-thread because it drains and re-cooks a tor process, and a
    request must never wait on that."""
    moved = relay.tor.note_timeout(lane)
    if not moved:
        return
    emit({"type": "lane", "kind": "timeout", "t": time.time(),
          "lane": lane.index, "cc": lane.exit_country, "ip": lane.exit_ip,
          "msg": moved})


def handle_conn(raw: socket.socket, host: str, port: int, seq: int,
                shop: CertShop, emit: Callable[[Dict], None],
                relay) -> None:
    """Own one intercepted TLS connection end to end (blocking thread).

    The `call` record carries the request BODY SIZE, because a provider
    rejection cannot otherwise be told apart from a transport fault. The far
    end answered one POST with `400 [invalid_request_error] Invalid upload
    request` after 100s; nothing in the log could say whether that body was
    oversized, and no other field can recover it after the fact."""
    try:
        # non-blocking
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
        _serve(client, host, port, seq, emit, relay)
    except (OSError, ssl.SSLError, TimeoutError):
        # idle ceiling
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def _serve(client: ssl.SSLSocket, host: str, port: int, seq: int,
           emit: Callable[[Dict], None], relay) -> None:
    """Serve model calls on one client TLS connection; a lane per call.

    Retry rules, in order of evidence:

      * a 429 names its own exit as limited -- retire the exit, retry
        elsewhere, and stop when no untried lane can still serve;
      * a 5xx is the far edge, which every exit rides -- another exit cannot
        route around a bad edge, it can only re-roll it. So 5xx stops after
        ``_5XX_ATTEMPTS`` attempts: the owner's log shows one call spending
        four attempts and four 3.4 MB uploads on four lanes, all 503, inside
        three minutes, and the next call doing it again with 504s. Two
        attempts still covers the single-exit bad moment; an edge-wide one
        burns two uploads instead of a lap of the pool.
      * a transport error before anything reached the client is free to
        retry, and the newest buffered verdict survives it.

    When the retries stop -- by cap, by 429 exhaustion, or because no lane is
    left -- the client gets the newest buffered verdict WHOLE: head and body.
    The client saw nothing on the failed attempts, so delivering the far
    end's own 429/503 is honest where an invented 502 was not.
    """
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

        body = _read_body(cf, headers)
        if body is None:
            return
        model = _model_of(body) if body and method == "POST" else ""
        call_n += 1

        held = b""
        tried = set()
        bad5xx = 0
        while True:
            # spread load
            lane = _grab_lane(relay, tried)
            if lane is None:
                # last body
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
                # request size
                "bytes": len(body),
            })
            with lane.lock:
                lane.active += 1
            try:
                err, status, got_held, retryable = _roundtrip(
                    client, lane, host, port, method, path, headers,
                    body, emit, seq, call_n, t0, relay,
                    # only once
                    charge_timeout=not tried)
            finally:
                with lane.lock:
                    lane.active -= 1
            if got_held:
                # freshest verdict
                held = got_held
            if err:
                # free retry
                if retryable:
                    tried.add(lane.index)
                    continue
                return
            if status in _RETRYABLE:
                if status == 429:
                    relay.report_refused(lane, status)
                    tried.add(lane.index)
                    if not relay.any_unlimited(tried):
                        # none left
                        if held:
                            client.sendall(held)
                        return
                    continue
                # far edge
                tried.add(lane.index)
                bad5xx += 1
                if bad5xx >= _5XX_ATTEMPTS:
                    # last verdict
                    if held:
                        client.sendall(held)
                    return
                continue
            # score it
            relay.tor.note_result(lane.exit_country, status)
            if status == 200:
                relay.tor.note_ok(lane)
            break


def _close_quiet(sock) -> None:
    """Close without caring whether it was already gone."""
    try:
        sock.close()
    except OSError:
        pass


def _peer_closed(sock) -> bool:
    """True when the far end has hung up on a socket we left idle.

    A closed socket reads as ready, and we are never expecting bytes on an
    idle tunnel, so "readable" means "finished"."""
    try:
        return bool(select.select([sock], [], [], 0)[0])
    except (OSError, ValueError):
        return True


class TunnelPool:
    """Idle upstream connections, kept per lane.

    A lane's SOCKS5 CONNECT plus TLS handshake costs about 1.3s of Tor
    circuit round trips -- measured 546ms + 723ms on a warm circuit -- and
    buys nothing on the next request to the same host. Keeping the socket
    costs one file descriptor. A stale entry is harmless: `take` checks
    whether the peer has already closed before handing it over.

    **The TTL IS the reuse rate, and 30s was too short for this traffic.**
    That is not a theory; it is arithmetic. The owner's requests arrive
    strictly sequentially, one at a time, round-robin across five lanes -- so
    the gap between two requests on the SAME lane is one full lap. Measured
    over his log, that gap has a median of 70.2s and, in the session that
    produced 34 calls at 0% reuse, a **minimum of 98.8s**. A tunnel that must
    be reused within 30s can therefore never be reused at all on this traffic,
    and the 0% was the TTL working exactly as written -- not a far end sending
    `Connection: close`, which was the first suspect and is now logged
    (`peer_close`) so it can be ruled in or out from data rather than argued.

    What 0% costs is the whole dial, every request: 546ms + 723ms of Tor round
    trips, plus the TLS handshake, paid before the body is even pushed. On a
    young circuit it is worse, and a young circuit is also where the SSLEOFs
    and the SOCKS build failures live -- the far end answers a first TLS
    session on a fresh circuit differently from a warm one. So a short TTL
    charges the user latency AND manufactures the exact conditions that
    produce the errors that look like lane faults.

    600s covers 84% of his same-lane gaps while still bounding how stale a
    socket can get, and staleness is already handled at `take`: a peer that
    hung up is caught by `_peer_closed`, a lane that changed exit by the IP
    check. The worst case a long TTL can produce is one wasted attempt on a
    socket the far end had dropped -- which retries on another lane for free,
    because nothing has reached the client yet."""

    def __init__(self, ttl: float = _KEEPALIVE_S) -> None:
        self.ttl = ttl
        self._idle: Dict[int, List[Tuple[float, str, object, object]]] = {}
        self._lock = threading.Lock()

    def take(self, lane: Lane) -> Optional[Tuple[object, object]]:
        """Newest usable idle tunnel for a lane, or None."""
        with self._lock:
            slots = self._idle.get(lane.index)
            if not slots:
                return None
            when, ip, up, fil = slots.pop()
        if time.monotonic() - when > self.ttl:
            _close_quiet(up)  # too old
            return None
        if ip != lane.exit_ip:
            _close_quiet(up)  # wrong country
            return None
        if _peer_closed(up):
            _close_quiet(up)  # hung up
            return None
        return up, fil

    def give(self, lane: Lane, up, fil) -> None:
        """Hand a tunnel back after a response that ended cleanly.

        Stale entries are pruned here rather than left for `take` to find one
        at a time. A lane that changes exit leaves every earlier tunnel
        pointing at the wrong relay, and since only `take` removed entries the
        list held those sockets open indefinitely -- one per request that
        outlived its relay. Anything past `ttl` or belonging to another exit is
        closed now, so the list is bounded by the lane's concurrency."""
        now = time.monotonic()
        with self._lock:
            slots = self._idle.setdefault(lane.index, [])
            keep = []
            for when, ip, old_up, _old_fil in slots:
                if (now - when) > self.ttl or ip != lane.exit_ip:
                    _close_quiet(old_up)  # prune
                    continue
                keep.append((when, ip, old_up, _old_fil))
            keep.append((now, lane.exit_ip, up, fil))
            self._idle[lane.index] = keep


def _note_of(raw: bytes) -> str:
    """The error body as one readable line, for the pane.

    opencode answers a limited exit or an unwell edge with a short body, and
    until now that body was buffered for the retry and then thrown away -- the
    pane showed `503 0.2 KB` with no way to know what the far end actually
    said. The first bytes are enough: measured error bodies run 100-300, well
    inside the capture cap.
    """
    text = " ".join(raw.decode("utf-8", "replace").split())
    return text[:200]


def _retry_after(rheaders: dict) -> float:
    """Seconds the far end says to wait, or 0 when it did not say.

    opencode answers a limited exit with `FreeUsageLimitError` and a numeric
    `retry-after` (measured 7.7h to 13.8h, median 11.0h -- so it counts down,
    and a later 429 carries a smaller number). What it counts down TO is the
    far end's own next window reset, not this exit's recovery: every one of the
    8 owner 429s resolves to the same instant, 05:30:03 local. The header may
    also be an HTTP date, which we ignore."""
    try:
        return max(0.0, float(rheaders.get(b"retry-after", b"0")))
    except (TypeError, ValueError):
        return 0.0


def _lat(t0: float, first_byte: Optional[float],
         first_event: Optional[float]) -> Dict:
    """Offset from request start to first upstream byte / first SSE event.

    Both span the whole attempt, so they include SOCKS connect and TLS
    handshake — that is the wall clock the user actually waits through.
    0 means the attempt never got that far."""
    return {
        "first_byte_s": round(first_byte - t0, 2) if first_byte else 0,
        "first_event_s": round(first_event - t0, 2) if first_event else 0,
    }


def _roundtrip(client: ssl.SSLSocket, lane: Lane, host: str, port: int,
               method: str, path: str, headers: dict, body: bytes,
               emit, seq: int, call_n: int, t0: float, relay: "object",
               charge_timeout: bool = False
               ) -> "tuple[str, int, bytes, bool]":
    """One upstream attempt; 429s buffer into ``held`` for a lane retry.

    The tunnel may come from ``relay.tunnels`` instead of being dialled, which
    skips the SOCKS5 CONNECT and TLS handshake. A pooled tunnel is only reused
    when the far end framed the end of its response, so the socket is never
    handed on mid-body.

    ``secs`` spans the whole attempt -- SOCKS connect, TLS handshake, all of
    it -- so it is NOT comparable to the read ceiling. Measured over the log,
    103 zero-byte timeouts cluster at 31s (42) and 45s (34): the first is the
    20s ceiling plus a cold SOCKS+TLS of about 10s, the second is the 30s
    first-byte window plus the same. Both mean "the far end never opened its
    mouth", which is a cold circuit, not a lane to retire -- 30 of 30 such
    timeouts landed on lanes that served a 200 within 15 minutes.

    **The upstream holds the HTTP head until the model has produced its first
    token.** That is the fact this whole function is shaped around, and it was
    only visible by measuring it: across 2042 clean 200s, `first_event_s`
    minus `first_byte_s` has a median of 0.03s and is under 0.5s in 92% of
    cases. The far end does not send a status line early and stream into it.
    It waits, thinks, and only then sends the head -- so **the head read IS
    the model's time-to-first-token.**

    Which means a ceiling on the head read is not a network timeout at all. It
    is a rule saying "the model must start speaking within N seconds". At 20s
    that rule is wrong for any high reasoning effort: the owner's failing
    calls carry `effort=xhigh`, and his own log shows them cut at 0.0 KB after
    20s -- a working request reported as a failure, then retried onto another
    lane whose model is thinking at exactly the same speed. The retry could
    never win; it only added 20s per lane.

    So both windows are generous and a silent peer is never cut. The signal
    that actually distinguishes a dead lane is **EOF**, not silence: a closed
    or dead circuit raises SSLEOF/EOF immediately and the caller retries, while
    a silent-but-open connection is a model thinking. Measured: the cold
    circuits that do fail arrive as SSLEOF, and every timeout in the log was
    silence with a live connection. Silence means "still working".

    **The DIAL is the exception, and it is short (30s) on purpose.** Whether
    to wait or to retry depends on whether a retry can do better, and the two
    halves answer differently:

      * the dial is building a circuit. Another lane's circuit may already be
        warm, so a retry is genuinely likely to be faster -- waiting 120s on a
        slow dial when a 5s retry was available is strictly worse. Measured:
        one call sat out a 120s dial window and then succeeded at 121.7s.
      * the head wait is a MODEL THINKING. Every other lane runs the same
        model at the same effort, so a retry cannot be faster -- it just adds
        another full wait. Measured: his `xhigh` calls were cut at 20s and
        retried onto lanes thinking at the same speed, forever.

    So: retry a slow dial, wait out a slow model.

    **The send window is a TOTAL BUDGET, and 30s was cutting real uploads.**
    It is 120s now. The proof is the shape of the successes, not a theory
    about how `sendall` times out -- and it took two wrong turns to get here,
    so both are written down.

    His bodies run 4.5 MB and GROW (09-21 median 4.7 MB, 09-22 5.4 MB: it is
    the whole conversation going back up on every turn). Over 107 successful
    200s carrying a body of 1 MB or more:

        send_s  p50 = 10.8s   p90 = 20.8s   max = 29.8s
        send_s > 30s:  0 of 107

    **Zero above 30.** A ceiling is only marginal if the successes approach
    it, and here they pile up against it -- 13 of 107 sit in the 20-30s band
    and not one gets past 29.8s. That truncation is the signature of a total
    budget: if the timeout were a per-wait gap, a 40s upload with short waits
    would sail through and appear in this sample. It never does. The 10 cuts
    are then simply the uploads that needed slightly more, and they are ALL on
    4.5-5.6 MB bodies, spread across lanes 1, 2, 4 and 5 -- a size effect, not
    one bad exit, which is what the first reading of this got wrong.

    The two wrong turns, because they are the reusable lesson:

      * `max_wait_s` was read as the send. It is not: it wraps the whole
        `sendall` in one `_timed` call but only ONE chunk of a body read, so
        it is neither a gap nor a total, and it reaches 257.5s on this log --
        values that CANNOT be sends once the ceiling is 30s. Those are reads.
        Reading it as the send is what argued the window back down to 30s
        after it had correctly been raised. `send_s` is the field that answers
        this question and `max_wait_s` is the field that does not.
      * "A 30s timeout on `sendall` must mean a per-wait stall" was reasoned
        about rather than measured. The distribution above refutes it. When a
        mechanism is uncertain, the successes against the ceiling decide it.

    120s is 4x the highest upload observed and covers a 20 MB body at the
    slowest rate seen (164 KB/s). `send_s` is now on every callend, so the
    next time this needs a value it can be read off rather than argued.

    The two phases still differ, but in RETRYABILITY rather than in how long
    they wait. Before commit nothing has reached the client, so a failure is
    free to retry elsewhere. After commit the bytes are already in front of the
    user, the attempt is NOT retryable, and cutting can achieve nothing except
    truncating an answer that was still arriving.

    **The ceiling is a GAP, never a total.** A socket timeout is per blocking
    read, so every arriving byte re-arms it. A model that thinks for ten
    minutes is not on a ten-minute clock; it is on a clock that resets each
    time the far end emits anything, and its total duration is unbounded. The
    suite pins this: case D runs a stream whose total passes the window
    several times over and it still completes. Only one unbroken silence
    trips it.

    ``_STREAM_IDLE_TIMEOUT`` is therefore 1800s, and the reason is that a
    smaller number has no upside. A stream that is emitting is unaffected at
    any value -- the window only ever measures silence. The one thing a short
    window can do is cut an answer whose pause happened to be long, and the
    longest gap measured on a healthy stream is 16.6s, which is why even the
    old 20s ceiling was cutting real answers. The single cost of a long window
    is a peer that is alive but permanently silent: that holds one thread for
    30 minutes rather than 5. With ``_MAX_CONNS`` at 64 that is bounded, and
    it beats destroying an answer a reasoning model was still producing.
    ``LINGLING_STREAM_IDLE_S`` overrides it.

    A refused dial takes the lane out of rotation at once: `lane.healthy` goes
    False the moment `sock.connect` is refused. Nothing else says so until the
    health daemon's next sweep, and `pick_lane` filters on `healthy`, so the
    lane keeps being handed out and every request that lands on it pays a
    refused dial first. Measured: 15 refusals, 17 of one run's errors on a
    single lane. The daemon brings the lane back -- but only once it has been
    taken out of rotation.

    `expect` is stripped from the forwarded headers, because this function
    ANSWERS it: `handle_conn` sends the client a local `100 Continue` so it
    will send its body. Passing the header on anyway, while sending the body
    immediately, is a protocol violation -- RFC 7231 5.1.1 says a proxy that
    responds to the expectation itself must not forward it. The upstream is
    then entitled to refuse the request WITHOUT reading the body, and
    `400 [invalid_request_error] Invalid upload request` is exactly what an
    upload path bailing before it ingests anything looks like.

    The far end's own `Connection: close` is honoured before a tunnel is
    pooled. It was never read, so a complete response on a connection the far
    end had asked to close still went back into the pool -- and the next reuse
    met an EOF that the pool's `_peer_closed` check had not seen yet, because
    the close had not finished propagating through Tor. ``keep`` now means the
    response is complete AND the far end did not ask for close.

    A malformed ``Content-Length`` from the far end is caught rather than
    escaping. It is parsed unguarded, and the handler took only
    ``(ssl.SSLError, OSError)`` -- so a ``ValueError`` flew straight out of this
    function, killed the MITM thread, and left the client hanging on a
    connection nobody would ever close. A bad length makes the message
    unparseable, so failing the call is right; escaping the handler is not.

    Nothing reaches the client until the far end has produced a model event.
    The head and the opening body bytes are held and flushed together on the
    first SSE event, so a stream that dies before then is free to retry -- the
    client saw nothing, and the caller's retry loop hands the request to
    another lane. This is not a nicety: measured over the log, 102 of 112
    ``TimeoutError``s arrived with zero bytes, i.e. the far end never said
    anything at all, and on the owner's own traffic 5 of 7 ``SSLEOFError``s did
    the same. Both land on younger circuits than the calls that go through --
    SSLEOF p50 41s against 102s for the 200s -- which is what a freshly built
    Tor circuit does to its first TLS session.

    A non-stream body commits as soon as it is parsed, and a retryable status
    never commits at all -- a held 429 or 503 must reach the caller untouched
    or the retry would send a second response. Once a model event is through,
    the attempt is committed: a mid-body cut is not retryable and must not be,
    because those bytes are already in front of the user.

    Every ``callend`` carries the same keys, the wait fields included. A key
    written on only some emit paths is worse than a missing one: a query
    filters on it, silently undercounts, and the shortfall reads as evidence.
    That has happened five times on this log -- `reused` missing from errors
    turned "are SSLEOFs on reused tunnels?" into a confident "0 of 149".

    ``status`` on a callend is what the CLIENT saw, not what the far end sent.
    Before commit the head is withheld, so a death there is a 0 -- nothing was
    delivered and the attempt is free to retry. After commit the client holds
    a 200 and part of a body, and that must be reported as 200: hardcoding 0
    on the error path made every truncated answer count as a total miss,
    understating the success rate by 151 calls on this log.

    **A retryable verdict is held WITH its head, and the first bytes of an
    error body ride the callend as ``note``.** The held buffer is what the
    caller delivers when the retries stop, so it must be a complete HTTP
    response -- head first, then body -- and it is the only record of what
    the far end said, which the pane shows. A 5xx itself is the caller's
    business: it caps them (see ``_serve``), because no exit can route
    around an edge that is unwell."""
    first_byte_at = None   # byte one
    first_event_at = None  # event one
    # above dial
    max_wait = 0.0         # longest wait
    client_wait = 0.0      # longest client
    send_wait = 0.0        # longest push
    sent = 0               # client bytes
    peer_close = False     # far end
    last_op = "upstream"   # which side
    status = 0             # upstream status
    pool = getattr(relay, "tunnels", None)

    def _keys() -> Dict:
        """The keys every callend carries, in one place.

        Not named for the tail it builds: `_tail` is already the split-read
        guard below, and shadowing it turned every emit into a TypeError --
        caught by `tools/verify/verify_pool_ttl.py`, which drives the real
        `_roundtrip` rather than inspecting the source.

        `send_s` is split out of `max_wait_s` because the two answer different
        questions and the log could not tell them apart. A `max_wait_s` of
        exactly 30.0 with `first_byte_s` of 0 was read as a READ ceiling for
        three sessions running -- it was the SEND window, and the reason it
        fired is that every one of the owner's requests carries a 4.5 MB
        upload. The read ceiling is 1800s, so it was never the reader.

        `peer_close` records the far end's own `Connection: close`. Whether it
        sends one decides whether ANY tunnel can be pooled, so it is the first
        thing to read when the reuse rate moves -- and it was invisible until
        now, which left the pool TTL as the only suspect for a reuse collapse
        the TTL fully explained anyway.

        `client_kb` is what the CLIENT actually received, against `kb` which is
        what the far end sent. **They are never equal**: `client_kb` carries the
        head this proxy rebuilds as well, so it is always slightly larger --
        measured over 20 calls, `client_kb - kb` is 0.0 to 0.4 KB with a median
        of 0.2 KB, and `client_kb < kb` has never been seen. The comparison
        that matters is therefore "did it get at least what we relayed", not
        equality.

        It exists for the `200 cut (SSLEOFError) [client stalled]` line, which
        is the one outcome the pane cannot explain on its own. 166 of those
        exist in the log with `client_wait_s` of 0.0 on every single one -- the
        write never waited, it raised at once, so the client had ALREADY closed
        and the proxy was merely the last to notice. What that does not say is
        whether the client left with the whole answer or half of it, and
        `cut=True` cannot be read either way without this field: the chunked
        terminator is written AFTER the last content chunk, so a client that
        closes the moment it has everything makes the NEXT write fail and the
        answer is logged as cut.

        The first live instance confirms the shape: `kb=1.3 client_kb=1.5
        stalled=client` on a 200 -- the client received every byte the far end
        sent and then hung up. Note what it still does NOT prove: `kb` is only
        what had ARRIVED, so a client that leaves mid-stream also shows
        `client_kb >= kb`. This tells you the client was not starved; it does
        not by itself tell you the answer was complete. Note also that it
        undercounts by at most one partial `sendall`."""
        return {**_lat(t0, first_byte_at, first_event_at),
                "send_s": round(send_wait, 1),
                "peer_close": peer_close,
                "client_kb": round(sent / 1024, 1)}

    kept = pool.take(lane) if pool is not None else None
    uf = None
    if kept is not None:
        up, uf = kept
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # cold lane
        sock.settimeout(_FIRST_BYTE_TIMEOUT)
        try:
            sock.connect(("127.0.0.1", lane.socks_port))
            err = netutil.socks5_open(
                sock, host, port,
                cred=netutil.slot_cred(lane.index, seq + call_n))
            if err:
                # lane dead
                _close_quiet(sock)
                emit({"type": "callend", "t": time.time(), "n": seq,
                      "c": call_n, "lane": lane.index,
                      "cc": lane.exit_country, "status": 0, "kb": 0,
                      "secs": round(time.time() - t0, 1), "err": err,
                      "cut": True,
                      "note": "",
                      "max_wait_s": round(max_wait, 1),
                      "client_wait_s": round(client_wait, 1),
                      "reused": kept is not None,
                      **_keys()})
                return err, 0, b"", True
            up = ssl.create_default_context().wrap_socket(
                sock, server_hostname=host)
        except (ssl.SSLError, OSError) as exc:
            sock.close()
            err = f"{type(exc).__name__}"
            if err == "ConnectionRefusedError":
                # lane down
                lane.healthy = False
            emit({"type": "callend", "t": time.time(), "n": seq, "c": call_n,
                  "lane": lane.index, "cc": lane.exit_country, "status": 0,
                  "kb": 0, "secs": round(time.time() - t0, 1), "err": err,
                  "cut": True,
                  "note": "",
                  "max_wait_s": round(max_wait, 1),
                  "client_wait_s": round(client_wait, 1),
                  "reused": kept is not None,
                  **_keys()})
            return err, 0, b"", True

    total = 0
    held = bytearray()
    note_buf = bytearray()  # error body
    want_note = False       # error capture
    clean_end = False       # terminator seen
    keep = False            # tunnel reusable
    retry_after = 0.0       # server reset
    is_sse = False          # event stream
    _tail = b""             # split-read guard
    _open = bytearray()     # pre-commit bytes
    _cap = bytearray()      # cut capture
    _committed = False      # client content

    def _stamp_byte() -> None:
        nonlocal first_byte_at
        first_byte_at = time.time()

    def _timed(fn, *args, **kwargs):
        """One blocking upstream read, timed.

        `max_wait_s` on the callend says how close a call came to the read
        ceiling. That is what a ceiling can be set from -- the log records no
        per-chunk timing, so a soak with this field is the only way to see the
        real gap distribution instead of guessing at it.

        `last_op` records WHICH SIDE was blocked, because the two sockets have
        separate timeouts and a stall on one must not be charged to the other.
        A call that ran 302.8s with `max_wait_s` of 2.2 is proof of that: no
        upstream read was anywhere near a ceiling, so the timeout had to be a
        `client.sendall` blocking on a client that had stopped reading.

        The timing lives in a `finally` because a read that RAISES is the one
        whose duration matters most. An update after the call skips it, which
        is why a 20s timeout used to log `max_wait_s 0.0` and a 302.8s stall
        logged 2.2 -- the longest read that SUCCEEDED."""
        nonlocal max_wait, last_op
        last_op = "upstream"
        t = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            # even raising
            waited = time.monotonic() - t
            if waited > max_wait:
                max_wait = waited

    def _to_client(data: bytes) -> None:
        """One send to the client, timed apart from the upstream reads.

        The client socket carries its own 300s timeout (`handle_conn`). When a
        client stops reading, this blocks for that whole window and raises
        TimeoutError -- which says nothing whatsoever about the lane. Timing it
        separately is what lets the caller tell the two apart, and the timing
        sits in a `finally` for the same reason as `_timed`: a send that raises
        is exactly the one whose duration has to be recorded."""
        nonlocal client_wait, last_op, sent
        last_op = "client"
        t = time.monotonic()
        try:
            client.sendall(data)
            # after success
            sent += len(data)
        finally:
            # even raising
            waited = time.monotonic() - t
            if waited > client_wait:
                client_wait = waited

    def _flush() -> None:
        # commit both
        nonlocal _committed
        if not _committed:
            _committed = True
            _to_client(bytes(_open))
            _open.clear()
            # widen ceiling
            up.settimeout(_STREAM_IDLE_TIMEOUT)

    def _send(data: bytes, body: bool = True) -> None:
        nonlocal total, _tail, first_event_at
        total += len(data)
        if _CAPTURE and len(_cap) < _CAPTURE_MAX:
            _cap.extend(data)
        if held is not None:
            held.extend(data)
            if want_note and body:
                # first bytes
                room = _NOTE_BYTES - len(note_buf)
                if room > 0:
                    note_buf.extend(data[:room])
            return
        # first event
        if body and first_event_at is None and is_sse:
            window = _tail + data
            if _FIRST_EVENT in window:
                first_event_at = time.time()
            _tail = window[-64:]
        if _committed:
            _to_client(data)
            return
        # pre-content hold
        _open.extend(data)
        # unbounded framing
        if first_event_at is not None or len(_open) > _PRE_COMMIT_MAX:
            _flush()

    try:
        # rebuild head
        out_head = f"{method} {path} HTTP/1.1\r\n".encode("latin1")
        # we answer
        skip = {"connection", "keep-alive", "proxy-authenticate",
                "proxy-authorization", "te", "trailer", "transfer-encoding",
                "upgrade", "content-length", "expect"}
        for k, v in headers.items():
            if k in skip:
                continue
            vv = v.decode("latin1") if isinstance(v, bytes) else v
            out_head += f"{k}: {vv}\r\n".encode("latin1")
        out_head += f"content-length: {len(body)}\r\n".encode()
        out_head += (b"connection: keep-alive\r\n\r\n" if pool is not None
                     else b"connection: close\r\n\r\n")
        # send window
        up.settimeout(_SEND_TIMEOUT)
        _push = time.monotonic()
        try:
            _timed(up.sendall, out_head + body)
        finally:
            # even raising
            send_wait = time.monotonic() - _push

        if uf is None:
            uf = up.makefile("rb")
        # head window
        up.settimeout(_READ_TIMEOUT)
        # head counts
        rhead = _timed(_read_head, uf, _stamp_byte, sock=up,
                       ceiling=_READ_TIMEOUT)
        if rhead is None:
            err = "upstream closed"
            emit({"type": "callend", "t": time.time(), "n": seq, "c": call_n,
                  "lane": lane.index, "cc": lane.exit_country, "status": 0,
                  "kb": 0, "secs": round(time.time() - t0, 1), "err": err,
                  "cut": True,
                  "note": "",
                  "max_wait_s": round(max_wait, 1),
                  "client_wait_s": round(client_wait, 1),
                  "reused": kept is not None,
                  **_keys()})
            return err, 0, b"", True
        try:
            status = int(rhead.split(b" ", 2)[1])
        except (IndexError, ValueError):
            pass
        rheaders = {}
        for raw in rhead.split(b"\r\n")[1:]:
            if raw and b":" in raw:
                k, v = raw.split(b":", 1)
                rheaders[k.strip().lower()] = v.strip()

        if status == 429:
            # exit limited
            retry_after = _retry_after(rheaders)
            relay.tor.note_limited(lane, retry_after)

        if status not in _RETRYABLE:
            held = None  # stream out
        is_sse = b"text/event-stream" in rheaders.get(b"content-type", b"")
        # error body
        want_note = status >= 400
        # head joins
        head_out = _force_connection_close(rhead)
        _open.extend(head_out)
        if held is not None:
            # keep head
            held.extend(head_out)
        if not is_sse and held is None:
            # plain body
            _flush()

        # honour close
        upstream_close = b"close" in rheaders.get(b"connection", b"").lower()
        peer_close = upstream_close

        if b"chunked" in rheaders.get(b"transfer-encoding", b""):
            # verbatim
            while True:
                size_line = _timed(uf.readline)
                if not size_line:
                    break
                try:
                    size = int(size_line.strip().split(b";")[0], 16)
                except ValueError:
                    _send(size_line, body=False)
                    break
                _send(size_line, body=False)
                if size == 0:
                    # trailer
                    while True:
                        tl = _timed(uf.readline)
                        if not tl:
                            break
                        _send(tl, body=False)
                        if tl in (b"\r\n", b"\n"):
                            break
                    clean_end = True
                    keep = not upstream_close
                    break
                chunk = _timed(_read_exact, uf, size + 2)
                _send(chunk)
                if len(chunk) < size + 2:
                    break  # died mid-chunk
        elif b"content-length" in rheaders:
            remaining = int(rheaders[b"content-length"])
            while remaining > 0:
                chunk = _timed(uf.read, min(65536, remaining))
                if not chunk:
                    break
                _send(chunk)
                remaining -= len(chunk)
            clean_end = remaining <= 0
            keep = clean_end and not upstream_close
        else:
            while True:
                chunk = _timed(uf.read, 65536)
                if not chunk:
                    break
                _send(chunk)
            clean_end = True  # eof ends

        # empty body
        if not _committed and held is None:
            emit({"type": "callend", "t": time.time(), "n": seq, "c": call_n,
                  "lane": lane.index, "cc": lane.exit_country, "status": 0,
                  "kb": 0, "secs": round(time.time() - t0, 1),
                  "err": "no body", "cut": True,
                  "note": "",
                  "max_wait_s": round(max_wait, 1),
                  "client_wait_s": round(client_wait, 1),
                  "reused": kept is not None,
                  **_keys()})
            return "no body", 0, b"", True

        if keep and held is None and pool is not None:
            # reuse tunnel
            pool.give(lane, up, uf)
            up = None

        if not clean_end:
            # dump capture
            _dump_cut(seq, call_n, lane, "eof-mid-body", bytes(_cap), total)

        emit({"type": "callend", "t": time.time(), "n": seq, "c": call_n,
              "lane": lane.index, "cc": lane.exit_country, "status": status,
              "kb": round(total / 1024, 1),
              "secs": round(time.time() - t0, 1), "err": "",
              "cut": not clean_end,
              "reused": kept is not None,
              "retry_after": round(retry_after),
              "note": _note_of(bytes(note_buf)),
              "max_wait_s": round(max_wait, 1),
              "client_wait_s": round(client_wait, 1),
              **_keys()})
        return "", status, bytes(held or b""), False
    # bad length
    except (ssl.SSLError, OSError, ValueError) as exc:
        err = f"{type(exc).__name__}"
        # unseen retries
        retryable = held is not None or not _committed
        # held only
        seen = total if _committed else len(_open)
        # upstream only
        if (charge_timeout and err.endswith("TimeoutError")
                and last_op == "upstream"):
            # charged here
            _note_timeout(relay, lane, emit, seq, call_n)
        if _committed and _cap:
            # dump capture
            _dump_cut(seq, call_n, lane, err, bytes(_cap), seen)
        emit({"type": "callend", "t": time.time(), "n": seq, "c": call_n,
              "lane": lane.index, "cc": lane.exit_country,
              # delivered status
              "status": status if _committed else 0,
              "kb": round(seen / 1024, 1),
              "secs": round(time.time() - t0, 1), "err": err,
              "cut": True,
              "max_wait_s": round(max_wait, 1),
              "client_wait_s": round(client_wait, 1),
              "stalled": last_op,
              "note": _note_of(bytes(note_buf)),
              "reused": kept is not None,
              **_keys()})
        return err, 0, b"", retryable
    finally:
        if up is not None:
            _close_quiet(up)
