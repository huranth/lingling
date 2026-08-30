"""Local rotating CONNECT relay -- the proxy that sits in front of OpenCode.

OpenCode is launched with ``HTTPS_PROXY=http://127.0.0.1:<port>`` so every
request it makes arrives here as a ``CONNECT host:443``. We pick the
least-busy healthy lane, open a SOCKS5 tunnel through its tor.exe to the
target, answer ``200 Connection Established``, and pipe bytes both ways.
Everything is end-to-end TLS beyond this point -- we never see content,
which is exactly why the health daemon does the 429 detection proactively.

Lane selection is least-active among healthy lanes (round-robin on ties), so
load spreads across distinct exit IPs by construction and a freshly burned
lane simply stops being picked.
"""

from __future__ import annotations

import asyncio
import itertools
import struct
import threading
import time
from typing import Callable, Dict, List, Optional

from .lanes import Lane, TorManager

_SOCKS_FAIL = {
    1: "general failure", 2: "not allowed", 3: "network unreachable",
    4: "host unreachable", 5: "connection refused", 6: "TTL expired",
    7: "cmd unsupported", 8: "bad address type",
}


class Relay:
    def __init__(
        self,
        tor: TorManager,
        host: str = "127.0.0.1",
        port: int = 0,
        event: Optional[Callable[[Dict], None]] = None,
        dial_timeout: float = 20.0,
    ) -> None:
        self.tor = tor
        self.host = host
        self.port = port  # 0 = ask the OS; read .port after start()
        self._emit = event or (lambda e: None)
        self.dial_timeout = dial_timeout
        #: How long a request waits for a lane to become usable before we
        #: admit defeat with a 502. OpenCode retries a failed connect loudly
        #: ("Cannot connect to API"); a quiet hold is invisible to the user.
        self.wait_budget = float(
            __import__("os").environ.get("LINGLING_LANE_WAIT", "60"))
        self._server: Optional[asyncio.AbstractServer] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()
        self._seq = itertools.count(1)
        self._rr = itertools.count()

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> int:
        """Serve on a background thread. Returns the bound port."""
        self._thread = threading.Thread(
            target=self._run, name="lane-relay", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("relay failed to bind")
        return self.port

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())
        self._loop.run_forever()

    async def _serve(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._shutdown)
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _shutdown(self) -> None:
        if self._server:
            self._server.close()
        # Drop the loop on the next tick so close() can land first.
        self._loop.call_later(0.2, self._loop.stop)

    # -- lane picking --------------------------------------------------------
    def pick_lane(self, exclude: Optional[set] = None) -> Optional[Lane]:
        candidates: List[Lane] = [
            l for l in self.tor.healthy_lanes()
            if not exclude or l.index not in exclude
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda l: l.active)

    # -- connection handling --------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        seq = next(self._seq)
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=15)
        except (asyncio.TimeoutError, ConnectionError):
            writer.close()
            return
        try:
            method, target, _ = line.decode("latin1").split(" ", 2)
        except ValueError:
            writer.close()
            return
        if method.upper() != "CONNECT":
            # Drain headers, then refuse: only tunnelling is supported. Plain
            # HTTP forwarding would mean terminating requests in cleartext.
            try:
                while True:
                    h = await asyncio.wait_for(reader.readline(), timeout=5)
                    if h in (b"\r\n", b"\n", b""):
                        break
            except asyncio.TimeoutError:
                pass
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n"
                         b"Content-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        host, _, port_s = target.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            writer.close()
            return
        # Drain the client's remaining CONNECT headers.
        try:
            while True:
                h = await asyncio.wait_for(reader.readline(), timeout=5)
                if h in (b"\r\n", b"\n", b""):
                    break
        except asyncio.TimeoutError:
            writer.close()
            return

        lane: Optional[Lane] = None
        upstream_r: Optional[asyncio.StreamReader] = None
        upstream_w: Optional[asyncio.StreamWriter] = None
        tried: set = set()
        err_note = ""
        deadline = time.time() + self.wait_budget
        held = False
        while True:
            for _ in range(max(1, len(self.tor.lanes))):
                lane = self.pick_lane(exclude=tried)
                if lane is None:
                    break
                tried.add(lane.index)
                try:
                    upstream_r, upstream_w = await self._dial(lane, host, port)
                    break
                except Exception as exc:  # noqa: BLE001
                    err_note = str(exc)
                    self._emit({
                        "type": "lane", "kind": "fail", "t": time.time(),
                        "lane": lane.index, "cc": lane.exit_country,
                        "ip": lane.exit_ip,
                        "msg": f"lane {lane.index} couldn't reach {host} "
                               f"({err_note}) -- switching lanes",
                    })
                    # A dial failure is a live signal the daemon hasn't seen.
                    lane.healthy = False
                    lane = None
            if lane is not None:
                break
            # No lane usable right now (still cooking, or all burned at
            # once). Hold the connection quietly and re-check -- far better
            # than an instant 502 that surfaces as "Cannot connect to API".
            if time.time() >= deadline:
                break
            if not held:
                held = True
                self._emit({
                    "type": "lane", "kind": "heal", "t": time.time(),
                    "lane": 0, "cc": "", "ip": "",
                    "msg": f"holding a request for {host} while a lane "
                           f"finishes cooking ...",
                })
            tried.clear()
            await asyncio.sleep(0.5)
        if lane is None or upstream_w is None or upstream_r is None:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n"
                         b"Content-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            self._emit({
                "type": "req", "t": time.time(), "n": seq, "lane": 0,
                "cc": "", "ip": "", "target": f"{host}:{port}", "ok": False,
                "note": "no lane available",
            })
            return

        with lane.lock:
            lane.active += 1
        self._emit({
            "type": "req", "t": time.time(), "n": seq, "lane": lane.index,
            "cc": lane.exit_country, "ip": lane.exit_ip,
            "target": f"{host}:{port}", "ok": True, "note": "",
        })
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        try:
            await self._pipe(reader, writer, upstream_r, upstream_w)
        finally:
            with lane.lock:
                lane.active -= 1
            try:
                upstream_w.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _dial(self, lane: Lane, host: str, port: int
                    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a connection to the lane's SOCKS5 port and CONNECT through
        it to (host, port). Domain form (atyp=0x03) so DNS resolves at the
        exit -- the exit IP must be the lane's, not ours."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", lane.socks_port),
            timeout=self.dial_timeout)

        async def _io() -> asyncio.StreamWriter:
            writer.write(bytes([0x05, 0x01, 0x00]))
            await writer.drain()
            resp = await reader.readexactly(2)
            if resp[0] != 0x05 or resp[1] != 0x00:
                raise ConnectionError("bad SOCKS5 greeting")
            addr = host.encode("idna")
            writer.write(bytes([0x05, 0x01, 0x00, 0x03, len(addr)])
                         + addr + struct.pack("!H", port))
            await writer.drain()
            head = await reader.readexactly(4)
            if head[1] != 0x00:
                raise ConnectionError(
                    _SOCKS_FAIL.get(head[1], f"socks reply {head[1]}"))
            atyp = head[3]
            if atyp == 0x01:
                await reader.readexactly(4)
            elif atyp == 0x03:
                ln = (await reader.readexactly(1))[0]
                await reader.readexactly(ln)
            elif atyp == 0x04:
                await reader.readexactly(16)
            await reader.readexactly(2)  # bound port
            return writer

        try:
            await asyncio.wait_for(_io(), timeout=self.dial_timeout)
        except Exception:
            writer.close()
            raise
        return reader, writer

    async def _pipe(self, client_reader: asyncio.StreamReader,
                    client_writer: asyncio.StreamWriter,
                    upstream_reader: asyncio.StreamReader,
                    upstream_writer: asyncio.StreamWriter) -> None:
        """Shuttle bytes both ways until either side closes."""
        async def pump(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await r.read(65536)
                    if not data:
                        break
                    w.write(data)
                    await w.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                try:
                    w.close()
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.gather(
            pump(client_reader, upstream_writer),
            pump(upstream_reader, client_writer),
            return_exceptions=True,
        )
