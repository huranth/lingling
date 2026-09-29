"""Loopback + SOCKS5 network helpers. Raw-socket by design: httpx cannot be
trusted to bound the SOCKS5 handshake, so one socket timeout covers the lot."""

from __future__ import annotations

import os
import platform
import socket
import ssl
import struct
import subprocess
import time
from typing import Optional, Container, Tuple

PORT_CHECK_TIMEOUT = 1.0

#: circuit slots
SOCKS_SLOTS = int(os.environ.get("LINGLING_SOCKS_SLOTS", "8"))


def slot_cred(lane_index: int, slot: int) -> Tuple[str, str]:
    """Username for one of a lane's isolated circuits.

    Tor keys circuit isolation on the SOCKS **username** -- ``IsolateSOCKSAuth``
    is on by default and a bare no-auth greeting gives every stream the same
    empty key. So without a credential a whole lane shares one circuit, and one
    ``RELAY_END`` takes every concurrent stream on it down together. One
    username per slot buys one circuit per slot, which is what bounds that
    blast radius. The password is never part of the key; it only has to be
    non-empty."""
    return f"L{lane_index}s{slot % SOCKS_SLOTS}", "x"


#: SOCKS5 replies
SOCKS_REPLY_CODES = {
    1: "general SOCKS server failure", 2: "connection not allowed",
    3: "network unreachable", 4: "host unreachable", 5: "connection refused",
    6: "TTL expired", 7: "command not supported",
    8: "address type not supported"}


def port_is_open(host: str, port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def bindable(port: int, host: str = "127.0.0.1") -> bool:
    """True if a fresh socket can bind+listen -- catches Windows' excluded
    port ranges (Hyper-V/WSL), which report free but fail bind (WSAEACCES)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            s.listen(1)
            return True
        finally:
            s.close()
    except OSError:
        return False


def find_free_port(start_port: int, host: str = "127.0.0.1",
                   max_offset: int = 200,
                   reserved: Optional[Container[int]] = None) -> int:
    """First port near ``start_port`` that is free and bindable.

    ``bindable`` is checked first on purpose. It is a local bind, so it is
    instant, while ``port_is_open`` pays a connect timeout on every port that
    black-holes -- and a Windows-excluded range (Hyper-V/WSL) black-holes all
    of them. Testing open-first cost ~0.2s per port, so a 200-port scan took
    ~40s and then failed anyway. The predicate is unchanged; only the order."""
    taken = reserved or frozenset()
    for offset in range(max_offset):
        port = start_port + offset
        if port in taken:
            continue
        if bindable(port, host) and not port_is_open(host, port):
            return port
    raise RuntimeError(f"no free TCP port found near {start_port} on {host}")


def pid_on_port(port: int) -> Optional[int]:
    if platform.system().lower() != "windows":
        return None
    try:
        output = subprocess.check_output(["netstat", "-ano"], text=True, timeout=5)
        for line in output.splitlines():
            if f"127.0.0.1:{port}" in line:
                parts = line.strip().split()
                if parts:
                    try:
                        return int(parts[-1])
                    except ValueError:
                        pass
    except Exception:
        pass
    return None


def _pid_alive(pid: int) -> bool:
    if platform.system().lower() != "windows":
        return False
    try:
        output = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], text=True, timeout=5)
        return str(pid) in output and "No tasks" not in output
    except Exception:
        return False


def kill_pid(pid: int, grace_s: float = 2.0) -> bool:
    if platform.system().lower() != "windows":
        return False
    try:
        subprocess.run(["taskkill", "/PID", str(pid)],
                       capture_output=True, text=True, timeout=10)
    except Exception:
        pass
    deadline = time.time() + grace_s
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, text=True, timeout=10)
        return True
    except Exception:
        return False


def pids_on_ports(ports: Container[int]) -> dict:
    """``{port: pid}`` for the given ports that something is LISTENING on.

    One `netstat` for the lot. `pid_on_port` shells out per port, which is
    fine for the two ports a lane owns and hopeless for a sweep across the
    whole lane range -- that ran `netstat` thousands of times and took minutes.
    """
    if platform.system().lower() != "windows":
        return {}
    want = set(ports)
    found = {}
    try:
        output = subprocess.check_output(["netstat", "-ano"], text=True,
                                         timeout=20)
    except Exception:  # noqa: BLE001
        return {}
    for line in output.splitlines():
        if "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        local = parts[1]
        if ":" not in local:
            continue
        try:
            port = int(local.rsplit(":", 1)[1])
        except ValueError:
            continue
        if port in want:
            try:
                found[port] = int(parts[-1])
            except ValueError:
                pass
    return found


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    """Exactly ``n`` bytes, or None if the peer closed early.

    A timeout is deliberately NOT caught here: it has to reach the caller's
    ``except socket.timeout`` so a stalled handshake still reports "timed out"
    rather than being relabelled a malformed reply."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def socks5_open(sock: socket.socket, host: str, port: int,
                cred: Optional[Tuple[str, str]] = None) -> str:
    """SOCKS5 CONNECT on a connected socket; "" on success, else a reason.

    ``cred`` selects username/password auth instead of no-auth. That is not
    access control: Tor's ``IsolateSOCKSAuth`` is on by default and the
    username is the circuit's isolation key, so a credential is how one lane
    gets many circuits instead of one. Omitting it keeps the old no-auth
    greeting, which every existing caller and verify suite still relies on.

    Two blocking reads: the greeting reply, then the CONNECT reply, which is
    where Tor actually builds the circuit. A socket timeout is per read, not a
    budget, so arming it once let a dead exit spend the window TWICE -- measured
    against a stalled upstream, a 3s window failed in 3.0s when the greeting was
    prompt and 4.5s when the greeting itself was 1.5s late. That is why the
    log's cold-connect timeouts land at 30-60s rather than at 30s, and it is
    worth knowing before reading any timeout number as a single window.

    So the second read gets only what is LEFT of the window, which is what this
    module always claimed: one socket timeout covers the lot. A handshake that
    needs longer than one window fails here, and the caller retries on another
    lane -- the window is not shortened, because the log's successful attempts
    have a p99 of 26.8s to their first byte, so it is doing real work.

    The CONNECT reply is variable length: VER REP RSV ATYP, then a BND.ADDR
    whose size ATYP decides, then BND.PORT. Reading a flat 10 bytes is right
    only for ATYP=0x01. Tor does return 0x01 with 0.0.0.0:0 today -- verified
    on a live lane -- so this was latent rather than live, but a domain or IPv6
    BND.ADDR would leave 12+ bytes in the buffer and the TLS handshake that
    follows would read them as its own first bytes. `relay._dial` has always
    parsed all three; this now matches it."""
    # one window
    window = sock.gettimeout()
    deadline = (time.time() + window) if window else None
    try:
        if cred is None:
            sock.sendall(bytes([0x05, 0x01, 0x00]))
            resp = sock.recv(2)
            if len(resp) != 2 or resp[0] != 0x05 or resp[1] != 0x00:
                return "bad SOCKS5 greeting"
        else:
            # both methods
            sock.sendall(bytes([0x05, 0x02, 0x00, 0x02]))
            resp = sock.recv(2)
            if len(resp) != 2 or resp[0] != 0x05:
                return "bad SOCKS5 greeting"
            if resp[1] == 0x02:
                user, password = cred
                ub = user.encode("utf-8")[:255]
                pb = password.encode("utf-8")[:255]
                sock.sendall(bytes([0x01, len(ub)]) + ub
                             + bytes([len(pb)]) + pb)
                auth = _recv_exact(sock, 2)
                if auth is None or auth[0] != 0x01 or auth[1] != 0x00:
                    return "SOCKS5 auth refused"
            elif resp[1] != 0x00:
                return "bad SOCKS5 greeting"
        # idna hosts
        addr = host.encode("idna")
        sock.sendall(bytes([0x05, 0x01, 0x00, 0x03, len(addr)])
                     + addr + struct.pack("!H", port))
        if deadline is not None:
            # remaining budget
            left = deadline - time.time()
            if left <= 0.0:
                return "timed out"
            sock.settimeout(left)
        # variable reply
        head = _recv_exact(sock, 4)
        if head is None or head[0] != 0x05:
            return "bad SOCKS5 reply"
        if head[1] != 0x00:
            return SOCKS_REPLY_CODES.get(head[1], f"reply code {head[1]}")
        atyp = head[3]
        if atyp == 0x01:
            rest = 4 + 2
        elif atyp == 0x03:
            first = _recv_exact(sock, 1)
            if first is None:
                return "bad SOCKS5 reply"
            rest = first[0] + 2
        elif atyp == 0x04:
            rest = 16 + 2
        else:
            return f"bad address type {atyp}"
        if _recv_exact(sock, rest) is None:
            return "bad SOCKS5 reply"
        return ""
    except socket.timeout:
        return "timed out"
    except OSError as exc:
        return f"tcp: {type(exc).__name__}"


def https_via_socks(proxy_port: int, host: str, method: str, path: str,
                    user_agent: str, body: bytes = b"",
                    extra_headers: Optional[dict] = None,
                    timeout: float = 15.0,
                    max_body: int = 8 * 1024 * 1024) -> Tuple[int, bytes]:
    """HTTPS request through a Tor lane's SOCKS5 port; returns (status, body).
    Raises on transport failure (lane-dead); status 429 means the exit IP
    has hit opencode's free tier limit for this window."""
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=timeout)
    try:
        err = socks5_open(sock, host, 443)
        if err:
            raise ConnectionError(f"socks5: {err}")
        ctx = ssl.create_default_context()
        tls = ctx.wrap_socket(sock, server_hostname=host)
        try:
            headers = {
                "Host": host,
                "User-Agent": user_agent,
                "Accept": "*/*",
                "Connection": "close",
            }
            if body:
                headers["Content-Type"] = "application/json"
                headers["Content-Length"] = str(len(body))
            if extra_headers:
                headers.update(extra_headers)
            head = f"{method} {path} HTTP/1.1\r\n" + "".join(
                f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
            tls.sendall(head.encode("ascii") + body)
            tls.settimeout(timeout)
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 65536:
                chunk = tls.recv(8192)
                if not chunk:
                    break
                buf += chunk
            raw_head, _, body = buf.partition(b"\r\n\r\n")
            status_line = raw_head.split(b"\r\n", 1)[0].decode("latin1", "replace")
            code = int(status_line.split(" ")[1]) if " " in status_line else 0
            chunked = b"transfer-encoding: chunked" in raw_head.lower()
            end = time.time() + timeout
            while len(body) < max_body and time.time() < end:
                try:
                    chunk = tls.recv(65536)
                except (socket.timeout, ssl.SSLError):
                    break
                if not chunk:
                    break
                body += chunk
            if chunked:
                body = _decode_chunked(body)
            return code, body
        finally:
            try:
                tls.close()
            except OSError:
                pass
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _decode_chunked(body: bytes) -> bytes:
    """Minimal HTTP chunked decoder; returns what it can on truncation."""
    out = bytearray()
    i = 0
    while True:
        j = body.find(b"\r\n", i)
        if j < 0:
            break
        try:
            size = int(body[i:j].split(b";")[0].strip(), 16)
        except ValueError:
            break
        if size == 0:
            break
        i = j + 2
        out += body[i:i + size]
        i += size + 2
    return bytes(out)


def https_get_via_socks(proxy_port: int, host: str, path: str,
                        user_agent: str, timeout: float = 15.0
                        ) -> Tuple[int, bytes]:
    """HTTPS GET through a lane's SOCKS5 port (health-probe path)."""
    return https_via_socks(proxy_port, host, "GET", path, user_agent,
                           timeout=timeout, max_body=65536)
