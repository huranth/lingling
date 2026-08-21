"""Background health daemon for the egress pool.

Every ``check_interval`` seconds: TCP- and HTTP-probe each WARP tunnel,
restart dead wireproxy processes, regenerate identities that cannot be
revived, keep the pool in sync with reality, and enforce a healthy minimum.
Burned identities (too many 429s or consecutive failures) are healed by
re-rolling their tunnel onto a fresh exit rather than re-registering.

On its own cadence (``probe_interval``) the daemon additionally runs the
real-model probe plus all healers — the SOCKS5 checks above cannot see
429s — plus lane spreading and rotation of burned Tor lanes.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from core import config
from providers.proxy_pool import ProxyPool
from providers import active_streams
from routing import sampler
from warp import probe as warp_probe
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
    """Full SOCKS5 CONNECT through a proxy to verify the tunnel works.

    Uses opencode.ai as the probe target (the actual upstream). If the proxy
    can reach opencode.ai on port 443, it's healthy enough for routing.
    Delegates to warp.probe.socks5_connect_check — the same raw handshake the
    probe pre-flight uses, so there is exactly one implementation of it.
    """
    return warp_probe.socks5_connect_check(
        proxy_url, target_host, target_port, timeout,
    ) == ""


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
        probe_interval: Optional[float] = None,
        tor_manager: Optional[Any] = None,
        catalog: Optional[Any] = None,
        log=None,
    ) -> None:
        self.warp = warp_manager
        self.pool = proxy_pool
        # Tor lanes share the pool but heal differently: no identity, no
        # tunnel re-roll — a process restart re-picks their exit route.
        self.tor = tor_manager
        # Catalog (the live model list) picks the real-model probe target.
        # Without it the probe pins one model id; when OpenCode gates that id
        # behind a key every lane reads probe_error and the whole pool looks
        # sick while actually fine. resolve_probe_model prefers a currently-
        # free non-reasoning model over the configured pin.
        self.catalog = catalog
        self.check_interval = check_interval
        # Real-model probe cadence (seconds). The per-cycle SOCKS5 probe cannot
        # see rate limits — an exit OpenCode has 429'd still CONNECTs fine — so
        # the daemon periodically re-runs the startup probe and healers.
        self.probe_interval = (
            config.PROBE_INTERVAL_S if probe_interval is None else probe_interval
        )
        self._next_probe_at = 0.0  # due as soon as warmup is over
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
        # Serialises probe+heal runs: the periodic loop and POST /api/warp/probe
        # share one daemon, and two overlapping runs would race to regenerate
        # the same identities.
        self._probe_lock = threading.Lock()
        # Same for health cycles: a probe's post-heal re-sync must not overlap
        # the loop's regular cycle, or both could restart the same instance.
        self._cycle_lock = threading.Lock()
        # First cycle warmup: skip HTTP probes and pool removal so the bootstrap
        # thread has time to register identities and start wireproxy without the
        # daemon's health checks immediately undoing its work.
        self._warmup = True

    def start(self) -> None:
        """Start the background health check thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        # First periodic probe waits a full interval — when the bootstrap ran
        # the startup probe moments earlier, an immediate re-probe is a
        # duplicate cost with no new information.
        self._next_probe_at = time.time() + self.probe_interval
        self._thread = threading.Thread(
            target=self._run_loop, name="warp-health", daemon=True
        )
        self._thread.start()
        self.log(
            "[warp-health] daemon up — checking every %ds, keeping %d healthy, poking opencode.ai:443",
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
            # After the warmup cycle, run the real-model probe + healers on
            # their own cadence. The SOCKS5 check above is blind to 429s, so
            # without this a rate limit acquired mid-session would sit in the
            # pool until the next restart.
            if (
                self.probe_interval > 0
                and not self._warmup
                and time.time() >= self._next_probe_at
            ):
                try:
                    self.probe_now()
                except Exception as exc:  # noqa: BLE001
                    self.log("[warp-health] probe cycle error: %s", exc)
                self._next_probe_at = time.time() + self.probe_interval
            self._stop.wait(self.check_interval)

    def probe_now(self) -> Optional[Any]:
        """Run one probe+heal+verify pass immediately.

        Returns the ProbeSummary, or None when a pass is already running
        (another caller holds the probe lock). Both the background loop and
        the dashboard's Probe button come through here, so healing the same
        identity twice concurrently is impossible.
        """
        if not self._probe_lock.acquire(blocking=False):
            self.log("[warp-health] probe already running — skipping")
            return None
        try:
            return self._probe_and_heal_burned_exits()
        finally:
            self._probe_lock.release()

    def formation_now(self) -> Optional[Dict[str, Any]]:
        """Assemble distinct exit lanes now. None when the lock is held.

        Shares the probe lock: formation restarts tunnels, and racing a heal
        or probe pass would fight over the same slots.
        """
        from warp import formation
        if not self._probe_lock.acquire(blocking=False):
            self.log("[warp-health] probe already running — formation skipped")
            return None
        try:
            result = formation.form_distinct_exits(
                self.pool, self.warp, log=self.log,
            )
            # Lanes changed hands: re-sync the pool right away so routing
            # sees the new spread immediately.
            try:
                self._check_and_heal()
            except Exception as exc:  # noqa: BLE001
                self.log("[warp-health] post-formation cycle error: %s", exc)
            return result
        finally:
            self._probe_lock.release()

    def _probe_and_heal_burned_exits(self) -> Optional[Any]:
        """Real-model probe of every exit, then run both healers on the results.

        Same probe + heal_expired + heal_rate_limited sequence as startup, plus
        a verification pass: each regenerated identity gets a fresh request to
        confirm its new exit IP is actually clean before a normal cycle re-syncs
        the pool. probe_all also refreshes /api/warp/probe for the dashboard.
        Returns the ProbeSummary, or None when there is nothing to probe.
        """
        proxies = self.pool.get_all_proxies()
        warp_proxies = [p for p in proxies if p.id.startswith("warp-")]
        if not warp_proxies:
            return None
        self.log(
            "[warp-health] real-model probe: testing %d exits ...",
            len(warp_proxies),
        )
        # Hand the catalog to probe_all so it both resolves a currently-serving
        # free model (not the hardcoded pin, which can 400 when OpenCode gates it)
        # AND converges: a model rejected on every lane is retired and the pass
        # is retried with the next free model, instead of leaving every lane read
        # probe_error while the egress is fine.
        summary = warp_probe.probe_all(
            self.pool, catalog=self.catalog, log=self.log,
        )
        healed = warp_probe.heal_rate_limited(
            self.pool, summary, self.warp, log=self.log,
        )
        healed += warp_probe.heal_expired(
            self.pool, summary, self.warp, log=self.log,
        )
        # Spread duplicated slots across the exits nobody is using, so the
        # pool's capacity is distinct exits rather than one shared lane.
        moved = warp_probe.spread_distinct_exits(
            self.pool, summary, self.warp, log=self.log,
        )
        rotated = self._rotate_burned_tor_lanes(summary)
        if healed or moved or rotated:
            # Re-sync immediately so freshly re-rolled exits rejoin the pool
            # instead of waiting out the next cycle. (The rate-limit healer
            # verifies each new exit with a real request as part of its roll
            # loop, so no separate verification pass is needed here.)
            try:
                self._check_and_heal()
            except Exception as exc:  # noqa: BLE001
                self.log("[warp-health] post-heal cycle error: %s", exc)
        # Post-heal sampler: re-sample the good models across the freshly-healed
        # green pool so per-(model, exit) routing stays current between startups.
        # Reuses this cycle's canary summary (no second canary probe) and is
        # gated by SAMPLER_INTERVAL_S so a fast heal cadence does not re-sample
        # OpenCode every cycle.
        if sampler.should_run():
            try:
                sampler.sample_models(
                    self.pool, self.catalog, summary, log=self.log,
                )
            except Exception as exc:  # noqa: BLE001
                self.log("[warp-health] sampler pass failed: %s", exc)
        return summary

    def _rotate_burned_tor_lanes(self, summary: Any) -> int:
        """Restart Tor lanes whose exit the probe found rate-limited or dead.

        Thin wrapper around :func:`warp.probe.rotate_burned_tor_lanes` so the
        daemon's call site (and existing tests) keep working; the real logic
        lives next to ``heal_expired`` / ``heal_rate_limited`` so the startup
        path can share it without depending on a constructed daemon.
        """
        return warp_probe.rotate_burned_tor_lanes(
            self.pool, self.tor, summary, log=self.log,
        )

    def _check_and_heal(self) -> Dict[str, Any]:
        """One full health check cycle. Returns a results dict.

        Only one cycle runs at a time: the loop's regular tick, a probe's
        post-heal re-sync, and an API-triggered check all mutate instances and
        the pool, and overlapping cycles would restart the same wireproxy or
        remove/re-add the same proxy concurrently. A contending caller just
        skips — the next cycle covers whatever it would have done.
        """
        if not self._cycle_lock.acquire(blocking=False):
            self.log("[warp-health] cycle already running — skipping")
            return {"skipped": True, "pool": self.pool.status()}
        try:
            return self._run_cycle()
        finally:
            self._cycle_lock.release()

    def _run_cycle(self) -> Dict[str, Any]:
        """Cycle body; caller holds ``_cycle_lock``."""
        self.log("[warp-health] poking the exits (cycle%s)...",
                 " (warmup)" if self._warmup else "")

        # 1. Health-check every instance
        results: List[Dict[str, Any]] = []
        for inst in self.warp.instances:
            result = self._health_check(inst)
            results.append(result)

        healthy_count = sum(1 for r in results if r["healthy"])
        self.log("[warp-health] cycle: %d/%d instances breathing",
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
        if healed:
            # Step 5 decides on this count; without the recompute a cycle that
            # healed everyone still logged "only 0 healthy — regenerating more"
            # and ran a no-op ensure-min pass off the pre-heal snapshot.
            healthy_count = sum(1 for r in results if r["healthy"])

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
            self.log("[warp-health] only %d healthy, need %d — time to mint some more",
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
            "%d regenerated, pool %d/%d — breathing on their own.",
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
        # A live httpx stream rides the wireproxy's SOCKS5 listener; restarting
        # the process sends a graceful FIN that reads as "upstream closed before
        # completing" once the stream is past its first byte (see stream_guard).
        # Defer until the stream drains instead of killing it under the request.
        if config.DEFER_REROLL_WHEN_BUSY and active_streams.active(f"warp-{inst.index}") > 0:
            self.log("[warp-health] #%d re-roll deferred -- stream in flight", inst.index)
            return
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
                if config.DEFER_REROLL_WHEN_BUSY and active_streams.active(f"warp-{inst.index}") > 0:
                    self.log("[warp-health] ensure-min: #%d regeneration deferred -- stream in flight",
                             inst.index)
                    continue
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

        The burn lives on the tunnel's exit IP, not the identity, so the fix
        is to re-roll the tunnel onto a fresh exit (cheap, no re-registration)
        and zero the counters that no longer describe the new address. Only a
        tunnel that fails to come back up is removed from the pool, where the
        dead-tunnel path regenerates it wholesale.
        Returns the number of identities re-rolled.
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
                if config.DEFER_REROLL_WHEN_BUSY and active_streams.active(px.id) > 0:
                    self.log("[warp-health] #%d burned (%s) -- re-roll deferred, stream in flight",
                             idx, reason)
                    continue
                self.log("[warp-health] #%d burned (%s) — re-rolling its tunnel ...",
                         idx, reason)
                if self.warp.re_roll_tunnel(inst, log=self.log) is not None:
                    self.pool.reset_counters(px.id)
                    self.log("[warp-health] #%d re-rolled onto a fresh exit", idx)
                    dumped += 1
                else:
                    # Tunnel would not come back: drop it so the dead-tunnel
                    # path regenerates the identity wholesale.
                    self.pool.remove(px.id)
                    self.log("[warp-health] #%d re-roll failed — removed for regeneration", idx)
        return dumped
