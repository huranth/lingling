"""Loopback + SOCKS5 network helpers.

Everything here is raw-socket on purpose: httpx et al. cannot be trusted to
bound the SOCKS5 handshake (their sync backends turn a stuck lane into an
unbounded ``recv``), so the health probe and the relay's upstream dial both
go through these primitives with one socket timeout covering the lot.
"""

from __future__ import annotations

import platform
import socket
import ssl
import struct
import subprocess
import time
from typing import Optional, Container, Tuple

PORT_CHECK_TIMEOUT = 1.0

_SOCKS_REPLY_CODES = {
    1: "general SOCKS server failure",
    2: "connection not allowed",
    3: "network unreachable",
    4: "host unreachable",
    5: "connection refused",
    6: "TTL expired",
    7: "command not supported",
    8: "address type not supported",
}


def port_is_open(host: str, port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def bindable(port: int, host: str = "127.0.0.1") -> bool:
    """True if a fresh socket can actually bind+listen -- catches Windows'
    administratively excluded port ranges (Hyper-V/WSL), which report free
    but fail bind with WSAEACCES."""
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
    taken = reserved or frozenset()
    for offset in range(max_offset):
        port = start_port + offset
        if port in taken:
            continue
        if not port_is_open(host, port) and bindable(port, host):
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


def socks5_open(sock: socket.socket, host: str, port: int) -> str:
    """Run the SOCKS5 no-auth CONNECT handshake on an already-connected
    socket. Returns "" on success, else a short reason string."""
    try:
        sock.sendall(bytes([0x05, 0x01, 0x00]))
        resp = sock.recv(2)
        if len(resp) != 2 or resp[0] != 0x05 or resp[1] != 0x00:
            return "bad SOCKS5 greeting"
        addr = host.encode("ascii")
        sock.sendall(bytes([0x05, 0x01, 0x00, 0x03, len(addr)])
                     + addr + struct.pack("!H", port))
        resp = sock.recv(10)
        if len(resp) < 2 or resp[0] != 0x05:
            return "bad SOCKS5 reply"
        if resp[1] != 0x00:
            return _SOCKS_REPLY_CODES.get(resp[1], f"reply code {resp[1]}")
        return ""
    except socket.timeout:
        return "timed out"
    except OSError as exc:
        return f"tcp: {type(exc).__name__}"


def socks5_connect(proxy_port: int, host: str, port: int,
                   timeout: float = PORT_CHECK_TIMEOUT) -> str:
    """Raw SOCKS5 CONNECT through 127.0.0.1:proxy_port. "" = tunnel works."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(("127.0.0.1", proxy_port))
        return socks5_open(sock, host, port)
    except socket.timeout:
        return "timed out"
    except OSError as exc:
        return f"tcp: {type(exc).__name__}"
    finally:
        try:
            sock.close()
        except OSError:
            pass


def https_get_via_socks(proxy_port: int, host: str, path: str,
                        user_agent: str, timeout: float = 15.0
                        ) -> Tuple[int, bytes]:
    """HTTPS GET through a Tor lane's SOCKS5 port.

    Returns ``(status_code, body_prefix)``. Raises on transport failure --
    callers treat "raised" as lane-dead and ``status == 429`` as lane-burned,
    which are different problems with different heals.
    """
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=timeout)
    try:
        err = socks5_open(sock, host, 443)
        if err:
            raise ConnectionError(f"socks5: {err}")
        ctx = ssl.create_default_context()
        tls = ctx.wrap_socket(sock, server_hostname=host)
        try:
            req = (
                f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                f"User-Agent: {user_agent}\r\nAccept: */*\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("ascii")
            tls.sendall(req)
            tls.settimeout(timeout)
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 65536:
                chunk = tls.recv(8192)
                if not chunk:
                    break
                buf += chunk
            head, _, body = buf.partition(b"\r\n\r\n")
            status_line = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
            code = int(status_line.split(" ")[1]) if " " in status_line else 0
            # Read a little more body when the first segment was tiny --
            # callers only need a prefix (exit-IP JSON) not the whole page.
            end = time.time() + timeout
            while len(body) < 4096 and time.time() < end:
                try:
                    chunk = tls.recv(8192)
                except (socket.timeout, ssl.SSLError):
                    break
                if not chunk:
                    break
                body += chunk
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
