"""Local rotating CONNECT relay in front of OpenCode: picks the least-loaded
healthy lane, tunnels each CONNECT through its tor.exe SOCKS5, and pipes
bytes blindly (end-to-end TLS -- the health daemon detects 429s instead)."""

from __future__ import annotations

import asyncio
import itertools
import os
import struct
import threading
import time
from typing import Callable, Dict, List, Optional

from . import netutil
from .lanes import Lane, TorManager

#: connection cap
_MAX_CONNS = int(os.environ.get("LINGLING_MAX_CONNS", "256"))

#: losing country
_LOSING = int(os.environ.get("LINGLING_LOSING_SCORE", "-2"))
#: thin pool
_LOSING_MIN_POOL = int(os.environ.get("LINGLING_LOSING_MIN_POOL", "4"))


class Relay:
    def __init__(
        self,
        tor: TorManager,
        host: str = "127.0.0.1",
        port: int = 0,
        event: Optional[Callable[[Dict], None]] = None,
        dial_timeout: float = 50.0,
    ) -> None:
        self.tor = tor
        self.host = host
        self.port = port  #: auto port
        self._emit = event or (lambda e: None)
        self.dial_timeout = dial_timeout
        #: never 502
        self.wait_budget = float(os.environ.get("LINGLING_LANE_WAIT", "90"))
        self._server: Optional[asyncio.AbstractServer] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()
        self._seq = itertools.count(1)
        #: cert shop
        self.cert_shop = None  # lingling.mitm.CertShop
        #: idle tunnels
        self.tunnels = None  # lingling.mitm.TunnelPool
        #: connection slots
        self._slots = threading.BoundedSemaphore(_MAX_CONNS)
        #: live count
        self._live = 0
        self._live_lock = threading.Lock()

    def _slot_enter(self) -> bool:
        """Take a connection slot. False means we are at the cap.

        The count is tracked beside the semaphore because a
        ``BoundedSemaphore`` cannot report it, and "we are at the cap" without
        the number is what made the original 503s undiagnosable: a slot held
        by a connection that never finished looks identical to a genuine
        burst."""
        if not self._slots.acquire(blocking=False):
            return False
        with self._live_lock:
            self._live += 1
        return True

    def _slot_exit(self) -> None:
        with self._live_lock:
            self._live -= 1
        self._slots.release()

    # lifecycle
    def start(self) -> int:
        """Serve on a background thread. Returns the bound port."""
        self._thread = threading.Thread(
            target=self._run, name="lane-relay", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("relay failed to bind")
        return self.port

    def _run(self) -> None:
        # selector loop
        if os.name == "nt":
            asyncio.set_event_loop_policy(
                asyncio.WindowsSelectorEventLoopPolicy())
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.set_exception_handler(self._quiet)
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
        # stop soon
        self._loop.call_later(0.2, self._loop.stop)

    @staticmethod
    async def _answer(writer: asyncio.StreamWriter, head: bytes) -> bool:
        """Write a proxy head; False if the client already vanished."""
        try:
            writer.write(head)
            await writer.drain()
            return True
        except (ConnectionError, OSError):
            writer.close()
            return False

    @staticmethod
    def _quiet(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        """Clients abort tunnels constantly; that is not news."""
        if isinstance(context.get("exception"), ConnectionError):
            return
        loop.default_exception_handler(context)

    # mitm
    def _should_mitm(self, host: str) -> bool:
        if self.cert_shop is None:
            return False
        from . import mitm
        return any(host == h or host.endswith("." + h) for h in
                   mitm.MITM_HOSTS)

    async def _handle_mitm(self, host: str, port: int, seq: int,
                           reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter) -> None:
        """200, dup socket, hand it to a MITM thread."""
        # bounded
        if not self._slot_enter():
            self._emit({
                "type": "lane", "kind": "fail", "t": time.time(),
                "lane": 0, "cc": "", "ip": "",
                "msg": f"at the {_MAX_CONNS}-connection cap -- refused one so "
                       f"the rest keep their lanes "
                       f"(LINGLING_MAX_CONNS)",
            })
            await self._answer(writer, b"HTTP/1.1 503 Service Unavailable\r\n"
                                       b"Content-Length: 0\r\n\r\n")
            writer.close()
            return
        try:
            if not await self._answer(
                    writer, b"HTTP/1.1 200 Connection Established\r\n\r\n"):
                self._slot_exit()
                return
            transport_sock = writer.get_extra_info("socket")
            if transport_sock is None:
                self._slot_exit()
                writer.close()
                return
            # pause reads
            transport = writer.transport
            try:
                transport.pause_reading()
            except (AttributeError, NotImplementedError):
                pass
            raw = transport_sock.dup()
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
        except BaseException:
            self._slot_exit()
            raise

        from . import mitm

        def _serve_conn() -> None:
            try:
                mitm.handle_conn(raw, host, port, seq, self.cert_shop,
                                 self._emit, self)
            finally:
                self._slot_exit()

        threading.Thread(target=_serve_conn, name=f"mitm-{seq}",
                         daemon=True).start()

    # lane picking
    def any_unlimited(self, exclude: set) -> bool:
        """True when some lane we have not tried can still serve.

        A 429 retires one exit, so retrying elsewhere is right -- until every
        remaining lane is limited too. Then one more attempt is a guaranteed 429
        and pure amplification. Measured over the log, and the two traffic
        classes differ sharply: the owner runs 668 attempts for 605 requests --
        1.10 each, with only 4% landing on an exit that had already refused.
        The soak harness, which fires bursts on purpose, runs 1.28 each with
        54% already refused. So the amplification this guards against is real
        but is a burst artefact, not normal traffic. A restarted lane has its
        deadline cleared, so a lane that is still cooking counts as available."""
        now = time.time()
        return any(l.index not in exclude and l.limited_until <= now
                   for l in self.tor.lanes)

    def pick_lane(self, exclude: Optional[set] = None) -> Optional[Lane]:
        """The least-loaded healthy lane, so a burst diverges evenly.

        ``active`` -- how many requests the lane is carrying right now -- is
        the first thing compared. That is the whole point: traffic spreads
        across the pool instead of stacking on one exit, and every healthy
        lane is forced to carry its share.

        There is deliberately no concurrency cap. A lane already carrying
        traffic still carries more. Holding a request while a healthy lane sat
        idle was worse than the load it was trying to avoid, and describing our
        own limit as the lane being "at cap" was simply untrue.

        The soft signals only break ties between equally-loaded lanes. A lane
        the far end has limited (429 with an hours-long `retry-after`) is a
        guaranteed 429, and a country that has been losing is the weaker bet.
        Neither is ever excluded -- the pool must not deadlock, and a 429 beats
        a stall. The older concentrated picker left the pool as good as one
        lane running at 4%; with this one, measured over the log, the owner's
        five lanes carry 22/19/17/22/20 percent and the soak's six carry
        20/19/19/15/16/11 -- as even as assignment can be."""
        candidates: List[Lane] = [
            l for l in self.tor.healthy_lanes()
            if not exclude or l.index not in exclude
        ]
        if not candidates:
            return None

        # thin pool
        strict = len(candidates) >= _LOSING_MIN_POOL
        now = time.time()

        def key(l: Lane):
            # server said
            limited = 1 if l.limited_until > now else 0
            # last resort
            losing = (1 if self.tor.score_of(l.exit_country) <= _LOSING
                      else 0) if strict else 0
            return (limited, l.active, losing, l.last_used_at)

        lane = min(candidates, key=key)
        lane.last_used_at = time.perf_counter_ns()
        # real request
        lane.last_real_at = time.time()
        return lane

    def report_refused(self, lane: Lane, status: int) -> None:
        """403 or 429 -- the far end refused this exit.

        Hand it to the health daemon, which owns the re-pin, so the exit that
        carried the request is the one charged for it."""
        # score it
        self.tor.note_result(lane.exit_country, status)
        if self.tor.limit_hook is not None:
            self.tor.limit_hook(lane, status)
            return
        # no daemon
        lane.healthy = False
        self._emit({
            "type": "lane", "kind": "limited", "t": time.time(),
            "lane": lane.index, "cc": lane.exit_country, "ip": lane.exit_ip,
            "msg": f"lane {lane.index} was refused ({status}) -- re-cooking it",
        })

    # connection handling
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
            # connect only
            try:
                while True:
                    h = await asyncio.wait_for(reader.readline(), timeout=5)
                    if h in (b"\r\n", b"\n", b""):
                        break
            except asyncio.TimeoutError:
                pass
            await self._answer(writer, b"HTTP/1.1 405 Method Not Allowed\r\n"
                               b"Content-Length: 0\r\n\r\n")
            writer.close()
            return

        host, _, port_s = target.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            writer.close()
            return
        try:
            while True:
                h = await asyncio.wait_for(reader.readline(), timeout=5)
                if h in (b"\r\n", b"\n", b""):
                    break
        except asyncio.TimeoutError:
            writer.close()
            return

        if self._should_mitm(host):
            await self._handle_mitm(host, port, seq, reader, writer)
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
                    upstream_r, upstream_w = await self._dial(
                        lane, host, port,
                        cred=netutil.slot_cred(lane.index, seq))
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
                    lane = None
            if lane is not None:
                break
            # never 502
            if time.time() >= deadline:
                break
            if not held:
                held = True
                self._emit({
                    "type": "lane", "kind": "fail", "t": time.time(),
                    "lane": 0, "cc": "", "ip": "",
                    "msg": f"no lane could reach {host} -- retrying",
                })
            tried.clear()
            await asyncio.sleep(0.5)
        if lane is None or upstream_w is None or upstream_r is None:
            await self._answer(writer, b"HTTP/1.1 502 Bad Gateway\r\n"
                               b"Content-Length: 0\r\n\r\n")
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
        answered = await self._answer(
            writer, b"HTTP/1.1 200 Connection Established\r\n\r\n")
        try:
            if answered:
                await self._pipe(seq, lane, reader, writer,
                                 upstream_r, upstream_w)
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

    async def _dial(self, lane: Lane, host: str, port: int,
                    cred: Optional[tuple] = None
                    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """SOCKS5 CONNECT to (host, port) through the lane; atyp=0x03 so DNS
        resolves at the exit -- the exit IP must be the lane's, not ours.

        ``cred`` asks for username/password auth, which is what gives a lane
        more than one circuit: Tor isolates on the SOCKS username."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", lane.socks_port),
            timeout=self.dial_timeout)

        async def _io() -> asyncio.StreamWriter:
            if cred is None:
                writer.write(bytes([0x05, 0x01, 0x00]))
                await writer.drain()
                resp = await reader.readexactly(2)
                if resp[0] != 0x05 or resp[1] != 0x00:
                    raise ConnectionError("bad SOCKS5 greeting")
            else:
                # both methods
                writer.write(bytes([0x05, 0x02, 0x00, 0x02]))
                await writer.drain()
                resp = await reader.readexactly(2)
                if resp[0] != 0x05:
                    raise ConnectionError("bad SOCKS5 greeting")
                if resp[1] == 0x02:
                    user, password = cred
                    ub = user.encode("utf-8")[:255]
                    pb = password.encode("utf-8")[:255]
                    writer.write(bytes([0x01, len(ub)]) + ub
                                 + bytes([len(pb)]) + pb)
                    await writer.drain()
                    auth = await reader.readexactly(2)
                    if auth[0] != 0x01 or auth[1] != 0x00:
                        raise ConnectionError("SOCKS5 auth refused")
                elif resp[1] != 0x00:
                    raise ConnectionError("bad SOCKS5 greeting")
            addr = host.encode("idna")
            writer.write(bytes([0x05, 0x01, 0x00, 0x03, len(addr)])
                         + addr + struct.pack("!H", port))
            await writer.drain()
            head = await reader.readexactly(4)
            if head[1] != 0x00:
                raise ConnectionError(
                    netutil.SOCKS_REPLY_CODES.get(head[1], f"socks reply {head[1]}"))
            atyp = head[3]
            if atyp == 0x01:
                await reader.readexactly(4)
            elif atyp == 0x03:
                ln = (await reader.readexactly(1))[0]
                await reader.readexactly(ln)
            elif atyp == 0x04:
                await reader.readexactly(16)
            await reader.readexactly(2)
            return writer

        try:
            await asyncio.wait_for(_io(), timeout=self.dial_timeout)
        except Exception:
            writer.close()
            raise
        return reader, writer

    async def _pipe(self, seq: int, lane: Lane,
                    client_reader: asyncio.StreamReader,
                    client_writer: asyncio.StreamWriter,
                    upstream_reader: asyncio.StreamReader,
                    upstream_writer: asyncio.StreamWriter) -> None:
        """Shuttle bytes both ways until either side closes."""
        stats = {"up": 0, "down": 0}
        started = time.time()

        async def pump(r: asyncio.StreamReader, w: asyncio.StreamWriter,
                       key: str) -> None:
            try:
                while True:
                    data = await r.read(65536)
                    if not data:
                        break
                    stats[key] += len(data)
                    w.write(data)
                    await w.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                try:
                    w.close()
                except Exception:  # noqa: BLE001
                    pass

        async def ticker() -> None:
            last = 0
            while True:
                await asyncio.sleep(20)
                total = stats["up"] + stats["down"]
                if total == last:
                    continue
                last = total
                self._emit({
                    "type": "flow", "t": time.time(), "n": seq,
                    "lane": lane.index, "cc": lane.exit_country,
                    "kb": round(total / 1024, 1),
                })

        beat = asyncio.create_task(ticker())
        try:
            await asyncio.gather(
                pump(client_reader, upstream_writer, "up"),
                pump(upstream_reader, client_writer, "down"),
                return_exceptions=True,
            )
        finally:
            beat.cancel()
            self._emit({
                "type": "reqend", "t": time.time(), "n": seq,
                "lane": lane.index, "cc": lane.exit_country,
                "kb": round((stats["up"] + stats["down"]) / 1024, 1),
                "secs": round(time.time() - started, 1),
            })
