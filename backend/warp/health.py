"""Background health daemon for WARP egress proxies.

Periodically: TCP-checks every WARP SOCKS5 port, HTTP-probes working ones to
verify the tunnel, restarts dead wireproxy processes, regenerates identities
that can't be revived, removes unhealthy proxies from the pool, keeps a minimum
number healthy, and dumps identities with too many 429s or consecutive failures
(recycling burned IPs automatically).
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from typing import Any, Dict, List, Optional

from providers.proxy_pool import ProxyPool
from warp.manager import WarpManager, _identity_config_ok, _port_is_open


# ---------------------------------------------------------------------------
# Health thresholds
# ---------------------------------------------------------------------------
PORT_CHECK_TIMEOUT = 1.0          # seconds for TCP port check
HTTP_PROBE_TIMEOUT = 6.0          # seconds for full HTTP proxy probe (hits opencode.ai)
MAX_CONSECUTIVE_FAILURES = 10     # this many consecutive failures -> dump identity
MAX_429_TOTAL = 50                # this many lifetime 429s -> dump identity
# Floor on healthy proxies. A hardcoded 6 against LINGLING_WARP_COUNT=3 could
# never be met, so _ensure_min_healthy re-registered every identity each cycle
# forever, hammering Cloudflare's rate-limited account-creation endpoint.
MIN_HEALTHY_PROXIES = 6
CHECK_INTERVAL = 60               # seconds between health check cycles


def _socks5_http_probe(
    proxy_url: str,
    target_host: str = "opencode.ai",
    target_port: int = 443,
    timeout: float = HTTP_PROBE_TIMEOUT,
) -> bool:
    """Full HTTP CONNECT through a SOCKS5 proxy to verify the tunnel works.

    Uses opencode.ai as the probe target (the actual upstream). If the proxy
    can reach opencode.ai on port 443, it's healthy enough for routing.
    Returns True only if the SOCKS5 handshake + TCP connect succeed.
    """
    if not proxy_url.startswith("socks5://"):
        return False
    host_port = proxy_url[len("socks5://"):]
    proxy_host, port_str = host_port.rsplit(":", 1)
    proxy_port = int(port_str)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((proxy_host, proxy_port))
        # SOCKS5 greet: no authentication
        sock.sendall(bytes([0x05, 0x01, 0x00]))
        resp = sock.recv(2)
        if len(resp) != 2 or resp[0] != 0x05 or resp[1] != 0x00:
            return False

        # CONNECT request with a domain name
        addr = target_host.encode("ascii")
        req = (
            bytes([0x05, 0x01, 0x00, 0x03, len(addr)])
            + addr
            + struct.pack("!H", target_port)
        )
        sock.sendall(req)
        resp = sock.recv(10)
        if len(resp) < 2 or resp[0] != 0x05 or resp[1] != 0x00:
            return False

        # CONNECT succeeded — the tunnel is working
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass


class WarpHealthDaemon:
    """Background daemon that keeps WARP proxies healthy and auto-heals them.

    Starts a daemon thread on :meth:`start` that runs a full health check every
    ``check_interval`` seconds. During each cycle it:

    * Checks every WARP instance (port + identity + full HTTP probe)
    * Heals unhealthy instances (restart wireproxy → regenerate identity)
    * Syncs the proxy pool: only healthy proxies are in the pool
    * Dumps burned identities (too many 429s/consecutive failures) and creates fresh ones
    * Ensures at least ``min_healthy`` working proxies are always available
    """

    def __init__(
        self,
        warp_manager: WarpManager,
        proxy_pool: ProxyPool,
        check_interval: int = CHECK_INTERVAL,
        min_healthy: Optional[int] = None,
        log=None,
    ) -> None:
        self.warp = warp_manager
        self.pool = proxy_pool
        self.check_interval = check_interval
        # Never ask for more healthy exits than exist. With fewer identities than
        # the default floor, _ensure_min_healthy would regenerate every one of
        # them on every cycle and never reach its target.
        wanted = MIN_HEALTHY_PROXIES if min_healthy is None else min_healthy
        self.min_healthy = max(1, min(wanted, len(warp_manager.instances) or wanted))
        self.log = log or logging.getLogger("uvicorn.error").info
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # proxy id -> (consecutive_failures, total_429, total_requests), kept
        # across a remove/re-add so burn history is not silently reset.
        self._stats: Dict[str, tuple] = {}
        # First cycle warmup: skip HTTP probes and pool removal so the bootstrap
        # thread has time to register identities and start wireproxy without the
        # daemon's health checks immediately undoing its work.
        self._warmup = True

    def start(self) -> None:
        """Start the background health check thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="warp-health", daemon=True
        )
        self._thread.start()
        self.log(
            "[warp-health] daemon started (check every %ds, min %d healthy, probe target opencode.ai:443)",
            self.check_interval, self.min_healthy,
        )

    def stop(self) -> None:
        """Signal the daemon to stop and wait for it."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
            self.log("[warp-health] daemon stopped")

    def check_and_heal(self) -> Dict[str, Any]:
        """Run one full health check cycle synchronously and return results.

        This is the same method the background thread calls, exposed so callers
        can do an immediate health check (e.g. from the API).
        """
        return self._check_and_heal()

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------
    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._check_and_heal()
            except Exception as exc:  # noqa: BLE001
                self.log("[warp-health] cycle error: %s", exc)
            self._stop.wait(self.check_interval)

    def _check_and_heal(self) -> Dict[str, Any]:
        """One full health check cycle. Returns a results dict."""
        self.log("[warp-health] running health check cycle%s...",
                 " (warmup)" if self._warmup else "")

        # 1. Health-check every instance
        results: List[Dict[str, Any]] = []
        for inst in self.warp.instances:
            result = self._health_check(inst)
            results.append(result)

        healthy_count = sum(1 for r in results if r["healthy"])
        self.log("[warp-health] cycle: %d/%d instances healthy",
                 healthy_count, len(results))

        # 2. Heal unhealthy instances
        healed = 0
        for i, inst in enumerate(self.warp.instances):
            if results[i]["healthy"]:
                continue
            self.log("[warp-health] healing unhealthy instance #%d (port %d) ...",
                     inst.index, inst.port)
            try:
                self._heal_instance(inst)
                results[i] = self._health_check(inst)
                if results[i]["healthy"]:
                    healed += 1
                    self.log("[warp-health] instance #%d healed successfully", inst.index)
                else:
                    self.log("[warp-health] instance #%d still unhealthy after heal", inst.index)
            except Exception as exc:  # noqa: BLE001
                self.log("[warp-health] heal error for #%d: %s", inst.index, exc)

        # 3. Dump burned identities (too many failures/429s). Regeneration
        # replaces the identity wholesale, so the health snapshot taken in step 1
        # is stale for anything it touched -- re-check those before the pool sync
        # below decides who belongs in the pool. Without this the dumped exits
        # were removed and never re-added: `warp_manager.status()` reads live
        # state so "identities" stayed correct while the pool gauge sat at 0.
        dumped = self._dump_burned_identities()
        if dumped:
            for i, inst in enumerate(self.warp.instances):
                results[i] = self._health_check(inst)
            healthy_count = sum(1 for r in results if r["healthy"])

        # 4. Sync pool: only healthy proxies
        added, removed = self._sync_pool(results)

        # 5. Ensure minimum healthy count
        regenerated = 0
        if healthy_count < self.min_healthy:
            self.log("[warp-health] only %d healthy, need %d — regenerating more",
                     healthy_count, self.min_healthy)
            regenerated = self._ensure_min_healthy(results)
            # Re-check the regenerated ones
            for i, inst in enumerate(self.warp.instances):
                if not results[i]["healthy"]:
                    results[i] = self._health_check(inst)
            healthy_count = sum(1 for r in results if r["healthy"])
            self._sync_pool(results)  # sync again after regeneration

        pool_status = self.pool.status()
        self.log(
            "[warp-health] cycle done: %d healthy, %d healed, %d dumped, "
            "%d regenerated, pool: %d/%d available",
            healthy_count, healed, dumped, regenerated,
            pool_status.get("available", 0), pool_status.get("total", 0),
        )

        # First cycle done — subsequent cycles will run full HTTP probes and
        # allow pool removal so truly dead proxies are evicted.
        self._warmup = False

        return {
            "healthy": healthy_count,
            "total": len(results),
            "healed": healed,
            "dumped": dumped,
            "regenerated": regenerated,
            "pool": pool_status,
            "instances": results,
        }

    # ------------------------------------------------------------------
    # Health checks
    # ------------------------------------------------------------------
    def _health_check(self, inst: Any) -> Dict[str, Any]:
        """Check one WARP instance. Returns a result dict."""
        pid = f"warp-{inst.index}"
        has_identity = bool(inst.private_key) and _identity_config_ok(inst)
        port_open = _port_is_open("127.0.0.1", inst.port, timeout=PORT_CHECK_TIMEOUT)

        # Thread-safe access to proxy pool stats
        proxy_in_pool = self.pool.get_by_id(pid)

        consecutive_fails = proxy_in_pool.consecutive_failures if proxy_in_pool else 0
        total_429s = proxy_in_pool.total_429 if proxy_in_pool else 0
        pool_healthy = consecutive_fails < MAX_CONSECUTIVE_FAILURES

        # Full HTTP probe through the SOCKS5 tunnel. Run whenever the port is
        # open, not only for proxies already in the pool: gating on membership
        # meant a fresh instance was admitted on "config exists + port listening"
        # alone, so a bound port with a dead tunnel routed real traffic for a full
        # cycle before the first probe ever ran.
        #
        # During the warmup cycle (first run after startup), skip the probe so
        # the bootstrap thread's freshly-started wireproxy isn't yanked from
        # the pool before its WARP tunnel has had time to establish.
        http_ok: Optional[bool] = None
        if has_identity and port_open and not self._warmup:
            try:
                http_ok = _socks5_http_probe(
                    inst.proxy_url, timeout=HTTP_PROBE_TIMEOUT
                )
            except Exception:  # noqa: BLE001
                http_ok = False

        healthy = (
            has_identity
            and port_open
            and pool_healthy
        )
        # If we ran the HTTP probe, it must also pass
        if http_ok is not None:
            healthy = healthy and http_ok

        return {
            "index": inst.index,
            "port": inst.port,
            "pid": pid,
            "has_identity": has_identity,
            "port_open": port_open,
            "http_probe_ok": http_ok,
            "consecutive_failures": consecutive_fails,
            "total_429": total_429s,
            "pool_healthy": pool_healthy,
            "in_pool": proxy_in_pool is not None,
            "healthy": healthy,
        }

    # ------------------------------------------------------------------
    # Healing actions
    # ------------------------------------------------------------------
    def _heal_instance(self, inst: Any) -> None:
        """Attempt to heal a broken instance — restart wireproxy, then regenerate if needed."""
        # Step 1: Try restarting wireproxy
        self.log("[warp-health] restarting wireproxy for #%d (port %d)...",
                 inst.index, inst.port)
        try:
            self.warp.restart_instance(inst)
            time.sleep(2)  # give it time to bring up the tunnel
            if _port_is_open("127.0.0.1", inst.port, timeout=2.0):
                self.log("[warp-health] restart successful for #%d", inst.index)
                return
        except Exception as exc:  # noqa: BLE001
            self.log("[warp-health] restart failed for #%d: %s", inst.index, exc)

        # Step 2: If restart didn't work or identity is broken, regenerate fully
        if not _identity_config_ok(inst) or not _port_is_open("127.0.0.1", inst.port, timeout=1.0):
            self.log("[warp-health] fully regenerating identity #%d ...", inst.index)
            try:
                self.warp.regenerate_instance(inst)
                time.sleep(2)
            except Exception as exc:  # noqa: BLE001
                self.log("[warp-health] regeneration failed for #%d: %s", inst.index, exc)
                raise

    def _remember_stats(self, proxy_id: str, px: Any) -> None:
        """Preserve a removed proxy's burn counters, keyed by id.

        `_sync_pool` drops an unhealthy proxy and re-adds it when it recovers, and
        `add()` builds a fresh Proxy with zeroed counters. A flapping exit
        therefore reset its 429 tally every cycle and could never reach
        MAX_429_TOTAL, so `_dump_burned_identities` never fired for exactly the
        proxies that most needed recycling.
        """
        self._stats[proxy_id] = (px.consecutive_failures, px.total_429, px.total_requests)

    def _restore_stats(self, proxy_id: str, px: Any) -> None:
        """Re-apply remembered counters to a proxy that just rejoined the pool."""
        remembered = self._stats.get(proxy_id)
        if remembered is None:
            return
        px.consecutive_failures, px.total_429, px.total_requests = remembered

    def _sync_pool(self, results: List[Dict[str, Any]]) -> tuple:
        """Sync proxy pool: add healthy proxies, remove unhealthy ones.

        During the warmup cycle the daemon only adds — never removes — so
        freshly-started wireproxy survives until its WARP tunnel establishes.
        Returns (added_count, removed_count).
        """
        added = 0
        removed = 0
        for i, inst in enumerate(self.warp.instances):
            pid = f"warp-{inst.index}"
            is_healthy = results[i]["healthy"]
            existing = self.pool.get_by_id(pid)

            if is_healthy and existing is None:
                fresh = self.pool.add(
                    inst.proxy_url,
                    proxy_id=pid,
                    label=f"WARP identity #{inst.index}",
                )
                # Carry the burn history across the gap, so a flapping exit still
                # accumulates towards being dumped.
                self._restore_stats(pid, fresh)
                added += 1
            elif not is_healthy and existing is not None:
                if self._warmup:
                    # During warmup the port may be open but the tunnel isn't
                    # ready yet — keep the proxy in the pool and let the next
                    # cycle re-evaluate with a full HTTP probe.
                    continue
                self._remember_stats(pid, existing)
                self.pool.remove(pid)
                removed += 1
            elif is_healthy and existing is not None:
                # The port can change under a migration. Written through the pool
                # so the mutation happens under its lock -- the executor reads
                # this field to build an httpx client.
                self.pool.set_url(pid, inst.proxy_url)
        return added, removed

    def _ensure_min_healthy(self, results: List[Dict[str, Any]]) -> int:
        """Regenerate unhealthy instances until we meet min_healthy.

        Returns the number of instances regenerated.
        """
        healthy_count = sum(1 for r in results if r["healthy"])
        need = self.min_healthy - healthy_count
        regenerated = 0

        for i, inst in enumerate(self.warp.instances):
            if need <= 0:
                break
            if not results[i]["healthy"]:
                try:
                    self.log("[warp-health] ensure-min: regenerating #%d ...", inst.index)
                    self.warp.regenerate_instance(inst)
                    time.sleep(2)
                    regenerated += 1
                    need -= 1
                except Exception as exc:  # noqa: BLE001
                    self.log("[warp-health] ensure-min heal failed for #%d: %s",
                             inst.index, exc)
        return regenerated

    def _dump_burned_identities(self) -> int:
        """Check pool proxies for too many 429s or consecutive failures.

        Dumps burned identities and regenerates fresh ones.
        Returns the number of identities dumped.
        """
        dumped = 0
        # Thread-safe snapshot of proxies
        all_proxies = self.pool.get_all_proxies()
        for px in all_proxies:
            if not px.id.startswith("warp-"):
                continue
            try:
                idx = int(px.id.split("-")[1])
            except (ValueError, IndexError):
                continue
            inst = next((i for i in self.warp.instances if i.index == idx), None)
            if inst is None:
                continue

            should_dump = False
            reason = ""
            if px.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                reason = f"{px.consecutive_failures} consecutive failures"
                should_dump = True
            if px.total_429 >= MAX_429_TOTAL:
                reason = f"{px.total_429} total 429s"
                should_dump = True

            if should_dump:
                self.log("[warp-health] dumping identity #%d (%s) — regenerating...",
                         idx, reason)
                self.pool.remove(px.id)
                try:
                    self.warp.regenerate_instance(inst)
                    self.log("[warp-health] fresh identity #%d created", idx)
                    dumped += 1
                except Exception as exc:  # noqa: BLE001
                    self.log("[warp-health] regeneration after dump failed for #%d: %s",
                             idx, exc)
        return dumped
