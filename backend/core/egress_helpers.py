"""Portable egress helpers shared by the Tor lane manager and health daemon.

Intentionally generic utilities for operating the Tor egress lanes:

* ``_port_is_open`` / ``_find_free_port`` — loopback TCP reachability + free-port
  discovery (used to reserve the SOCKS / control port space per lane).
* ``_pid_on_port`` / ``_pid_alive`` / ``_kill_pid`` — Windows-only orphan cleanup
  for tor.exe children left holding a loopback port by a previous run.
* ``socks5_connect_check`` / ``_socks5_http_probe`` — a raw SOCKS5 CONNECT that
  proves a tunnel can actually reach a target, plus the OpenCode-specific thin
  wrapper. httpx et al. cannot be trusted to bound the SOCKS5 handshake, so the
  health probe uses this raw socket path instead.

Nothing in this module is tied to a specific egress vendor; it is the single,
shared implementation Tor needs.
"""

from __future__ import annotations

import platform
import socket
import struct
import subprocess
import time
from typing import Optional, Any, Container

# Health thresholds shared with the Tor health daemon. Single source of truth.
PORT_CHECK_TIMEOUT = 1.0          # seconds for TCP port check
MAX_CONSECUTIVE_FAILURES = 10     # this many consecutive failures -> dump lane
MAX_429_TOTAL = 50                # this many lifetime 429s -> dump lane


def _port_is_open(host: str, port: int, timeout: float = 0.2) -> bool:
    """Return True if a TCP connection to host:port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _bindable(port: int, host: str = "127.0.0.1") -> bool:
    """Return True if a fresh socket can actually bind+listen on host:port.

    ``_port_is_open`` only proves nothing is *listening*; on Windows a port
    inside an administratively excluded range (Hyper-V/WSL reserves blocks
    like 52093-52192) still ``bind()``s with WSAEACCES even when it is
    perfectly free, and a tor child then dies at launch with "Failed to bind
    one of the listener ports". The real test is the bind itself.
    """
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


def _find_free_port(
    start_port: int,
    host: str = "127.0.0.1",
    max_offset: int = 200,
    reserved: Optional[Container[int]] = None,
) -> int:
    """Find the next free TCP port at or after start_port.

    ``reserved`` is a set of ports that are spoken for even though nothing is
    listening on them right now. Without it a regenerating lane could take a
    *sibling's* assigned port: siblings are frequently down at exactly the
    moment the health daemon is regenerating, so their ports scan as free. Both
    torrcs then wrote the same SocksPort and whichever tor started second failed
    to bind, losing an exit until someone noticed.

    A port counts as free only if it is neither listening nor inside a
    Windows-administered exclusion range (see :func:`_bindable`) -- the
    exclusion ranges are silent bind-killers otherwise.
    """
    taken = reserved or frozenset()
    for offset in range(max_offset):
        port = start_port + offset
        if port in taken:
            continue
        if not _port_is_open(host, port) and _bindable(port, host):
            return port
    raise RuntimeError(f"no free TCP port found near {start_port} on {host}")


def _pid_on_port(port: int) -> Optional[int]:
    """Return the PID listening on 127.0.0.1:port (Windows-only fast path)."""
    if platform.system().lower() != "windows":
        return None
    try:
        output = subprocess.check_output(
            ["netstat", "-ano"], text=True, timeout=5,
        )
        for line in output.splitlines():
            if f"127.0.0.1:{port}" in line:
                parts = line.strip().split()
                if parts:
                    try:
                        return int(parts[-1])
                    except ValueError:
                        pass
    except Exception:  # noqa: BLE001
        pass
    return None


def _pid_alive(pid: int) -> bool:
    """Return True if a Windows process with this PID is still running."""
    if platform.system().lower() != "windows":
        return False
    try:
        output = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], text=True, timeout=5,
        )
        return str(pid) in output and "No tasks" not in output
    except Exception:  # noqa: BLE001
        return False


def _kill_pid(pid: int, grace_s: float = 2.0) -> bool:
    """Kill a process by PID. Windows-only for now."""
    if platform.system().lower() != "windows":
        return False
    try:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass
    # Give the process a moment to exit, then force kill if still alive.
    deadline = time.time() + grace_s
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, text=True, timeout=10)
        return True
    except Exception:  # noqa: BLE001
        return False


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


def socks5_connect_check(
    proxy_url: str, host: str, port: int = 443,
    timeout: float = PORT_CHECK_TIMEOUT,
) -> str:
    """Raw SOCKS5 CONNECT through ``proxy_url`` to ``(host, port)`` under a hard timeout.

    Returns ``""`` when the proxy completed the handshake and connected;
    otherwise a short reason string. This exists because httpx cannot be trusted
    to bound this exchange: its SOCKS5 handshake reads run with no timeout
    (httpcore passes none down, and the sync backend turns that into a blocking
    ``recv``), so a lane that accepts TCP but never answers would park an httpx
    request forever no matter what timeout it was given. The socket timeout set
    here covers connect, handshake and reply alike.
    """
    scheme = proxy_url.split("://", 1)[0] if "://" in proxy_url else ""
    if scheme not in ("socks5", "socks5h"):
        return "not a socks5:// proxy"
    rest = proxy_url.split("://", 1)[1]
    if rest.startswith("["):  # [v6]:port
        host_part, _, port_part = rest.partition("]")
        proxy_host = host_part[1:]
        proxy_port = int(port_part.lstrip(":")) if port_part.startswith(":") else 1080
    else:
        proxy_host, sep, port_part = rest.rpartition(":")
        if not sep or not port_part.isdigit():
            return "unparseable proxy address"
        proxy_host, proxy_port = proxy_host, int(port_part)
    if not proxy_host:
        return "unparseable proxy address"

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((proxy_host, proxy_port))
        # SOCKS5 greet: no auth
        sock.sendall(bytes([0x05, 0x01, 0x00]))
        resp = sock.recv(2)
        if len(resp) != 2 or resp[0] != 0x05 or resp[1] != 0x00:
            return "bad SOCKS5 greeting"
        # CONNECT request with a domain-name target
        addr = host.encode("ascii")
        sock.sendall(
            bytes([0x05, 0x01, 0x00, 0x03, len(addr)]) + addr + struct.pack("!H", port)
        )
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
    finally:
        try:
            sock.close()
        except OSError:  # noqa: BLE001
            pass


def _socks5_http_probe(
    proxy_url: str,
    target_host: str = "opencode.ai",
    target_port: int = 443,
    timeout: float = PORT_CHECK_TIMEOUT,
) -> bool:
    """Full SOCKS5 CONNECT through a proxy to verify the tunnel works.

    Uses opencode.ai as the probe target (the actual upstream). If the proxy can
    reach opencode.ai on port 443, it's healthy enough for routing. ``socks5h://``
    (remote DNS) is normalised for the raw handshake -- resolution happens over
    the tunnel either way.
    """
    return socks5_connect_check(proxy_url, target_host, target_port, timeout) == ""