"""Per-request MITM for opencode.ai: a local CA terminates TLS per call.

One call goes down a wave of ``_RACE_WIDTH`` exits, not down the whole fleet.
The free tier flags a client that turns up from half a dozen addresses for the
same session: the 2026-10-18 live session answered 200 on every wave of five
and was refused ``403 FreeTierError`` on every wave of ten, from that session's
very first call -- and the refusal then stuck to the client, not to the exits.
Ten lanes are still worth having, because they spread the day's allowance and
give the sweep somewhere to move a lane to.  Ten copies of one call are not.
"""

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

from . import bounds, exits, netutil
from .bench import FREE_LIMIT, GATE_LIMIT, RATE_LIMIT
from .lanes import Lane

MITM_HOSTS = ("opencode.ai",)

_FIRST_BYTE_TIMEOUT = bounds.number("LINGLING_FIRST_BYTE_S", 30,
                                    bounds.TICK_S, bounds.WINDOW_S)
_SEND_TIMEOUT = bounds.number("LINGLING_SEND_S", 120,
                              bounds.TICK_S, bounds.WINDOW_S)
_READ_TIMEOUT = bounds.number("LINGLING_STREAM_TIMEOUT", 1800,
                              bounds.TICK_S, bounds.WINDOW_S)
_STREAM_IDLE_TIMEOUT = bounds.number("LINGLING_STREAM_IDLE_S", 1800,
                                     bounds.TICK_S, bounds.WINDOW_S)
_FIRST_CONTENT_TIMEOUT = bounds.number("LINGLING_FIRST_CONTENT_S", 30,
                                       bounds.TICK_S, bounds.WINDOW_S)
_STALL_CUT_S = bounds.number("LINGLING_STALL_CUT_S", 120,
                             bounds.TICK_S, bounds.WINDOW_S)
_PULSE_S = bounds.number("LINGLING_PULSE_S", 60,
                         bounds.TICK_S, bounds.WINDOW_S)
#: five exits
_RACE_WIDTH = bounds.count("LINGLING_RACE_WIDTH", 5, 0, 64)
_SLOW_EXIT_S = bounds.number("LINGLING_SLOW_EXIT_S", 20,
                             bounds.TICK_S, bounds.WINDOW_S)
_FIRST_EVENT = b"data:"
_KEEPALIVE_S = bounds.number("LINGLING_KEEPALIVE_S", 600,
                             bounds.TICK_S, bounds.WINDOW_S)

_RETRYABLE = frozenset({429, 502, 503, 504})

#: gate refusal
_GATE_REFUSAL = 403

#: announced gates
_GATE_TOLD: Dict[str, float] = {}
_GATE_TOLD_LOCK = threading.Lock()


def _gate_is_news(exit_fp: str, until: float) -> bool:
    """True the first time this exit's closure is announced in this process.

    The gate refuses in a blink, so a busy session keeps many copies of one
    exit in flight at once, and each of them lands its own 403.  Without this
    the pane carried one identical line per copy -- dozens in a second -- for
    one closed exit.  The first copy speaks for the exit; the rest are the
    same fact, already said.
    """
    now = time.time()
    with _GATE_TOLD_LOCK:
        if _GATE_TOLD.get(exit_fp, 0.0) > now:
            return False
        if len(_GATE_TOLD) > 256:
            for fp, deadline in list(_GATE_TOLD.items()):
                if deadline <= now:
                    del _GATE_TOLD[fp]
        _GATE_TOLD[exit_fp] = until
        return True

_5XX_ATTEMPTS = 2
_LIMIT_WAVES = bounds.count("LINGLING_LIMIT_WAVES", 2, 0, 16)
_LIMIT_WAIT_S = bounds.number("LINGLING_LIMIT_WAIT_S", 2.0,
                             0.0, bounds.WINDOW_S)
_NOTE_BYTES = 512

_CAPTURE = os.environ.get("LINGLING_CAPTURE", "")
#: where to write the upstream request/response, for when the far end
#: refuses and the reason is not in the status code
_DEBUG_UPSTREAM = os.environ.get("LINGLING_DEBUG_UPSTREAM", "")
_CAPTURE_MAX = bounds.count("LINGLING_CAPTURE_BYTES", 8192, 0, 1_048_576)

_PRE_COMMIT_MAX = 65536

_HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate",
                         "proxy-authorization", "te", "trailer",
                         "transfer-encoding", "upgrade", "content-length",
                         "expect"})

_ca_lock = threading.Lock()
_ca_ctx: Dict[Path, "tuple"] = {}


def crypto_available() -> bool:
    """True when the crypto stack the MITM needs actually imports."""
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
    netutil.chmod_quiet(mitm_dir, 0o700)
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
    netutil.chmod_quiet(key_path, 0o600)
    return cert, key, cert_path


class CertShop:
    """Mints per-host certs signed by the local CA, cached on disk."""

    def __init__(self, mitm_dir: Path) -> None:
        with _ca_lock:
            self._ca_cert, self._ca_key, self.ca_pem_path = _ensure_ca(mitm_dir)
        self._dir = mitm_dir / "certs"
        self._dir.mkdir(parents=True, exist_ok=True)
        netutil.chmod_quiet(self._dir, 0o700)

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
            netutil.chmod_quiet(key_path, 0o600)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert_path), str(key_path))
        return ctx


def _read_head(f, on_first=None, sock=None, ceiling=None) -> Optional[bytes]:
    """Read one HTTP head (request or status line + headers) through CRLFCRLF."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = f.read(1)
        if not chunk:
            return None
        if on_first is not None:
            on_first()
            on_first = None
        if sock is not None and ceiling is not None:
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
    """The client's request body, or None if it did not arrive whole."""
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
    """The model, reasoning effort, and output cap of one request body."""
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
    """Always advertise ``Connection: close`` on the upstream request."""
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
    out.extend(lines[i:])
    return b"\r\n".join(out)


def _dump_cut(seq: int, call_n: int, lane, err: str, cap: bytes,
              total: int) -> None:
    """Write a cut stream's bytes, so the dying point can be read."""
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
    """The sticky favorite, else the least-loaded lane we have not tried."""
    sticky = getattr(relay.tor, "sticky", None)
    if sticky is not None:
        lane = sticky.next_lane(tried)
        if lane is not None:
            return lane
    return relay.pick_lane(exclude=tried)


def _race_lanes(relay, tried: set) -> List[Lane]:
    """Every live lane that can serve this call, the favorite first."""
    now = time.time()
    lanes: List[Lane] = []
    taken = set(tried)
    spare: Optional[Lane] = None
    same_exit: Optional[Lane] = None
    riders: Dict[str, int] = {}

    def live(lane) -> bool:
        return lane.limited_until <= now

    exit_key = exits.exit_key

    def shares_an_exit(lane) -> bool:
        """True when a lane already in this wave sits on that exit."""
        key = exit_key(lane)
        return bool(key) and key in riders

    first = _grab_lane(relay, taken)
    if first is not None and not live(first):
        spare = first
        first = None
    if first is not None:
        lanes.append(first)
        taken.add(first.index)
        key = exit_key(first)
        if key:
            riders[key] = first.index
    while _RACE_WIDTH <= 0 or len(lanes) < _RACE_WIDTH:
        lane = relay.pick_lane(exclude=taken)
        if lane is None or lane.index in taken:
            break
        taken.add(lane.index)
        if not live(lane):
            continue
        if _RACE_WIDTH > 0 and shares_an_exit(lane):
            if same_exit is None:
                same_exit = lane
            continue
        lanes.append(lane)
        key = exit_key(lane)
        if key:
            riders[key] = lane.index
    if same_exit is not None and (_RACE_WIDTH <= 0
                                  or len(lanes) < _RACE_WIDTH):
        lanes.append(same_exit)
    if not lanes and spare is not None:
        lanes.append(spare)
    return lanes


class _Copy:
    """One lane's go at one call: cuttable from the thread that won.

    It also remembers the exit it dialled.  A lane can be moved onto another
    relay while a copy is still in flight, so a verdict has to name the exit it
    came from: that is the relay the far end counted, not the one the lane
    happens to ride by the time the answer is read.
    """

    def __init__(self, lane: Lane) -> None:
        self.lane = lane
        self.exit_fp = lane.exit_fingerprint
        self.exit_ip = lane.exit_ip
        self.race: Optional[_Race] = None
        self.thread: Optional[threading.Thread] = None
        self.sock = None
        self.beaten = False
        self._lock = threading.Lock()

    def arm(self, sock) -> bool:
        """Hand over the socket to cut on a loss; False once beaten."""
        with self._lock:
            if self.beaten:
                return False
            self.sock = sock
            return True

    def beat(self) -> None:
        """Another copy has the client: cut this upstream, and stop it."""
        with self._lock:
            self.beaten = True
            sock, self.sock = self.sock, None
        if sock is not None:
            _abort_quiet(sock)

    def stands(self) -> bool:
        """True while this copy may still speak for itself, or charge."""
        return self.race is not None and self.race.speak(self)

    def owns(self) -> bool:
        """True once this copy is the one that took the client."""
        return self.race is not None and self.race.winner is self

    def claim(self) -> bool:
        """Take the client for this copy, or lose to one already there."""
        return self.race is None or self.race.claim(self)


class _Beaten(Exception):
    """A copy that lost, raised where it would have written to the client."""


class _Race:
    """One call over every lane: the first answer to commit takes the client"""

    def __init__(self, copies: List[_Copy]) -> None:
        self.copies = copies
        self.winner: Optional[_Copy] = None
        self.model = ""
        self._lock = threading.Lock()
        for copy in copies:
            copy.race = self

    def speak(self, copy: _Copy) -> bool:
        """A copy may speak until one wins, and after that only the winner."""
        with self._lock:
            return self.winner is None or self.winner is copy

    def claim(self, copy: _Copy) -> bool:
        """Take the client for this copy, or lose to one already there."""
        with self._lock:
            if self.winner is None:
                self.winner = copy
                others = [c for c in self.copies if c is not copy]
            elif self.winner is copy:
                return True
            else:
                return False
        for other in others:
            try:
                other.beat()
            except Exception:  # noqa: BLE001
                pass
        return True

    def raced(self) -> int:
        """How many lanes this call went out on."""
        return len(self.copies)


class _Round:
    """What one race came back with: the client's answer, or the reason."""

    def __init__(self) -> None:
        self.committed = False
        self.held = b""
        self.retryable = False
        self.limits: List[tuple] = []
        self.gates: List[tuple] = []
        self.refused = False
        self.gated = False
        self.fivexx = False


def _race_call(client, lanes: List[Lane], host: str, port: int, method: str,
               path: str, headers: dict, body: bytes, emit, seq: int,
               call_n: int, t0: float, relay, model: str = "") -> _Round:
    """Fire one call at every lane at once; the first answer takes the client"""
    copies = [_Copy(lane) for lane in lanes]
    race = _Race(copies)
    race.model = model
    plain = len(lanes) == 1
    emit({
        "type": "call", "t": t0, "n": seq, "c": call_n,
        "lane": lanes[0].index if plain else 0,
        "cc": lanes[0].exit_country if plain else "",
        "ip": lanes[0].exit_ip if plain else "",
        "lanes": [l.index for l in lanes],
        "ccs": [l.exit_country for l in lanes],
        "method": method, "path": path, "model": model, "host": host,
        "bytes": len(body),
    })
    out: List[tuple] = []
    lock = threading.Lock()

    def run(copy: _Copy) -> None:
        def say(ev: Dict) -> None:
            """A copy that lost has nothing to say in the pane."""
            if race.speak(copy):
                emit(ev)
        got = _roundtrip(client, copy.lane, host, port, method, path,
                         headers, body, say, seq, call_n, t0, relay,
                         charge_timeout=copy is copies[0], model=model,
                         copy=copy)
        with lock:
            out.append((copy, got))

    for copy in copies:
        with copy.lane.lock:
            copy.lane.active += 1
    threads = [threading.Thread(target=run, args=(c,), daemon=True,
                                name=f"race-{seq}-{c.lane.index}")
               for c in copies]
    for copy, thread in zip(copies, threads):
        copy.thread = thread
        thread.start()
    try:
        while race.winner is None and any(t.is_alive() for t in threads):
            time.sleep(0.05)
        winner = race.winner
        if winner is None:
            for thread in threads:
                thread.join()
        else:
            winner.thread.join()
            for thread in threads:
                if thread is not winner.thread:
                    thread.join(timeout=1.0)
    finally:
        for copy in copies:
            with copy.lane.lock:
                copy.lane.active -= 1
    return _tally(race, out, relay)


def _note_ok(relay, lane) -> None:
    """A real 200 through an exit: it earns a free verification stamp."""
    note = getattr(relay.tor, "note_verified", None)
    if note is not None:
        note(lane.exit_fingerprint)


def _note_model_ok(relay, copy, lane) -> None:
    """A model answer through an exit: it earns the model mark."""
    note = getattr(relay.tor, "note_model_ok", None)
    if note is not None:
        note(getattr(copy, "exit_fp", "") or lane.exit_fingerprint)


def _note_spend(relay, lane) -> None:
    """Count a landed request against the exit that carried it.

    Counted against the exit's address, which is what the far end meters.
    Only the winner is counted: a beaten copy is cut before the far end has
    seen the whole request, so charging the day for it would overstate what we
    have spent.  This is the ledger the picker walks past a hard-used exit on.
    """
    note = getattr(relay.tor, "note_spend", None)
    if note is not None:
        note(exits.exit_key(lane))


def _tally(race: _Race, out: List[tuple], relay) -> _Round:
    """Judge a race: the client's answer, or what the copies earned.

    Only a 200 takes the call.  Every copy starts at the same instant, so the
    first 200 to arrive is also the quickest lane's 200 -- and everything that
    is not a 200 is held back, however fast it comes.  That is the whole rule,
    and refusing is cheaper than answering, so without it a spent exit, a
    stalled edge or a gate refusal beats every healthy lane to the client by
    seconds and the call dies on the cheapest answer rather than the best one.

    A held answer is still a real answer: the first refusal is kept and handed
    over when no lane can do better, so a pool with nothing left to give says
    what the far end actually said instead of inventing a 502.  A 403 is the
    free tier's gate and moves no lane -- the gate refused this exit, not this
    tor -- but the exit is sat out, because racing it on every call pays for
    the same wall again.
    """
    got = _Round()
    for copy, (err, status, body, retryable) in out:
        if race.winner is copy:
            relay.tor.note_result(copy.lane.exit_country, status)
            if status:
                _note_spend(relay, copy.lane)
            if status == 200:
                _note_ok(relay, copy.lane)
                if getattr(race, "model", ""):
                    _note_model_ok(relay, copy, copy.lane)
            continue
        if err:
            if copy.beaten:
                continue
            got.retryable = got.retryable or retryable
            continue
        if status == _GATE_REFUSAL:
            got.gated = True
            got.retryable = True
            got.gates.append((copy.lane, copy.exit_fp))
            if not got.held:
                got.held = body
            continue
        if status == 402:
            # "Upstream request failed: Insufficient account funds" is the
            # free-model pool for that address running dry. It is the day's
            # wall, not a gate, so the exit sits out until 00:00 UTC instead
            # of being asked again on every call.
            got.limits.append((copy.lane, copy.exit_fp, 0.0, FREE_LIMIT))
            got.retryable = True
            if not got.held:
                got.held = body
            continue
        if status == 429:
            section, wait = _limit_of(body)
            if section:
                got.limits.append((copy.lane, copy.exit_fp, wait, section))
                got.retryable = True
                if not got.held:
                    got.held = body
                continue
            got.refused = True
            got.held = body
            continue
        if status in _RETRYABLE:
            got.fivexx = True
            got.retryable = True
            if not got.held:
                got.held = body
            continue
        if status:
            got.retryable = True
            if not got.held:
                got.held = body
            continue
    got.committed = race.winner is not None
    return got


def _sticky_ceiling(relay, lane) -> float:
    """While stuck on a lane, its release ceiling replaces the slow one."""
    sticky = getattr(relay.tor, "sticky", None)
    if sticky is not None and sticky.stuck_on(lane.index):
        return sticky.release_s
    return _SLOW_EXIT_S


def _sticky_verdict(relay, lane) -> None:
    """A verdict on the stuck lane wakes the others."""
    sticky = getattr(relay.tor, "sticky", None)
    if sticky is not None:
        sticky.on_verdict(lane.index)


def _sticky_sample(relay, lane, t0: float, first_byte_at, first_event_at,
                   model: str = "") -> None:
    """Donate this attempt's latency to the round -- model calls only"""
    sticky = getattr(relay.tor, "sticky", None)
    if sticky is None or not model:
        return
    stamp = first_event_at or first_byte_at
    if stamp is not None:
        sticky.record(lane.index, round(stamp - t0, 2))


def _note_timeout(relay, lane, emit, seq: int, call_n: int) -> None:
    """A timeout means a dead exit: move the lane at once, as a 429 does."""
    moved = relay.tor.note_timeout(lane)
    _sticky_verdict(relay, lane)
    if not moved:
        return
    emit({"type": "lane", "kind": "timeout", "t": time.time(),
          "lane": lane.index, "cc": lane.exit_country, "ip": lane.exit_ip,
          "msg": moved})


def _note_ssl(relay, lane, emit, seq: int, call_n: int) -> None:
    """An SSL cut means a dead exit: the timeout verdict, at once."""
    moved = relay.tor.note_ssl_error(lane)
    _sticky_verdict(relay, lane)
    if not moved:
        return
    emit({"type": "lane", "kind": "ssl", "t": time.time(),
          "lane": lane.index, "cc": lane.exit_country, "ip": lane.exit_ip,
          "msg": moved})


def _note_slow(relay, lane, emit, seq: int, call_n: int,
               elapsed: float) -> None:
    """A slow first event is a bad exit: the timeout verdict, at once."""
    moved = relay.tor.note_slow_exit(lane, elapsed)
    _sticky_verdict(relay, lane)
    if not moved:
        return
    emit({"type": "lane", "kind": "slow", "t": time.time(),
          "lane": lane.index, "cc": lane.exit_country, "ip": lane.exit_ip,
          "msg": moved})


def handle_conn(raw: socket.socket, host: str, port: int, seq: int,
                shop: CertShop, emit: Callable[[Dict], None],
                relay) -> None:
    """Own one intercepted TLS connection end to end (blocking thread)."""
    try:
        raw.setblocking(True)
        raw.settimeout(300)
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
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def _serve(client: ssl.SSLSocket, host: str, port: int, seq: int,
           emit: Callable[[Dict], None], relay) -> None:
    """Serve model calls on one client TLS connection; a lane per call.

    A wave where every lane answered 403 ends the call.  The refusal names the
    client -- the free tier will not credit these requests through any address
    -- so walking the rest of the pool on the same call is the same wall paid
    for twice, and it is what turns one tripped call into a pool with no exits
    left.  The far end's own 403 is the client's answer.
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
        burnt = False
        limit_waves = 0
        while True:
            lanes = _race_lanes(relay, tried)
            if not lanes:
                if burnt and limit_waves < _LIMIT_WAVES \
                        and relay.any_unlimited(set()):
                    limit_waves += 1
                    time.sleep(_LIMIT_WAIT_S)
                    tried.clear()
                    continue
                if held:
                    client.sendall(held)
                else:
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n"
                                   b"Content-Length: 0\r\n\r\n")
                return
            tried.update(l.index for l in lanes)
            got = _race_call(client, lanes, host, port, method, path, headers,
                             body, emit, seq, call_n, time.time(), relay,
                             model)
            for lane, exit_fp, wait, section in got.limits:
                relay.tor.note_limited(lane, wait, section, fingerprint=exit_fp)
                relay.report_refused(lane, 429, exit_fp)
                _sticky_verdict(relay, lane)
            for lane, exit_fp in got.gates:
                until = relay.tor.note_limited(lane, 0.0, GATE_LIMIT,
                                               fingerprint=exit_fp)
                if not until or not _gate_is_news(exit_fp, until):
                    continue
                _sticky_verdict(relay, lane)
                emit({"type": "lane", "kind": "limited", "t": time.time(),
                      "lane": lane.index, "cc": lane.exit_country,
                      "ip": lane.exit_ip,
                      "msg": f"lane {lane.index} "
                             f"{{{lane.exit_country}}} 403 -- the gate closed "
                             f"this exit, sitting it out"})
            if got.held:
                held = got.held
            if got.committed:
                return
            if got.gated and len(got.gates) >= len(lanes):
                #: wave gated
                if held:
                    client.sendall(held)
                return
            if got.refused:
                client.sendall(held)
                return
            if got.limits:
                burnt = True
            if got.limits and not relay.any_unlimited(tried):
                if limit_waves < _LIMIT_WAVES and relay.any_unlimited(set()):
                    limit_waves += 1
                    time.sleep(_LIMIT_WAIT_S)
                    tried.clear()
                    continue
                if held:
                    client.sendall(held)
                return
            if got.fivexx:
                bad5xx += 1
                if bad5xx >= _5XX_ATTEMPTS:
                    if held:
                        client.sendall(held)
                    return
            if not got.retryable:
                return
            continue


def _close_quiet(sock) -> None:
    """Close without caring whether it was already gone."""
    try:
        sock.close()
    except OSError:
        pass


def _abort_quiet(sock) -> None:
    """Cut a connection loose, so a read blocked on it lets go at once."""
    netutil.abort_quiet(sock)


def _peer_closed(sock) -> bool:
    """True when the far end has hung up on a socket we left idle."""
    try:
        return bool(select.select([sock], [], [], 0)[0])
    except (OSError, ValueError):
        return True


class TunnelPool:
    """Idle upstream connections, kept per lane."""

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
            _close_quiet(up)
            return None
        if ip != lane.exit_ip:
            _close_quiet(up)
            return None
        if _peer_closed(up):
            _close_quiet(up)
            return None
        return up, fil

    def give(self, lane: Lane, up, fil) -> None:
        """Hand a tunnel back after a response that ended cleanly."""
        now = time.monotonic()
        with self._lock:
            slots = self._idle.setdefault(lane.index, [])
            keep = []
            for when, ip, old_up, _old_fil in slots:
                if (now - when) > self.ttl or ip != lane.exit_ip:
                    _close_quiet(old_up)
                    continue
                keep.append((when, ip, old_up, _old_fil))
            keep.append((now, lane.exit_ip, up, fil))
            self._idle[lane.index] = keep


def _note_of(raw: bytes) -> str:
    """The error body as one readable line, for the pane.

    Some exits compress their refusal, and a compressed body decodes to a line
    of mojibake -- two hundred characters of it, on every gated call.  A body
    that is not text says nothing here: the status and the exit already say
    what happened, and a line the operator cannot read is worse than no line.
    """
    text = raw.decode("utf-8", "replace")
    if "\ufffd" in text:
        return ""
    return " ".join(text.split())[:200]


def _retry_after(rheaders: dict) -> float:
    """Seconds the far end says to wait, or 0 when it did not say."""
    try:
        return max(0.0, float(rheaders.get(b"retry-after", b"0")))
    except (TypeError, ValueError):
        return 0.0


def _limit_of(held: bytes) -> "tuple[str, float]":
    """Which section a held 429 belongs in: the free wall, rate, or neither."""
    if netutil.UPSTREAM_UNAVAILABLE in held:
        return "", 0.0
    rheaders = {}
    for raw in held.split(b"\r\n\r\n", 1)[0].split(b"\r\n")[1:]:
        if b":" in raw:
            k, v = raw.split(b":", 1)
            rheaders[k.strip().lower()] = v.strip()
    section = FREE_LIMIT if netutil.QUOTA_SPENT in held else RATE_LIMIT
    return section, _retry_after(rheaders)


def section_of(note: bytes, status: int) -> str:
    """The notepad section a finished call belongs in, for the pane."""
    if status != 429:
        return ""
    return _limit_of(bytes(note))[0]


def _lat(t0: float, first_byte: Optional[float],
         first_event: Optional[float]) -> Dict:
    """Offset from request start to first upstream byte / first SSE event."""
    return {
        "first_byte_s": round(first_byte - t0, 2) if first_byte else 0,
        "first_event_s": round(first_event - t0, 2) if first_event else 0,
    }


_LOST = ("", 0, b"", False)


def _debug_upstream(head: bytes, status: int, rheaders: dict,
                     body: bytes) -> None:
    """Write the upstream exchange to a file, with the token redacted.

    The far end's own words are the only way to tell a gate from a quota
    from a spent pool, and none of them are in the status code: 403 is
    "the free tier can only be used from within OpenCode", 402 is
    "insufficient account funds", 429 is the daily wall. The file is
    appended to, so a session leaves the whole exchange behind.
    """
    if not _DEBUG_UPSTREAM:
        return
    try:
        text = head.decode("latin1", "replace")
        lines = []
        for line in text.split("\r\n"):
            if line.lower().startswith("authorization:"):
                line = "authorization: <redacted>"
            lines.append(line)
        out = ["=== request ===", *lines, "=== response ===",
               f"status: {status}"]
        for k, v in rheaders.items():
            out.append(f"{k}: {v.decode('latin1', 'replace')}")
        out.append("=== body ===")
        raw = body[:2000]
        chunked = any(k == b"transfer-encoding" and b"chunked" in v.lower()
                      for k, v in rheaders.items())
        if chunked and raw:
            # the captured bytes carry the chunked framing, and a codec
            # cannot read that: take the payload back out
            plain = b""
            while raw:
                line, _, rest = raw.partition(b"\r\n")
                try:
                    size = int(line.split(b";")[0].strip() or b"0", 16)
                except ValueError:
                    break
                if size == 0:
                    break
                plain += rest[:size]
                raw = rest[size + 2:]
            raw = plain
        enc = ""
        for k, v in rheaders.items():
            if k == b"content-encoding":
                enc = v.decode("latin1", "replace").lower()
        if raw and enc in ("gzip", "deflate", "br", "zstd"):
            try:
                import gzip as _gz
                import zlib as _zl
                if enc == "gzip":
                    raw = _gz.decompress(raw)
                elif enc == "deflate":
                    raw = _zl.decompress(raw, -_zl.MAX_WBITS)
                elif enc == "br":
                    import brotli as _br
                    raw = _br.decompress(raw)
                else:
                    raw = b"<" + enc.encode() + b" body, not decoded>"
            except Exception:  # noqa: BLE001
                raw = b"<compressed body, not decoded>"
        out.append(raw.decode("utf-8", "replace"))
        with open(_DEBUG_UPSTREAM, "a", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
    except OSError:
        pass


def _build_head(method: str, path: str, headers: dict, body: bytes,
                keepalive: bool) -> bytes:
    """The upstream request head, without the hop-by-hop headers."""
    out = f"{method} {path} HTTP/1.1\r\n".encode("latin1")
    for k, v in headers.items():
        if k in _HOP_BY_HOP:
            continue
        text = v.decode("latin1") if isinstance(v, bytes) else v
        out += f"{k}: {text}\r\n".encode("latin1")
    out += f"content-length: {len(body)}\r\n".encode()
    return out + (b"connection: keep-alive\r\n\r\n" if keepalive
                  else b"connection: close\r\n\r\n")


def _parse_head(head: bytes) -> "tuple[int, Dict[bytes, bytes]]":
    """The status code and the headers, out of one upstream head."""
    try:
        status = int(head.split(b" ", 2)[1])
    except (IndexError, ValueError):
        status = 0
    headers: Dict[bytes, bytes] = {}
    for raw in head.split(b"\r\n")[1:]:
        if raw and b":" in raw:
            k, v = raw.split(b":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, headers


def _pump_chunked(uf, read, send, upstream_close: bool) -> "tuple[bool, bool]":
    """Copy a chunked body verbatim: ``(ended cleanly, tunnel reusable)``."""
    while True:
        size_line = read(uf.readline)
        if not size_line:
            return False, False
        try:
            size = int(size_line.strip().split(b";")[0], 16)
        except ValueError:
            send(size_line, body=False)
            return False, False
        send(size_line, body=False)
        if size == 0:
            while True:
                tail = read(uf.readline)
                if not tail:
                    break
                send(tail, body=False)
                if tail in (b"\r\n", b"\n"):
                    break
            return True, not upstream_close
        chunk = read(_read_exact, uf, size + 2)
        send(chunk)
        if len(chunk) < size + 2:
            return False, False


def _pump_length(uf, read, send, remaining: int,
                 upstream_close: bool) -> "tuple[bool, bool]":
    """Copy a body of known length: ``(ended cleanly, tunnel reusable)``."""
    while remaining > 0:
        chunk = read(uf.read, min(65536, remaining))
        if not chunk:
            break
        send(chunk)
        remaining -= len(chunk)
    ended = remaining <= 0
    return ended, ended and not upstream_close


def _pump_eof(uf, read, send) -> "tuple[bool, bool]":
    """Copy until the far end closes.

    An unframed body has no terminator, so its tunnel is never reusable.
    """
    while True:
        chunk = read(uf.read, 65536)
        if not chunk:
            break
        send(chunk)
    return True, False


def _emit_cut(emit, seq: int, call_n: int, lane: Lane, t0: float, err: str,
              max_wait: float, client_wait: float, reused: bool,
              keys: Dict) -> None:
    """One callend for an attempt that never delivered a body.

    The tail is read by name, so a caller that forgot a key gets a
    ``KeyError`` here instead of a callend a reader cannot group.
    """
    emit({"type": "callend", "t": time.time(), "n": seq, "c": call_n,
          "lane": lane.index, "cc": lane.exit_country, "status": 0, "kb": 0,
          "secs": round(time.time() - t0, 1), "err": err, "cut": True,
          "note": "",
          "max_wait_s": round(max_wait, 1),
          "client_wait_s": round(client_wait, 1),
          "reused": reused,
          "first_byte_s": keys["first_byte_s"],
          "first_event_s": keys["first_event_s"],
          "exit": keys["exit"],
          "ip": keys["ip"],
          "send_s": keys["send_s"],
          "peer_close": keys["peer_close"],
          "client_kb": keys["client_kb"],
          "raced": keys["raced"]})


def _dial(lane: Lane, host: str, port: int, pool, copy, charge_timeout: bool,
          may) -> "tuple":
    """A tunnel to the far end: a pooled one, else a cold SOCKS5 dial.

    Returns ``(up, uf, kept, err, verdict)``. An ``err`` of ``"-"`` means this
    copy was beaten to the lane and owes nothing; otherwise the caller reports
    the error, after charging the lane the ``verdict`` -- ``"timeout"``,
    ``"ssl"``, or nothing.
    """
    kept = pool.take(lane) if pool is not None else None
    if kept is not None:
        up, uf = kept
        if copy is not None and not copy.arm(up):
            pool.give(lane, up, uf)
            return None, None, kept, "-", ""
        return up, uf, kept, "", ""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(_FIRST_BYTE_TIMEOUT)
    try:
        sock.connect(("127.0.0.1", lane.socks_port))
        err = netutil.socks5_open(
            sock, host, port,
            cred=netutil.lane_cred(lane.index))
        if err:
            _close_quiet(sock)
            if copy is not None and copy.beaten:
                return None, None, kept, "-", ""
            verdict = ("timeout" if err == "timed out" and charge_timeout
                       and may() else "")
            return None, None, kept, err, verdict
        up = ssl.create_default_context().wrap_socket(
            sock, server_hostname=host)
        if copy is not None and not copy.arm(up):
            _close_quiet(up)
            return None, None, kept, "-", ""
        return up, None, kept, "", ""
    except (ssl.SSLError, OSError) as exc:
        sock.close()
        if copy is not None and copy.beaten:
            return None, None, kept, "-", ""
        if type(exc).__name__ == "ConnectionRefusedError":
            lane.healthy = False
        verdict = ("ssl" if charge_timeout and isinstance(exc, ssl.SSLError)
                   and may() else "")
        return None, None, kept, f"{type(exc).__name__}", verdict


def _roundtrip(client: ssl.SSLSocket, lane: Lane, host: str, port: int,
               method: str, path: str, headers: dict, body: bytes,
               emit, seq: int, call_n: int, t0: float, relay: "object",
               charge_timeout: bool = False,
               model: Optional[str] = None, copy: Optional[_Copy] = None
               ) -> "tuple[str, int, bytes, bool]":
    """One upstream attempt; 429s buffer into ``held`` for a lane retry."""
    if copy is not None and copy.beaten:
        return _LOST
    if model is None:
        model = _model_of(body) if body else ""
    first_byte_at = None
    first_event_at = None
    content_by = t0 + _FIRST_CONTENT_TIMEOUT
    max_wait = 0.0
    client_wait = 0.0
    send_wait = 0.0
    sent = 0
    peer_close = False
    last_op = "upstream"
    status = 0
    pool = getattr(relay, "tunnels", None)

    def _may() -> bool:
        """True while this copy may still charge its lane: unbeaten, or won."""
        return copy is None or copy.stands()

    def _witness() -> bool:
        """True when this copy's lane carries this call's dead-exit verdict"""
        return charge_timeout or (copy is not None and copy.owns())

    def _keys() -> Dict:
        """The keys every callend carries, in one place."""
        rode = copy if copy is not None else None
        return {**_lat(t0, first_byte_at, first_event_at),
                "exit": (rode.exit_fp if rode is not None
                         else lane.exit_fingerprint),
                "ip": rode.exit_ip if rode is not None else lane.exit_ip,
                "send_s": round(send_wait, 1),
                "peer_close": peer_close,
                "client_kb": round(sent / 1024, 1),
                "raced": copy.race.raced() if copy is not None
                and copy.race is not None else 0}

    up, uf, kept, err, verdict = _dial(lane, host, port, pool, copy,
                                       charge_timeout, _may)
    if err == "-":
        return _LOST
    if err:
        if verdict == "timeout":
            _note_timeout(relay, lane, emit, seq, call_n)
        elif verdict == "ssl":
            _note_ssl(relay, lane, emit, seq, call_n)
        _emit_cut(emit, seq, call_n, lane, t0, err, max_wait, client_wait,
                  kept is not None, _keys())
        return err, 0, b"", True

    total = 0
    held = bytearray()
    note_buf = bytearray()
    want_note = False
    clean_end = False
    keep = False
    retry_after = 0.0
    is_sse = False
    _tail = b""
    _open = bytearray()
    _cap = bytearray()
    _committed = False
    progress = {"bytes": 0, "at": time.time()}
    heard = {"at": time.time()}
    lane.track_stream(up, progress,
                      hangup=lambda: netutil.abort_quiet(client))
    beat_stop = threading.Event()
    beat: Optional[threading.Thread] = None

    def _stamp_byte() -> None:
        nonlocal first_byte_at
        first_byte_at = time.time()

    def _timed(fn, *args, **kwargs):
        """One blocking upstream read, timed."""
        nonlocal max_wait, last_op
        last_op = "upstream"
        t = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            waited = time.monotonic() - t
            if waited > max_wait:
                max_wait = waited

    def _to_client(data: bytes) -> None:
        """One send to the client, timed apart from the upstream reads."""
        nonlocal client_wait, last_op, sent
        last_op = "client"
        t = time.monotonic()
        try:
            client.sendall(data)
            sent += len(data)
            progress["bytes"] = sent
            progress["at"] = time.time()
        finally:
            waited = time.monotonic() - t
            if waited > client_wait:
                client_wait = waited

    def _beat() -> None:
        """Say this copy is still out there, and how long it has heard nothing."""
        while not beat_stop.wait(_PULSE_S):
            emit({"type": "flow", "t": time.time(), "n": seq, "c": call_n,
                  "lane": lane.index, "cc": lane.exit_country,
                  "ip": lane.exit_ip,
                  "kb": round(progress["bytes"] / 1024, 1),
                  "waited_s": round(time.time() - progress["at"], 1)})

    def _ceiling_left() -> float:
        """The read ceiling for this stream, whichever phase it is in."""
        if not _committed:
            return max(0.05, min(_READ_TIMEOUT, content_by - time.time()))
        return max(0.05,
                   min(_STREAM_IDLE_TIMEOUT,
                       heard["at"] + _STALL_CUT_S - time.time()))

    def _read(fn, *args):
        """One upstream read, under whichever ceiling applies right now."""
        up.settimeout(_ceiling_left())
        return _timed(fn, *args)

    def _flush() -> None:
        nonlocal _committed
        if not _committed:
            if copy is not None and not copy.claim():
                raise _Beaten
            _committed = True
            _to_client(bytes(_open))
            _open.clear()
            up.settimeout(_STREAM_IDLE_TIMEOUT)

    def _send(data: bytes, body: bool = True) -> None:
        nonlocal total, _tail, first_event_at
        total += len(data)
        heard["at"] = time.time()
        if _CAPTURE and len(_cap) < _CAPTURE_MAX:
            _cap.extend(data)
        if held is not None:
            held.extend(data)
            if want_note and body:
                room = _NOTE_BYTES - len(note_buf)
                if room > 0:
                    note_buf.extend(data[:room])
            return
        if body and first_event_at is None and is_sse:
            window = _tail + data
            if _FIRST_EVENT in window:
                first_event_at = time.time()
            _tail = window[-64:]
        if _committed:
            _to_client(data)
            return
        _open.extend(data)
        if first_event_at is not None or len(_open) > _PRE_COMMIT_MAX:
            _flush()

    try:
        out_head = _build_head(method, path, headers, body, pool is not None)
        up.settimeout(_SEND_TIMEOUT)
        beat = threading.Thread(target=_beat, name=f"beat-{seq}-{lane.index}",
                                daemon=True)
        beat.start()
        _push = time.monotonic()
        try:
            _timed(up.sendall, out_head + body)
        finally:
            send_wait = time.monotonic() - _push

        if uf is None:
            uf = up.makefile("rb")
        head_window = _ceiling_left()
        up.settimeout(head_window)
        rhead = _timed(_read_head, uf, _stamp_byte, sock=up,
                       ceiling=head_window)
        if rhead:
            heard["at"] = time.time()
        if rhead is None:
            _emit_cut(emit, seq, call_n, lane, t0, "upstream closed",
                      max_wait, client_wait, kept is not None, _keys())
            return "upstream closed", 0, b"", True
        status, rheaders = _parse_head(rhead)

        if status == 429:
            retry_after = _retry_after(rheaders)

        if status == 200:
            held = None
        is_sse = b"text/event-stream" in rheaders.get(b"content-type", b"")
        want_note = status >= 400
        head_out = _force_connection_close(rhead)
        _open.extend(head_out)
        if held is not None:
            held.extend(head_out)
        if not is_sse and held is None:
            _flush()

        upstream_close = b"close" in rheaders.get(b"connection", b"").lower()
        peer_close = upstream_close

        if b"chunked" in rheaders.get(b"transfer-encoding", b""):
            clean_end, keep = _pump_chunked(uf, _read, _send, upstream_close)
        elif b"content-length" in rheaders:
            clean_end, keep = _pump_length(
                uf, _read, _send, int(rheaders[b"content-length"]),
                upstream_close)
        else:
            clean_end, keep = _pump_eof(uf, _read, _send)

        if not _committed and held is None:
            _emit_cut(emit, seq, call_n, lane, t0, "no body",
                      max_wait, client_wait, kept is not None, _keys())
            return "no body", 0, b"", True

        if keep and held is None and pool is not None:
            lane.drop_stream(up)
            pool.give(lane, up, uf)
            up = None

        if not clean_end:
            _dump_cut(seq, call_n, lane, "eof-mid-body", bytes(_cap), total)

        _sticky_sample(relay, lane, t0, first_byte_at, first_event_at, model)

        if (first_event_at is not None
                and first_event_at - t0 >= min(_sticky_ceiling(relay, lane),
                                               _SLOW_EXIT_S)):
            _note_slow(relay, lane, emit, seq, call_n,
                       round(first_event_at - t0, 1))

        if _DEBUG_UPSTREAM and not is_sse:
            _debug_upstream(out_head, status, rheaders, bytes(_cap))

        emit({"type": "callend", "t": time.time(), "n": seq, "c": call_n,
              "lane": lane.index, "cc": lane.exit_country, "status": status,
              "kb": round(total / 1024, 1),
              "secs": round(time.time() - t0, 1), "err": "",
              "cut": not clean_end,
              "reused": kept is not None,
              "retry_after": round(retry_after),
              "limit": section_of(note_buf, status),
              "note": _note_of(bytes(note_buf)),
              "max_wait_s": round(max_wait, 1),
              "client_wait_s": round(client_wait, 1),
              **_keys()})
        return "", status, bytes(held or b""), False
    except _Beaten:
        return _LOST
    except (ssl.SSLError, OSError, ValueError) as exc:
        err = f"{type(exc).__name__}"
        retryable = held is not None or not _committed
        seen = total if _committed else len(_open)
        if (_witness() and err.endswith("TimeoutError")
                and last_op == "upstream" and _may()):
            _note_timeout(relay, lane, emit, seq, call_n)
        elif (_witness() and isinstance(exc, ssl.SSLError)
                and last_op == "upstream" and _may()):
            _note_ssl(relay, lane, emit, seq, call_n)
        if _committed and _cap:
            _dump_cut(seq, call_n, lane, err, bytes(_cap), seen)
        emit({"type": "callend", "t": time.time(), "n": seq, "c": call_n,
              "lane": lane.index, "cc": lane.exit_country,
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
        beat_stop.set()
        if up is not None:
            lane.drop_stream(up)
            _close_quiet(up)
