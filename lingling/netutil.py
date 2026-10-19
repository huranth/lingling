"""Loopback + SOCKS5 network helpers"""

from __future__ import annotations

import errno
import os
import pathlib
import platform
import socket
import ssl
import struct
import subprocess
import threading
import time
from typing import Optional, Container, Tuple

from . import bounds

PORT_CHECK_TIMEOUT = 1.0

_NO_WINDOW = 0x08000000


def _quiet() -> dict:
    """creationflags that keep a console tool from opening a window."""
    return {"creationflags": _NO_WINDOW} if os.name == "nt" else {}


def _console_text(cmd: list[str], timeout: float) -> str:
    """One console tool's output, read as bytes and decoded leniently.

    ``text=True`` decodes with the interpreter's own codec, which is UTF-8
    under PYTHONUTF8=1 and cp949 under a Korean console.  The tools answer in
    the console's code page, so that decode raised UnicodeDecodeError and
    check_output returned None -- and the caller then walked None.  The bytes
    are what matter here, so they are decoded as latin-1, which never fails.
    """
    raw = subprocess.check_output(cmd, timeout=timeout, **_quiet())
    return raw.decode("latin-1", "replace")

UPSTREAM_UNAVAILABLE = b'"server_error"'

QUOTA_SPENT = b'"FreeUsageLimitError"'


def _pid_alive_local(pid: int) -> bool:
    """Is that pid running right now, from the OS's own table."""
    if platform.system().lower() != "windows":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        output = _console_text(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], 5)
        return str(pid) in output and "No tasks" not in output
    except Exception:
        return True


_session_mutex = threading.Lock()
_session_handles: dict[pathlib.Path, int] = {}


def chmod_quiet(path, mode: int) -> None:
    """Restrict a file or directory; best effort, meaningful on POSIX only."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def session_lock(data_dir: str) -> Optional[int]:
    """Hold an OS lock until release; None acquired, pid/-1 busy, OSError failed."""
    path = pathlib.Path(data_dir).resolve() / "session.lock"
    with _session_mutex:
        if path in _session_handles:
            return os.getpid()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path.with_name("session.guard")),
                     os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                try:
                    text = path.read_text(encoding="utf-8").strip()
                except OSError:
                    text = ""
                return int(text) if text.isdigit() and int(text) > 0 else -1
            acquired = True
            try:
                text = path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                text = ""
            pid = int(text) if text.isdigit() else 0
            if pid and _pid_alive_local(pid):
                return pid
            path.write_text(str(os.getpid()), encoding="utf-8")
            _session_handles[path] = fd
            fd = -1
            return None
        finally:
            if fd != -1:
                if acquired and os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                os.close(fd)


def release_session_lock(data_dir: str) -> None:
    """Remove our PID before releasing the stable, OS-held guard."""
    path = pathlib.Path(data_dir).resolve() / "session.lock"
    with _session_mutex:
        fd = _session_handles.pop(path, None)
        if fd is None:
            return
        try:
            if path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                path.unlink()
        except OSError:
            pass
        finally:
            os.close(fd)

SOCKS_SLOTS = bounds.count("LINGLING_SOCKS_SLOTS", 8, 1, 64)


def slot_cred(lane_index: int, slot: int) -> Tuple[str, str]:
    """Username for one of a lane's isolated circuits."""
    return f"L{lane_index}s{slot % SOCKS_SLOTS}", "x"


def lane_cred(lane_index: int) -> Tuple[str, str]:
    """The one circuit a lane's probe and its live traffic share."""
    return slot_cred(lane_index, 0)


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
    """True if a fresh socket can bind+listen (Windows excluded port ranges)."""
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
    """First port near ``start_port`` that is free and bindable."""
    taken = reserved or frozenset()
    for offset in range(max_offset):
        port = start_port + offset
        if port in taken:
            continue
        if bindable(port, host) and not port_is_open(host, port):
            return port
    raise RuntimeError(f"no free TCP port found near {start_port} on {host}")


def pids_on_ports(ports: Container[int]) -> dict:
    """``{port: pid}`` for the given ports that something is LISTENING on."""
    if platform.system().lower() != "windows":
        return {}
    want = set(ports)
    found = {}
    try:
        output = _console_text(["netstat", "-ano"], 20)
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


def abort_quiet(sock) -> None:
    """Cut a connection loose, so a read blocked on it lets go at once."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except (OSError, AttributeError):
        pass
    try:
        sock.close()
    except (OSError, AttributeError):
        pass


def _recv_exact(sock: socket.socket, n: int,
                deadline: Optional[float] = None) -> Optional[bytes]:
    """Exactly ``n`` bytes, or None if the peer closed early."""
    buf = b""
    while len(buf) < n:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout("SOCKS5 deadline expired")
            sock.settimeout(left)
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def socks5_open(sock: socket.socket, host: str, port: int,
                cred: Optional[Tuple[str, str]] = None) -> str:
    """SOCKS5 CONNECT on a connected socket; "" on success, else a reason.

    A port outside 0..65535 is refused here rather than packed: struct.pack
    raises struct.error for it, and struct.error is neither an OSError nor a
    ValueError, so it escaped every handler and killed the worker thread.
    """
    window = sock.gettimeout()
    deadline = (time.monotonic() + window) if window else None

    def send(data: bytes) -> None:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout("SOCKS5 deadline expired")
            sock.settimeout(left)
        sock.sendall(data)

    try:
        if cred is None:
            send(bytes([0x05, 0x01, 0x00]))
            resp = _recv_exact(sock, 2, deadline)
            if resp is None or resp[0] != 0x05 or resp[1] != 0x00:
                return "bad SOCKS5 greeting"
        else:
            send(bytes([0x05, 0x02, 0x00, 0x02]))
            resp = _recv_exact(sock, 2, deadline)
            if resp is None or resp[0] != 0x05:
                return "bad SOCKS5 greeting"
            if resp[1] == 0x02:
                user, password = cred
                ub = user.encode("utf-8")[:255]
                pb = password.encode("utf-8")[:255]
                send(bytes([0x01, len(ub)]) + ub + bytes([len(pb)]) + pb)
                auth = _recv_exact(sock, 2, deadline)
                if auth is None or auth[0] != 0x01 or auth[1] != 0x00:
                    return "SOCKS5 auth refused"
            elif resp[1] != 0x00:
                return "bad SOCKS5 greeting"
        addr = host.encode("idna")
        if not 1 <= port <= 65535:
            return "bad port"
        send(bytes([0x05, 0x01, 0x00, 0x03, len(addr)])
             + addr + struct.pack("!H", port))
        head = _recv_exact(sock, 4, deadline)
        if head is None or head[0] != 0x05:
            return "bad SOCKS5 reply"
        if head[1] != 0x00:
            return SOCKS_REPLY_CODES.get(head[1], f"reply code {head[1]}")
        atyp = head[3]
        if atyp == 0x01:
            rest = 4 + 2
        elif atyp == 0x03:
            first = _recv_exact(sock, 1, deadline)
            if first is None:
                return "bad SOCKS5 reply"
            rest = first[0] + 2
        elif atyp == 0x04:
            rest = 16 + 2
        else:
            return f"bad address type {atyp}"
        if _recv_exact(sock, rest, deadline) is None:
            return "bad SOCKS5 reply"
        return ""
    except socket.timeout:
        return "timed out"
    except OSError as exc:
        return f"tcp: {type(exc).__name__}"
    finally:
        sock.settimeout(window)


def https_via_socks(proxy_port: int, host: str, method: str, path: str,
                    user_agent: str, body: bytes = b"",
                    extra_headers: Optional[dict] = None,
                    timeout: float = 15.0,
                    max_body: int = 8 * 1024 * 1024,
                    cred: Optional[Tuple[str, str]] = None
                    ) -> Tuple[int, bytes]:
    """HTTPS request through a Tor lane's SOCKS5 port; returns (status, body)."""
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=timeout)
    try:
        err = socks5_open(sock, host, 443, cred=cred)
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
