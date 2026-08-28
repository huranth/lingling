"""Background health daemon for Tor egress lanes.

A daemon thread runs
a full check every ``check_interval`` seconds, healing dead lanes and syncing
the shared ProxyPool so only healthy lanes serve traffic. Lanes enter the pool
under ``tor-N`` ids with ``"Tor lane #N {cc}""`` labels.

The probe is two checks, not one:

1. OpenCode reachability -- a raw SOCKS5 CONNECT to opencode.ai:443
   (``core.egress_helpers._socks5_http_probe``). A lane that OpenCode has
   Tor-blocked cools immediately and the pool falls back to a healthy lane. No
   duplicated probe code; the only concession is normalising ``socks5h://`` to
   ``socks5://`` on the call (the helper predates the ``h`` form).
2. An IsTor probe -- ``https://check.torproject.org/api/ip`` through the lane
   returns ``{"IsTor": true, "IP": ...}`` *and* the real distinct exit IP,
   which is what the dashboard shows so "5 lanes" truthfully reports 5 IPs.

Lane is unhealthy when the SOCKS port dies, the control port dies (we'd lose
NEWNYM/rebuild), there's no recent circuit build, or either probe fails.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from providers.proxy_pool import ProxyPool
from core.egress_helpers import (
    MAX_429_TOTAL,
    MAX_CONSECUTIVE_FAILURES,
    PORT_CHECK_TIMEOUT,
    _socks5_http_probe,
    _port_is_open,
)

from tor.manager import TorLane, TorManager


# Tor's extra hops make every probe slower. The OpenCode probe
# through the SOCKS5 CONNECT handshake clears in a couple of seconds on a
# warm lane; the IsTor probe goes all the way to a TLS GET, so it gets a more
# generous budget. Both are bounded so a stuck lane doesn't drag the cycle.
TOR_HTTP_PROBE_TIMEOUT = 10.0
TOR_ISTOR_PROBE_TIMEOUT = 15.0
# How many cycles a Tor lane may sit at "post-heal still failing while the
# SOCKS port reopened" before the loop escalation falls through to a wholesale
# ``regenerate_lane`` (DataDirectory wipe + consensus re-fetch + circuit
# rebuild). Mirrors ``_ESCALATE_AFTER_RESTARTS`` from the daemon's own design:
# two cheap-restart chances to clear a transient quirk, then escalate to a
# wholesale re-roll. Tor's regenerate is heavy (full guard
# consensus + first circuit build, 30-90s each) so the cooldown below is
# correspondingly larger than the cheap-restart streak, but the streak is
# kept at 3 so the operator's "how many cycles
# until the daemon escalates" mental model is the same on both families.
_TOR_ESCALATE_AFTER_RESTARTS = 3
# Cooldown between consecutive ``regenerate_lane`` calls on the *same* lane.
# A persistent block (OpenCode poisoning every exit in a country, or every
# guard consensus rejecting us) would otherwise re-roll Tor's slow bootstrap
# at one per 3 cycles = ~once per 3 minutes -- each regenerate eats 30-90s of
# the lane's lifetime, and re-rolling faster than circuits can stabilise just
# moves the lane into perpetual boot. Sized at 2x ``MaxCircuitDirtiness``
# (= ``MaxCircuitDirtiness`` * 2 = 1200s) and intentionally in lockstep with
# ``TOR_STUCK_CIRCUIT_S``: one full Dirtiness interval of a fresh circuit
# is the unit of time the network takes to settle, so that's the minimum
# window a fresh identity deserves before the daemon rolls it again.
_TOR_REGENERATE_COOLDOWN_S = 1200.0
MAX_REGEN_FAILURES = 3  # circuit-breaker cap on consecutive failed wholesale regenerates before sideline
_SIDELINED_RECHECK_S = 6000.0  # 100 min -- 10x the regenerate cooldown
# Fast-fail cap on consecutive daemon cycles in which a lane's
# ``_health_check`` returned ``healthy=False``. Without this, a lane whose
# ExitNodes country has been Cloudflare/OpenCode-banned sits in the heal
# ladder's natural churn for ~3 cheap-restart cycles + 1st-regenerate
# (30-90s) + 1200 s regen cooldown + 2 more regens = ~43 minutes before
# ``MAX_REGEN_FAILURES`` finally sidelines it. ``unhealthy_cycles`` is
# bumped every time ``_check_and_heal``'s step-1 snapshot finds the lane
# unhealthy and reset only on a passing probe -- so the count
# monotonically tracks "how long has this lane been broken", even across
# intermediate ``regenerate_lane`` re-rolls which don't change the exit
# blockage. Past the cap the lane is sidelined through the same path
# (``MAX_REGEN_FAILURES`` tribunal) so the existing
# ``_maybe_unsideline_stale`` revival handles its comeback without a new
# code path. Sized at 4 cycles so the wall-clock fast-fail is ~4 minutes
# against the default 60 s ``check_interval``; configurable via env if
# your Tor network path is significantly slower (mobile/high-latency
# Tor exits need a wider cap). Wall-clock equivalence of the default =
# ``_FAST_FAIL_PROBING_CYCLES * check_interval = 4 * 60 = 240 s``.
_FAST_FAIL_PROBING_CYCLES = int(os.getenv("LINGLING_TOR_FAST_FAIL_PROBING_CYCLES", "4") or 4)
# Destination-side burn detector. Distinct from the
# stuck-probing fast-fail above: that branch watches the lane's own
# ``_health_check`` probe (SOCKS/Circuit reachability to check.torproject.org)
# give up; this branch watches the pool ledger and asks "is the lane
# *itself* healthy even though every real egress attempt through it gets
# 429-throttled by the destination?". When the answer is yes the heal
# ladder's existing regenerate-within-country is wasted work -- the
# destination has the country persona rate-limited, not one exit IP, so we
# rotate ``lane.exit_country`` to an unused country from
# ``TorManager.countries`` and regenerate. Tunable via env so operators
# on slower upstreams (where 6 consecutive 429s is normal hiccup) can
# widen the floor.
_DEST_BURN_CONSEC = int(os.getenv("LINGLING_TOR_DEST_BURN_CONSEC", "6") or 6)
_DEST_BURN_TOTAL_429_MIN = int(os.getenv("LINGLING_TOR_DEST_BURN_429_MIN", "6") or 6)
# Fraction of total_requests that must be 429 to count as "burned at the
# destination" rather than "unlucky timing". 0.5 by default; raise this
# if your egress sees a lot of periodic 429 batches that recover on their
# own (e.g. quota windows that lift every few minutes), otherwise the
# rotate path will fire on those and cycle the country on every cooldown.
_DEST_BURN_429_RATIO = float(os.getenv("LINGLING_TOR_DEST_BURN_429_RATIO", "0.5") or 0.5)
# Minimum total_requests before the burn verdict is meaningful -- the lane
# has to have actually carried some traffic, not just had a couple of
# fresh-consensus probes through it. 4 is the floor: at least 4 requests
# through Tor before we call the verdict.
_DEST_BURN_TOTAL_MIN = int(os.getenv("LINGLING_TOR_DEST_BURN_TOTAL_MIN", "4") or 4)
# Cooldown between successive destination-burn rotations on the same lane,
# so a persistent block can't re-burn Tor's slow circuit bootstrap faster
# than once per this window. ``_TOR_REGENERATE_COOLDOWN_S`` (1200 s) is
# too wide for this case (a per-country burn that lasts the cooldown is
# better sidelined than re-attempted), so the rotate path uses its own
# narrower cooldown; default 10 minutes.
_DEST_BURN_ROTATE_COOLDOWN_S = float(
    os.getenv("LINGLING_TOR_DEST_BURN_ROTATE_COOLDOWN_S", "600") or 600.0
)
# A lane that hasn't built a circuit in this window is "up but stuck" --
# the SOCKS port is open, tor is alive, but nothing's flowing. Restart it.
# Sized at 2x ``MaxCircuitDirtiness`` (1200 = 2 x 600) on purpose. With the
# restored 600s circuit lifespan a healthy lane may legitimately show no
# new CIRC BUILT event for nearly a full Dirtiness interval -- existing
# streams just keep using the long-lived general circuit -- so the previous
# 180s threshold fired a false "stuck" heal on every healthy-but-quiet lane
# each cycle, one flap per lane per minute. 2x gives one full Dirtiness
# window of grace (a lane that would have built a fresh circuit in normal
# rotation still reads healthy) before we declare truly wedged: no BUILT in
# more than two windows means tor really is stuck, not just conserving its
# current circuit. Keep this in lockstep with tor/manager.py's
# ``MaxCircuitDirtiness`` -- changing either without the other reintroduces
# the flap.
TOR_STUCK_CIRCUIT_S = 1200.0
# Anti-sync padding between consecutive heal invocations (``restart_lane`` /
# ``regenerate_lane``) within one health cycle. The classic co-flunk: a 429
# storm on a fresh exit IP drops every lane on the affected country at the
# same wall-clock second, and the heal loop iterates serially -- so left to
# itself each ``_heal_lane`` call kicks off a different tor.exe milliseconds
# after the previous one. Every lane then tears down its current circuits
# and starts bootstrapping at the same instant; the ProxyPool reads ``0/N``
# for the entire 30-90s rebuild window -- the "only 0 cooking, need 3"
# alarm. Spacing consecutive heals apart prevents the reboot-storm from
# being a circuit-rebuild-storm: as soon as lane N+1 starts its takeover,
# lanes #1..N are already mid-rebuild and starting to surface in the pool.
# Sized just aggressive enough that a 5-lane cohort spreads across several
# seconds and gentle enough that an extra ~3-5s of pre-stagger padding is
# in the noise next to a single ``regenerate_lane`` (30-90s of consensus
# re-fetch + first circuit build). Capped at MAX so a 20-lane cohort does
# not grow without bound -- past MAX each further heal just stacks.
_LANE_HEAL_STAGGER_BASE_S = 0.5
_LANE_HEAL_STAGGER_PER_LANE_S = 0.3
_LANE_HEAL_STAGGER_MAX_S = 1.5


def _lane_heal_stagger_s(heal_pos: int) -> float:
    """Seconds to sleep before the (heal_pos+1)th heal invocation in a cycle.

    Mirrors the ``min(MAX, BASE + pos * PER)`` shape from the spec. ``pos=0``
    is the first heal of the cycle (no wait); subsequent heals grow the gap
    so tor.exe's circuit-rebuild storm doesn't pin the entire cohort offline
    simultaneously.
    """
    return min(
        _LANE_HEAL_STAGGER_MAX_S,
        _LANE_HEAL_STAGGER_BASE_S + heal_pos * _LANE_HEAL_STAGGER_PER_LANE_S,
    )


def _probe_url(proxy_url: str) -> str:
    """Normalise socks5h:// -> socks5:// for the SOCKS5 probe helper.

    The helper (reused from core.egress_helpers) gates on the legacy
    ``socks5://`` prefix; tor lanes register as ``socks5h://`` for remote DNS
    over the tunnel (the form torproject recommends). Stripping the ``h`` here
    is purely cosmetic for the probe: it encodes the target hostname as a
    SOCKS5 DOMAIN request (atyp=0x03), which is remote DNS regardless.
    """
    if proxy_url.startswith("socks5h://"):
        return "socks5://" + proxy_url[len("socks5h://"):]
    return proxy_url


def _socks5h_is_tor_probe(
    proxy_url: str, timeout: float = TOR_ISTOR_PROBE_TIMEOUT,
) -> Tuple[bool, str]:
    """GET check.torproject.org/api/ip through the lane. Returns (is_tor, ip).

    Uses httpx with ``socks5h://`` proxying (httpx[socks] + socksio are
    already project deps). The endpoint replies
    ``{"IsTor": true, "IP": "<exit ipv4>"}`` when the request leaves through a
    Tor exit; ``IsTor`` is false otherwise. We use it both as a "is this
    really a Tor exit" check and to capture the distinct exit IP the lane is
    presenting to the world (surfaced in the dashboard).
    """
    try:
        import httpx
    except ImportError:
        return False, ""
    try:
        with httpx.Client(proxy=proxy_url, timeout=timeout) as client:
            r = client.get("https://check.torproject.org/api/ip")
            if r.status_code != 200:
                return False, ""
            obj = r.json()
            return bool(obj.get("IsTor")), str(obj.get("IP") or "")
    except Exception:  # noqa: BLE001
        return False, ""


class TorHealthDaemon:
    """Background daemon that keeps Tor lanes healthy and auto-heals them.

    Loop shape: every cycle, health-check every
    lane, heal unhealthy ones, dump burned lanes (too many 429s / consecutive
    failures) by regenerating them, sync the pool to only-healthy lanes, and
    ensure a minimum healthy count.

    The first cycle is a *warmup*: it skips probes and pool removal so the
    bootstrap thread's freshly-launched tor.exe instances aren't yanked from
    the pool before their circuits establish (tor's first build is slow,
    especially on a cold first build).
    """

    def __init__(
        self,
        tor_manager: TorManager,
        proxy_pool: ProxyPool,
        check_interval: int = 60,
        min_healthy: Optional[int] = None,
        log=None,
    ) -> None:
        self.tor = tor_manager
        self.pool = proxy_pool
        self.check_interval = check_interval
        # Floor on healthy lanes, never asking for more than exist (mirrors
        # a clamp: otherwise every cycle would regenerate every lane).
        wanted = 3 if min_healthy is None else min_healthy
        self.min_healthy = max(1, min(wanted, len(tor_manager.lanes) or wanted))
        # ``self.log`` keeps the uvicorn.error.info callable the daemon spoke
        # through from day one. ``self._log`` adds the full Logger so the loop
        # has a .debug tier for per-cycle idle chatter ("cycle running", "N/N
        # healthy", clean-cycle done -- flooding INFO once a minute per lane)
        # and a .warning tier for rare caught-at-top-level failures (a
        # transient flap no longer looks like routine INFO noise). State-change
        # events stay at ``self.log`` / INFO.
        # one-to-one. Tests assert on behaviour, not log strings, so the tier
        # reshuffle + voice change is a noop for them.
        self.log = log or logging.getLogger("uvicorn.error").info
        self._log = logging.getLogger("uvicorn.error")
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # Burn history keyed by proxy id, carried across remove/re-add so a
        # flapping lane still accumulates toward being dumped.
        self._stats: Dict[str, tuple] = {}
        self._warmup = True

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="tor-health", daemon=True)
        self._thread.start()
        self.log(
            "tor-health: eyes open -- cadence %ds, floor %d healthy, "
            "probes opencode.ai:443 + check.torproject.org",
            self.check_interval, self.min_healthy,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
            self.log("tor-health: clocking out.")

    def check_and_heal(self) -> Dict[str, Any]:
        """Run one cycle synchronously (for an immediate API-driven check)."""
        return self._check_and_heal()

    # ------------------------------------------------------------------
    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._check_and_heal()
            except Exception as exc:  # noqa: BLE001
                self._log.warning("tor-health: poke cycle went sideways: %s", exc)
            self._stop.wait(self.check_interval)

    def _check_and_heal(self) -> Dict[str, Any]:
        # Per-cycle "we're awake" idle chatter floods INFO once a minute per
        # lane; lives on .debug so the operator only sees real events.
        if self._log.isEnabledFor(logging.DEBUG):
            self._log.debug("tor-health: poke cycle running%s",
                            " (warmup)" if self._warmup else "")

        # 0. Long-cooldown revival: any sidelined lane that's been sitting
        # out for at least ``_SIDELINED_RECHECK_S`` gets one re-probe attempt
        # at the top of this cycle. A successful revive re-registers the
        # proxy in the pool (the post-revive ``_sync_pool`` keeps it there
        # because the freshly-re-registered proxy passes the next
        # ``_health_check``); a still-broken revive leaves the sideline
        # stamp in place and the loop waits another full cooldown.
        self._maybe_unsideline_stale()

        # 1. Health-check every lane.
        results: List[Dict[str, Any]] = [self._health_check(l) for l in self.tor.lanes]
        healthy_count = sum(1 for r in results if r["healthy"])
        if self._log.isEnabledFor(logging.DEBUG):
            self._log.debug("tor-health: cycle verdict -- %d/%d lanes still cooking",
                            healthy_count, len(results))

        # 2. Heal unhealthy lanes.
        healed = 0
        # Anti-sync: position-within-cycle counter for ``_lane_heal_stagger_s``.
        # Bumped once per heal action (the main ``_heal_lane`` below and the
        # escalate ``regenerate_lane`` further down) -- not once per loop
        # iteration -- so an iteration that consults ``lane.healing`` or sees
        # the lane healthy never spends the budget. Reset per cycle; phases 3
        # (dump) and 5 (ensure-min) carry their own counters below. See the
        # constant block above ``_lane_heal_stagger_s`` for the rationale.
        heal_seq = 0
        for i, lane in enumerate(self.tor.lanes):
            # Symmetric completion of the heal-flag protocol. The daemon's
            # other actors -- ``_heal_lane`` (line ~507), the escalate path
            # (line ~299), ``_ensure_min_healthy`` (line ~601), and
            # ``_dump_burned_lanes`` (line ~660) -- each flip ``lane.healing``
            # True just before invoking ``regenerate_lane``. Until now this
            # heal-loop never CONSULTED the flag, so a lane the daemon had
            # already kicked into regeneration could be re-rolled by a
            # subsequent ``_heal_lane`` in the same or next cycle and the
            # second wipe raced the first torrc+restart write (double-restart
            # absorbed as a transient 503). The flag is now honoured: an
            # in-flight heal owns the lane this turn and the next
            # ``_check_and_heal`` tick re-evaluates when it settles.
            if getattr(lane, "healing", False):
                continue
            # Sidelined lanes are in permanent retirement: the cheap-poke
            # branch must not re-clamp the streak to a fresh cycle, the
            # escalate branch must not reach for ``regenerate_lane`` again,
            # and ``_dump_burned_lanes`` (next step) ignores them because the
            # sideline handler already pulled them out of the pool. The lane
            # stays in ``self.tor.lanes`` so the dashboard can render its
            # "sidelined" chip honestly; the revival happens on the long
            # cooldown via ``_maybe_unsideline_stale`` at the top of the next
            # cycle, matching the heal-loop filter.
            if getattr(lane, "sidelined", False):
                continue
            # Fast-fail for permanently-blocked exits. Counter is the
            # monotonic ``lane.unhealthy_cycles`` bumped by the heal-loop
            # *before* this branch. Once it crosses
            # ``_FAST_FAIL_PROBING_CYCLES`` the lane is sidelined through
            # the same path as the ``MAX_REGEN_FAILURES`` tribunal below
            # (sets ``sidelined=True``, stamps ``last_sideline_at``,
            # evicts from the pool) so the existing
            # ``_maybe_unsideline_stale`` revival picks it back up after
            # ``_SIDELINED_RECHECK_S`` -- no second revival code path to
            # maintain. Default cap = 4 cycles × 60 s = 4 minutes, wider
            # than a single Tor circuit rebuild (30-90 s, consensus
            # re-fetch included) so a slow-but-recovering lane isn't
            # retired mid-bootstrap, but well under the heal ladder's
            # natural ~43 minute convergence so the dashboard chip ladder
            # visibly climbs from "down" to "sidelined" before either
            # user-facing requests fail or the operator notices the
            # stuck row. Tunable via
            # ``LINGLING_TOR_FAST_FAIL_PROBING_CYCLES`` for networks
            # whose typical Tor cycle exceeds 60 s.
            if lane.unhealthy_cycles >= _FAST_FAIL_PROBING_CYCLES:
                now = time.time()
                pid = f"tor-{lane.index}"
                try:
                    lane.sidelined = True
                    lane.last_sideline_at = now
                    # Pin the circuit-breaker counter at the cap so the
                    # post-sideline summarise (the regen-failures chip on
                    # the sidelined slot) matches what an organic
                    # ``MAX_REGEN_FAILURES`` retiree would carry into
                    # the next unsideline -- one glance at the dashboard
                    # reads the same way regardless of which path
                    # produced the retirement.
                    lane.regen_failures = MAX_REGEN_FAILURES
                    # Mirror the organic tribunal: clear stale UX bits
                    # so the slot reads "sidelined" (not "down -> sidelined"
                    # with a stale chip in the trailing cycles).
                    lane.probing = False
                    if self.pool.get_by_id(pid) is not None:
                        self.pool.remove(pid)
                    self.log(
                        "tor-health: lane #%d sideline -- fast-fail "
                        "(unhealthy %d cycles >= %d)",
                        lane.index, lane.unhealthy_cycles,
                        _FAST_FAIL_PROBING_CYCLES,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._log.warning(
                        "tor-health: fast-fail sideline flamed out for "
                        "lane #%d: %s", lane.index, exc,
                    )
                continue
            if results[i]["healthy"]:
                # Destination-side burn: probe-passes-but-pool-LEDGER-says
                # --egress-429s.  Distinct from the heal ladder below --
                # that one only fires when ``_health_check`` itself flagged
                # a tunnel problem, so a lane the heal ladder has
                # determined is fine can still be burned at the
                # destination (the SOCKS port is open, tor builds circuits
                # fine to check.torproject.org, but every chat-API request
                # gets a 429 from the per-IP persona). Re-rolling the
                # DataDirectory within the same ``ExitNodes {cc}`` pin
                # wouldn't escape that -- the *country* persona is what's
                # throttled, not just one exit IP.  Detecting this here
                # (in the "lane is healthy" branch) is the only place
                # where we know the lane is up but the egress is
                # throttled; the heal ladder sees nothing wrong.  The
                # action (``_maybe_handle_destination_burn`` reads
                # ``self.pool.status()`` for the lane's pool proxy and
                # rotates ``lane.exit_country`` to an unused country
                # when the per-lane ledger is past conviction).
                # No-op when no burn detected.
                burn_status = self._destination_burn_status(lane)
                self._maybe_handle_destination_burn(lane, burn_status)
                # Reset the per-instance restart streak whenever a non-heal
                # cycle observes the lane healthy. The streak only
                # accumulates while the post-heal probe fails, so a single
                # flap never poisons the counter toward an unnecessary
                # ``regenerate_lane`` later. ``unhealthy_cycles`` resets
                # here too -- a passing probe is the lane's "I'm back"
                # signal and the fast-fail gate should not look at past
                # failures once the lane has recovered.
                lane.restart_attempts = 0
                lane.unhealthy_cycles = 0
                continue
            # Step-1 snapshot says unhealthy: bump the monotonic failure
            # counter the heal loop's fast-fail gate reads. This is the
            # authoritative signal of "this lane has been broken for N
            # consecutive daemon cycles". It does NOT reset across
            # ``regenerate_lane`` calls inside this loop -- the lane's *exit
            # blockage* is unchanged across DataDirectory wipes, so the
            # counter keeps climbing toward ``_FAST_FAIL_PROBING_CYCLES``
            # until either the exit recovers (next cycle's
            # ``results[i]["healthy"]`` is True -- reset above) or fast-fail
            # sidelining trips. Reset only on a passing probe so it tracks
            # "how long has this lane been unhealthy" monotonically.
            lane.unhealthy_cycles += 1
            # Warmup: a port-open lane whose first probe failed is still
            # bootstrapping its circuits, not broken -- the SOCKS port only
            # opens after stem's ``Bootstrapped 100``, but the first circuit
            # through the tunnel can lag it. Restarting/regenerating it now
            # would tear down a tor that was about to come up. Port-closed
            # lanes still heal (a launch that never bound its port is a real
            # failure worth retrying).
            if self._warmup and _port_is_open("127.0.0.1", lane.socks_port, timeout=PORT_CHECK_TIMEOUT):
                continue
            # Anti-sync stagger before kicking off this lane's heal. The gap
            # grew during a 429-storm co-flunk pattern: every lane tore down
            # + rebuilt at the same instant, leaving 0/N lanes for the
            # bootstrap window. Without this gap the rebuild stack races
            # itself; with it, each lane kicks its tor.exe off a fraction of
            # a second later, so the cohort never co-reboots. See
            # ``_lane_heal_stagger_s``.
            if heal_seq > 0:
                time.sleep(_lane_heal_stagger_s(heal_seq))
            heal_seq += 1
            self.log("tor-health: cooking lane #%d (%s) on port %d back to health ...",
                     lane.index, lane.exit_country, lane.socks_port)
            try:
                did_regenerate = self._heal_lane(lane)
                results[i] = self._health_check(lane)
                if results[i]["healthy"]:
                    lane.restart_attempts = 0
                    # Successful post-heal probe (cheap-restart or
                    # wholesale-regenerate both land here): the lane is
                    # back, the monotonic ``unhealthy_cycles`` resumes its
                    # zero baseline right now rather than carrying an
                    # extra cycle's worth until the next cycle's
                    # step-1 reset.
                    lane.unhealthy_cycles = 0
                    healed += 1
                    self.log("tor-health: lane #%d's back in the kitchen", lane.index)
                    continue
                # Post-heal verdict still unhealthy:
                # if ``_heal_lane`` already rolled a fresh DataDirectory
                # inside this call (its regenerate fallback fired because the
                # SOCKS port failed to reopen after restart, or the control
                # port was dead), give the new lane bootstrap at least one
                # cycle to settle rather than immediately re-bumping the
                # streak against the next-cycle escalate. A fresh Tor
                # consensus + first-circuit build runs 30-90s -- cheap-
                # restart-looping the lane inside that window just prolongs
                # the boot and never lets it build a working circuit.
                if did_regenerate:
                    self.log("tor-health: lane #%d's fresh from a regenerate "
                             "but still flunking -- sitting one cycle before "
                             "streak tracking kicks back in", lane.index)
                    continue
                # Restart reopened the SOCKS/Control ports but the upstream
                # tunnel stays failing -- the persistent-stuck-exit pattern.
                # Bump the per-lane streak and only escalate to a wholesale
                # ``regenerate_lane`` once it's persistent across
                # ``_TOR_ESCALATE_AFTER_RESTARTS`` cycles *and* the cooldown
                # since the last regenerate has elapsed. Without this gate Tor
                # -- whose regenerate is far heavier, a full
                # DataDirectory wipe + consensus re-fetch + circuit rebuild --
                # would happily re-roll every 3 cycles = once per ~3 minutes
                # against a stable block, moving the lane into permanent
                # boot instead of letting a fresh circuit stabilise.
                lane.restart_attempts += 1
                cooldown_left = (
                    _TOR_REGENERATE_COOLDOWN_S
                    - (time.time() - lane.last_regenerate_at)
                )
                streak_at_cap = lane.restart_attempts >= _TOR_ESCALATE_AFTER_RESTARTS
                cooldown_ok = lane.last_regenerate_at == 0.0 or cooldown_left <= 0
                if streak_at_cap and not cooldown_ok:
                    if self._log.isEnabledFor(logging.DEBUG):
                        self._log.debug(
                            "tor-health: lane #%d's stuck post-restart "
                            "(streak %d) but regenerate cooldown has %.0fs "
                            "left -- cheap-retry this cycle",
                            lane.index, lane.restart_attempts,
                            max(0.0, cooldown_left),
                        )
                    continue
                if not streak_at_cap:
                    if self._log.isEnabledFor(logging.DEBUG):
                        self._log.debug(
                            "tor-health: lane #%d's still sideways after "
                            "heal (port-open, probe-fail), cheap-retry %d/%d",
                            lane.index, lane.restart_attempts,
                            _TOR_ESCALATE_AFTER_RESTARTS,
                        )
                    continue
                self.log("tor-health: lane #%d's been stuck for %d "
                         "post-heal cycles -- escalating to a wholesale "
                         "regenerate", lane.index, lane.restart_attempts)
                lane.healing = True
                lane.probing = True
                # Anti-sync: the escalate regenerate is its own tor-modifying
                # action -- spend another stagger slot so it doesn't stack
                # on top of the main-heal teardown that just fired on this
                # lane or land flush against the next lane's heal.
                if heal_seq > 0:
                    time.sleep(_lane_heal_stagger_s(heal_seq))
                heal_seq += 1
                try:
                    self.tor.regenerate_lane(lane, log=self.log)
                    time.sleep(2)
                except Exception as exc:  # noqa: BLE001
                    self._log.warning("tor-health: escalate-regenerate tanked for "
                                      "lane #%d: %s", lane.index, exc)
                finally:
                    lane.healing = False
                    lane.restart_attempts = 0
                    lane.last_regenerate_at = time.time()
                results[i] = self._health_check(lane)
                if results[i]["healthy"]:
                    healed += 1
                    # Successful post-regen: clear circuit-breaker state so
                    # the next heal on this lane starts from zero, not from a
                    # near-cap streak that's one regen away from permanent
                    # retirement. Reset ``unhealthy_cycles`` here too -- the
                    # regenerate brought the lane back, no point carrying
                    # the failure window through the recovery cycle.
                    lane.regen_failures = 0
                    lane.sidelined = False
                    lane.last_sideline_at = 0.0
                    lane.unhealthy_cycles = 0
                    self.log("tor-health: lane #%d's back in the kitchen "
                             "after the wholesale swap", lane.index)
                else:
                    lane.regen_failures += 1
                    # Past the cap the loop-eats-its-tail pattern collapses:
                    # bump the circuit-breaker counter. Once it crosses the
                    # threshold retire the lane for the long sideline cooldown
                    # so the next cycle doesn't re-burn Tor's slow regenerate
                    # on a known-burned exit. ``continue`` to the next lane --
                    # the for-loop's natural iteration step takes us there.
                    if lane.regen_failures >= MAX_REGEN_FAILURES:
                        try:
                            lane.sidelined = True
                            lane.last_sideline_at = time.time()
                            pid = f"tor-{lane.index}"
                            if self.pool.get_by_id(pid) is not None:
                                self.pool.remove(pid)
                            self.log(
                                "tor-health: lane #%d sidelined (regen_failures=%d) -- "
                                "retreating; will re-test in ~%.0fm",
                                lane.index, lane.regen_failures,
                                _SIDELINED_RECHECK_S / 60.0,
                            )
                        except Exception as exc:  # noqa: BLE001
                            self._log.warning(
                                "tor-health: sideline flamed out for lane #%d: %s",
                                lane.index, exc,
                            )
                        continue
                    self.log("tor-health: lane #%d's still sideways even "
                             "after the wholesale swap (regen_failures=%d)",
                             lane.index, lane.regen_failures)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("tor-health: heal flamed out for lane #%d: %s",
                                  lane.index, exc)

        # 3. Dump burned lanes (re-check the touched lanes before pool sync -
        # regenerate replaces the lane wholesale, so the step-1 snapshot is
        # stale; without this the pool gauge sat at 0 while status reported
        # healthy lanes).
        dumped = self._dump_burned_lanes()
        if dumped:
            for i, lane in enumerate(self.tor.lanes):
                results[i] = self._health_check(lane)
            healthy_count = sum(1 for r in results if r["healthy"])

        # 4. Sync pool: only healthy lanes.
        self._sync_pool(results)

        # 5. Ensure minimum healthy lane count.
        regenerated = 0
        if healthy_count < self.min_healthy:
            self.log("tor-health: only %d cooking, need %d -- cooking more lanes",
                     healthy_count, self.min_healthy)
            regenerated = self._ensure_min_healthy(results)
            for i, lane in enumerate(self.tor.lanes):
                if not results[i]["healthy"]:
                    results[i] = self._health_check(lane)
            healthy_count = sum(1 for r in results if r["healthy"])
            self._sync_pool(results)

        pool_status = self.pool.status()
        if self._log.isEnabledFor(logging.DEBUG):
            self._log.debug(
                "tor-health: cycle out -- %d cooking, %d healed, %d dumped, "
                "%d regenerated, pool %d/%d live",
                healthy_count, healed, dumped, regenerated,
                pool_status.get("available", 0), pool_status.get("total", 0),
            )

        # First cycle over -- subsequent cycles run full probes + pool removal.
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
    def _health_check(self, lane: TorLane) -> Dict[str, Any]:
        pid = f"tor-{lane.index}"
        port_open = _port_is_open("127.0.0.1", lane.socks_port, timeout=PORT_CHECK_TIMEOUT)
        control_alive = _port_is_open("127.0.0.1", lane.control_port, timeout=PORT_CHECK_TIMEOUT)

        proxy_in_pool = self.pool.get_by_id(pid)
        # When the proxy is in the
        # pool, take the live pool counters; when it isn't (evicted last
        # cycle, not yet re-pooled), hold the previously-persisted value on
        # the lane -- not a hard-coded 0. The previous 0-fallback flipped a
        # permanently-unhealthy Tor lane's counters to zero the moment the
        # pool evicted it, so the dashboard's chip ladder fell back to
        # ``up``-off-``running`` and the operator lost the cause-of-eviction
        # at a glance. Frozen-on-eviction keeps the verdict the daemon is
        # already computing honest in the UI too.
        if proxy_in_pool is not None:
            consecutive_fails = proxy_in_pool.consecutive_failures
            total_429s = proxy_in_pool.total_429
        else:
            consecutive_fails = lane.consecutive_failures
            total_429s = lane.total_429
        pool_healthy = consecutive_fails < MAX_CONSECUTIVE_FAILURES

        # Run both probes whenever the SOCKS port is up -- warmup included.
        # The warmup cycle used to skip probes entirely, which left every
        # lane with no verdict (``healthy=False``) for the first 60s and the
        # dashboard painted the whole rack ``down`` until the first request
        # happened to land. A lane whose SOCKS port is open has finished
        # bootstrapping (stem only returns once ``Bootstrapped 100`` fires),
        # so probing it in the first cycle is safe: a pass marks it healthy
        # immediately, a fail keeps ``probing=True`` (still settling) and
        # the heal loop below skips port-open lanes during warmup so the
        # still-bootstrapping tor isn't torn down.
        opencode_reachable: Optional[bool] = None
        is_tor: Optional[bool] = None
        if port_open:
            try:
                opencode_reachable = _socks5_http_probe(
                    _probe_url(lane.proxy_url), timeout=TOR_HTTP_PROBE_TIMEOUT)
            except Exception:  # noqa: BLE001
                opencode_reachable = False
            try:
                is_tor, exit_ip = _socks5h_is_tor_probe(
                    lane.proxy_url, timeout=TOR_ISTOR_PROBE_TIMEOUT)
                if is_tor and exit_ip:
                    lane.exit_ip = exit_ip
                elif not is_tor:
                    # Probe failed or non-Tor exit: clear the cached IP so the
                    # dashboard never shows a stale value for a dead lane.
                    lane.exit_ip = ""
            except Exception:  # noqa: BLE001
                pass

        # "up but stuck" -- SOCKS port open, no circuit built recently. This
        # catches the case where tor is alive but every build is failing
        # (e.g. the pinned country has no exits); restart beats waiting.
        stuck = (
            lane.last_circuit_built_ts > 0
            and (time.time() - lane.last_circuit_built_ts) > TOR_STUCK_CIRCUIT_S
        )

        healthy = port_open and control_alive and pool_healthy
        if opencode_reachable is not None:
            healthy = healthy and opencode_reachable
        if is_tor is not None:
            healthy = healthy and is_tor
        if stuck:
            healthy = False

        # Past warmup this health check has actually probed the lane (or
        # decided it didn't warrant a probe) -- so we now have a real verdict
        # (up/down) and the dashboard can drop the transitional "probing" chip
        # it shows during cold start, when a freshly-launched tor's bound SOCKS
        # port doesn't yet prove circuits are carrying traffic. During warmup
        # the probe only runs when the SOCKS port is open (see above), and a
        # FAILING warmup probe keeps ``probing`` up -- stem returns once the
        # control port connects, so a port-open lane can still be building its
        # first circuit, and "no verdict yet" is more honest than ``down``.
        # Only a passing warmup probe (``healthy``) clears the flag. Cleared
        # here, re-raised by _heal_lane / _ensure_min_healthy /
        # _dump_burned_lanes alongside `healing` whenever the daemon re-spawns
        # or regenerates the lane.
        if not self._warmup or (port_open and healthy):
            lane.probing = False

        # Stamp ``verified_at`` the moment a probe closes with ``healthy=True``
        # so the chip ladder can split cold-start ``probing`` (never verified,
        # may legitimately fail) from "re-verifying post-restart" (was up, was
        # bounced, the next probe will re-confirm).
        # render the same chip semantics. Stamped only on success -- a lane
        # that has started failing cannot trick the UI into thinking the
        # bounce is just a re-roll.
        if healthy:
            lane.verified_at = time.time()

        # Persist the per-cycle verdict back onto the lane so
        # ``TorLane.status()`` surfaces it through ``/api/tor`` for the
        # dashboard's identity rack. The verdict was previously stranded in
        # the transient ``results[i]`` dict that this method returns -- which
        # never escaped the daemon's loop -- so the rack built its chips off
        # ``TorLane.running`` alone (port-open = "up", even when the probe
        # through the tunnel just failed). Mirrors the write-back in
        # one-to-one so the same frontend
        # ``slotRow`` ladder renders truthfully on both families.
        # During warmup a port-open lane's first probe may fail while its
        # circuits are still settling -- that must not persist as a
        # ``False`` verdict (the dashboard would flip the lane to ``down``
        # mid-bootstrap). A passing probe writes the ``up`` verdict at once;
        # a failing one leaves ``healthy=None`` (no verdict) so the chip
        # stays on ``probing`` until a post-warmup cycle judges the lane.
        if healthy or not self._warmup:
            lane.healthy = healthy
        lane.consecutive_failures = consecutive_fails
        lane.total_429 = total_429s

        return {
            "index": lane.index,
            "port": lane.socks_port,
            "socks_port": lane.socks_port,
            "control_port": lane.control_port,
            "exit_country": lane.exit_country,
            "exit_ip": lane.exit_ip,
            "control_alive": control_alive,
            "port_open": port_open,
            "opencode_reachable": opencode_reachable,
            "is_tor": is_tor,
            "consecutive_failures": consecutive_fails,
            "total_429": total_429s,
            "pool_healthy": pool_healthy,
            "in_pool": proxy_in_pool is not None,
            "last_circuit_built_ts": lane.last_circuit_built_ts,
            "renewing": lane.renewing,
            "boot_ok": lane.boot_ok,
            # True while the health daemon is actively restarting or
            # regenerating this lane. Surfaced so the dashboard can render a
            # "healing" chip -- separate from renewing (the NEWNYM sweep) and
            # from boot_ok (initial bootstrap), so a manual heal is visibly
            # distinct from either.
            "healing": lane.healing,
            "probing": lane.probing,
            # Circuit-breaker trio surfaced so the dashboard can render the
            # "sidelined" chip honestly (a sidelined slot is *not* a working
            # one even if tor.exe is up).
            # one-to-one so the same frontend chip ladder handles both pools.
            "regen_failures": lane.regen_failures,
            "sidelined": lane.sidelined,
            "last_sideline_at": lane.last_sideline_at,
            "pid": pid,
            "healthy": healthy,
        }

    # ------------------------------------------------------------------
    # Healing actions
    # ------------------------------------------------------------------
    def _heal_lane(self, lane: TorLane) -> bool:
        """Restart tor.exe for a lane; if that fails, regenerate wholesale.

        Sets ``lane.healing`` for the whole operation so the dashboard shows a
        "healing" chip while the daemon restarts or regenerates -- distinct
        from ``renewing`` (the dashboard's NEWNYM sweep). Cleared in finally
        so a regenerate that raises never leaves the lane stuck "healing".

        Returns ``True`` when this call ended with a wholesale
        ``regenerate_lane`` (either the SOCKS or control port failed to come
        back up after a restart), ``False`` when it ended with only a restart.
        The caller in ``_check_and_heal`` uses the return value to defer
        streak-tracking on a freshly-rolled DataDirectory: a Tor regenerate
        is the slow, expensive one (consensus re-fetch + first circuit
        build, 30-90s), so the new lane deserves at least one cycle to settle
        before we re-bump the streak against the next-cycle escalate.
        """
        lane.healing = True
        # The restarted/regenerated tor's SOCKS port may bind before its first
        # circuit builds -- raise `probing` alongside `healing` so the slot
        # reads "probing" (unverified) instead of the verdict it held before
        # the heal. The post-heal `_health_check` clears it again, so the
        # operator sees healing -> probing -> up|down rather than healing ->
        # a stale verdict that hadn't been re-probed yet.
        lane.probing = True
        did_regenerate = False
        try:
            try:
                self.tor.restart_lane(lane, log=self.log)
                time.sleep(2)
                if _port_is_open("127.0.0.1", lane.socks_port, timeout=2.0):
                    self.log("tor-health: lane #%d's back in the kitchen after a poke",
                             lane.index)
                    return False
            except Exception as exc:  # noqa: BLE001
                self._log.warning("tor-health: restart poke flamed out for lane #%d: %s",
                                  lane.index, exc)
            # Restart didn't bring it back -- wipe the DataDirectory and re-roll.
            # Reset the per-lane restart streak + stamp the cooldown here too,
            # for the same reasons as the heal path:
            # the new lane starts from a clean slate, and without the stamp the
            # loop's cooldown gate would happily re-escalate on a freshly-
            # regenerated lane within minutes, moving the lane into permanent
            # boot instead of letting the fresh guard consensus stabilise.
            if (not _port_is_open("127.0.0.1", lane.socks_port, timeout=1.0)
                    or not _port_is_open("127.0.0.1", lane.control_port, timeout=1.0)):
                self.log("tor-health: cooking a fresh lane for #%d from scratch ...",
                         lane.index)
                try:
                    self.tor.regenerate_lane(lane, log=self.log)
                    time.sleep(2)
                    did_regenerate = True
                    lane.restart_attempts = 0
                    lane.last_regenerate_at = time.time()
                except Exception as exc:  # noqa: BLE001
                    self._log.warning("tor-health: regenerate tanked for lane #%d: %s",
                                      lane.index, exc)
                    raise
            return did_regenerate
        finally:
            lane.healing = False

    def _destination_burn_status(self, lane: TorLane) -> Dict[str, Any]:
        """Read the pool ledger for ``tor-<lane.index>`` and answer
        "is this lane's egress persona throttled at the destination?"..

        Distinct from ``_health_check`` which only proves the SOCKS/Circuit
        reachability to ``check.torproject.org`` -- a probe the Tor network
        itself answers fine even when the e.g. chat API's per-IP persona
        is freshly 429-throttled. The pool ledger tracks real-egress
        outcomes (the executor calls ``mark_failure``/``mark_success`` per
        upstream response) and is the only place that knows "every request
        through this lane the past 30 seconds got a 429".

        Returns a tag-only dict with a ``burned`` flag for the heal loop
        to branch on, plus the raw numbers so the destination-burn action
        can log them and the dashboard can render "burned 9×429" without
        a second walk of the pool.
        """
        pid = f"tor-{lane.index}"
        empty = {
            "burned": False, "consec": 0, "total_429": 0, "total": 0,
            "ratio": 0.0, "reason": "no-proxy",
        }
        if self.pool is None:
            return empty
        proxy = self.pool.get_by_id(pid)
        if proxy is None:
            return empty
        consec = int(getattr(proxy, "consecutive_failures", 0) or 0)
        total_429 = int(getattr(proxy, "total_429", 0) or 0)
        total = int(getattr(proxy, "total_requests", 0) or 0)
        # Floor on total_requests -- a lane that's only carried 2 probes
        # can't be called burned on 2/2 of 429s (could be sampling noise).
        if total < _DEST_BURN_TOTAL_MIN:
            return {
                "burned": False, "consec": consec, "total_429": total_429,
                "total": total, "ratio": (total_429 / total if total else 0.0),
                "reason": "below-floor",
            }
        ratio = total_429 / total if total else 0.0
        burned = (
            consec >= _DEST_BURN_CONSEC
            and total_429 >= _DEST_BURN_TOTAL_429_MIN
            and ratio >= _DEST_BURN_429_RATIO
        )
        return {
            "burned": burned, "consec": consec, "total_429": total_429,
            "total": total, "ratio": ratio, "reason": "ok" if burned else "below-conviction",
        }

    def _maybe_handle_destination_burn(
        self, lane: TorLane, status: Dict[str, Any]
    ) -> None:
        """Rotate ``lane.exit_country`` and fire a regenerate when the
        pool-ledger detector above flags a destination burn AND the lane
        isn't already under regenerate cooldown.

        Distinct from the existing heal loop's ``regenerate-ladder``:
        that ladder always re-uses the lane's current ``exit_country``
        (which is exactly what the destination has throttled), so a
        re-roll within same country can't escape a per-country
        throttling. Rotating to a fresh country and regenerating brings
        the new persona online in ``_TOR_REGENERATE_COOLDOWN_S`` minus
        the existing regenerate bonanza the heal loop already pays -- and
        uses ``_DEST_BURN_ROTATE_COOLDOWN_S`` (default 10 min) as the
        gap between successive rotate attempts on the same lane, so a
        persistent per-country burn sideline-evicts the lane via
        ``destination_burn_count`` long before it can re-burn every
        cycle.

        No-op when:
        - ``status["burned"]`` is False
        - the lane is currently healing / sidelined
        - another burn-handler already fired within
          ``_DEST_BURN_ROTATE_COOLDOWN_S``
        - ``TorManager.next_unused_exit_country`` returned ``None`` (every
          configured country is in use by another active lane; backing
          off rather than regenerating onto the same throttled persona)
        """
        if not status["burned"]:
            return
        if getattr(lane, "healing", False) or getattr(lane, "sidelined", False):
            return
        now = time.time()
        if (
            getattr(lane, "last_burn_rotate_at", 0.0) > 0.0
            and now - lane.last_burn_rotate_at < _DEST_BURN_ROTATE_COOLDOWN_S
        ):
            return
        new_cc = self.tor.rotate_exit_country(lane)
        if new_cc is None:
            self.log(
                "tor-health: lane #%d burned at destination "
                "(consec=%d, 429=%d/%d) but every configured country is "
                "in use -- holding rotation",
                lane.index, status["consec"], status["total_429"], status["total"],
            )
            lane.destination_burn_count += 1
            return
        lane.destination_burn_count += 1
        lane.last_burn_rotate_at = now
        lane.last_regenerate_at = now
        lane.probing = True
        lane.healing = True
        lane.boot_ok = False
        try:
            self.tor.regenerate_lane(
                lane, log=lambda m, *a: self._log.info(m, *a),
            )
            self.log(
                "tor-health: lane #%d burned at destination "
                "(consec=%d, 429=%d/%d, ratio=%.2f) -- rotated %s -> %s "
                "and queued regenerate (burn_count=%d)",
                lane.index, status["consec"], status["total_429"],
                status["total"], status["ratio"], lane.exit_country,
                new_cc, lane.destination_burn_count,
            )
        finally:
            lane.healing = False

    def _maybe_unsideline_stale(self) -> None:
        """Single stale-revive pass over ``self.tor.lanes``.

        Walks every sidelined lane once per cycle and, for any whose
        ``last_sideline_at`` is older than ``_SIDELINED_RECHECK_S``, calls
        ``TorManager.try_retry_sidelined`` to re-probe the lane through its
        existing tor.exe tunnel and (on success) re-register it in the proxy
        pool. The next cycle's ``_sync_pool`` + ``_health_check`` will either
        confirm the lane is back (and keep it in the pool) or re-sideline it
        via the escalate path. Failures here are caught so a probe-side
        blip can't take down the rest of the cycle.
        ``_maybe_unsideline_stale``.
        """
        now = time.time()
        for lane in self.tor.lanes:
            if not getattr(lane, "sidelined", False):
                continue
            if now - lane.last_sideline_at < _SIDELINED_RECHECK_S:
                continue
            try:
                self.tor.try_retry_sidelined(lane, self.pool, log=self.log)
            except Exception as exc:  # noqa: BLE001
                self._log.warning(
                    "tor-health: revive attempt flamed out for lane #%d: %s",
                    lane.index, exc,
                )

    def _remember_stats(self, proxy_id: str, px: Any) -> None:
        """Preserve a removed lane's burn counters across the remove/re-add
        gap, so a flapping lane still accumulates towards being dumped."""
        self._stats[proxy_id] = (px.consecutive_failures, px.total_429, px.total_requests)

    def _restore_stats(self, proxy_id: str, px: Any) -> None:
        remembered = self._stats.get(proxy_id)
        if remembered is None:
            return
        px.consecutive_failures, px.total_429, px.total_requests = remembered

    def _sync_pool(self, results: List[Dict[str, Any]]) -> Tuple[int, int]:
        """Add healthy lanes, remove unhealthy ones. Warmup only adds."""
        added = removed = 0
        for i, lane in enumerate(self.tor.lanes):
            pid = f"tor-{lane.index}"
            is_healthy = results[i]["healthy"]
            existing = self.pool.get_by_id(pid)

            if is_healthy and existing is None:
                fresh = self.pool.add(
                    lane.proxy_url,
                    proxy_id=pid,
                    label=f"Tor lane #{lane.index} {{{lane.exit_country}}}",
                )
                self._restore_stats(pid, fresh)
                added += 1
            elif not is_healthy and existing is not None:
                if self._warmup:
                    # Let the still-bootstrapping tor ride until next cycle.
                    continue
                self._remember_stats(pid, existing)
                self.pool.remove(pid)
                removed += 1
            elif is_healthy and existing is not None:
                # Port can change under regenerate; write through the pool so
                # the executor reads the new url under the pool's lock.
                self.pool.set_url(pid, lane.proxy_url)
        return added, removed

    def _ensure_min_healthy(self, results: List[Dict[str, Any]]) -> int:
        """Regenerate unhealthy lanes until the minimum healthy count is met."""
        healthy_count = sum(1 for r in results if r["healthy"])
        need = self.min_healthy - healthy_count
        regenerated = 0
        # Anti-sync: own per-loop counter so the ensure-min phase never
        # co-fires its regenerates with either the previous heal phase (2)
        # or the dump phase (3) -- the underlying tor.exe would still all
        # tear down + rebuild inside the same wall-clock second otherwise.
        # See ``_lane_heal_stagger_s``.
        regen_seq = 0
        for i, lane in enumerate(self.tor.lanes):
            if need <= 0:
                break
            if not results[i]["healthy"]:
                if self._warmup and _port_is_open(
                        "127.0.0.1", lane.socks_port, timeout=PORT_CHECK_TIMEOUT):
                    # Warmup lane whose first probe failed but whose SOCKS
                    # port is open: still bootstrapping its circuits, not
                    # broken -- regenerating (DataDirectory wipe + consensus
                    # re-fetch) would tear down a tor that was about to come
                    # up. The heal loop already skips these; the next cycle
                    # judges them once warmup lapses.
                    continue
                lane.healing = True
                # Freshly regenerated lane re-enters the "unverified" window
                # until the next cycle's probe -- raised here in lock with
                # `healing` so the dashboard's "healing -> probing -> up"
                # transition works the same way as the explicit heal path.
                lane.probing = True
                if regen_seq > 0:
                    time.sleep(_lane_heal_stagger_s(regen_seq))
                regen_seq += 1
                try:
                    self.log("tor-health: ensure-min -- cooking lane #%d from scratch",
                             lane.index)
                    self.tor.regenerate_lane(lane, log=self.log)
                    time.sleep(2)
                    # Same reset as the other Tor regenerate sites: a freshly
                    # generated lane starts from a clean streak + cooldown
                    # timestamp so the loop's cooldown gate doesn't
                    # immediately re-escalate on the lane while its new
                    # DataDirectory / consensus is still settling.
                    lane.restart_attempts = 0
                    lane.last_regenerate_at = time.time()
                    regenerated += 1
                    need -= 1
                except Exception as exc:  # noqa: BLE001
                    self._log.warning("tor-health: ensure-min regenerate flamed out for lane #%d: %s",
                                      lane.index, exc)
                finally:
                    lane.healing = False
        return regenerated

    def _dump_burned_lanes(self) -> int:
        """Regenerate lanes whose pool counters crossed the burn thresholds."""
        dumped = 0
        # Anti-sync: own per-loop counter so the dump phase never co-fires
        # its regenerates with the heal phase above or the ensure-min phase
        # below -- a sustained 429 storm that hits burn thresholds across
        # multiple lanes is the textbook co-flunk trigger, and the dump
        # loop is the one consolidated place the daemon re-rolls them all
        # in a single pass. See ``_lane_heal_stagger_s``.
        dump_seq = 0
        for px in self.pool.get_all_proxies():
            if not px.id.startswith("tor-"):
                continue
            try:
                idx = int(px.id.split("-")[1])
            except (ValueError, IndexError):
                continue
            lane = next((l for l in self.tor.lanes if l.index == idx), None)
            if lane is None:
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
                # PB5-W3-XX sibling (dump-burned-cooldown, Tor side): mirrors
                # A 429-storm downstream
                # burns across every lane simultaneously, and the loop here
                # would otherwise regenerate the lot in lockstep (3+ Tor
                # lanes, each 30-90s of DataDirectory wipe + consensus re-
                # fetch + circuit build, racing the rebuild). The loop's
                # escalate path already consults ``lane.last_regenerate_at``
                # but this dump loop set that stamp AFTER its own regen,
                # never CONSULTED it before -- so a lane burned again within
                # the cooldown window got re-rolled fresh while the prior
                # regen was still building a circuit. Vault the dump behind
                # the same cooldown gate so a re-burn in the cooldown window
                # holds this cycle and the next one re-evaluates once it
                # lapses. Without this, a sustained Cloudflare-side 429 ban
                # would re-roll every Tor lane on every 60s cycle -- never
                # letting any lane build its first circuit.
                cooldown_left = (
                    _TOR_REGENERATE_COOLDOWN_S
                    - (time.time() - lane.last_regenerate_at)
                ) if lane.last_regenerate_at > 0 else 0.0
                if cooldown_left > 0:
                    if self._log.isEnabledFor(logging.DEBUG):
                        self._log.debug(
                            "tor-health: lane #%d burned (%s) but "
                            "regenerate cooldown has %.0fs left -- hold this "
                            "cycle (and downstream burns stay put)",
                            idx, reason, max(0.0, cooldown_left),
                        )
                    continue
                self.log("tor-health: dumping lane #%d (%s) -- cooking a fresh one ...",
                         idx, reason)
                self.pool.remove(px.id)
                # Mirror _heal_lane / _ensure_min_healthy: flag the lane while
                # we regenerate it so the dashboard renders the healing chip
                # (a burn-dump looks identical to a manual heal from the
                # operator's seat -- the lane was here, now it's gone but
                # coming back). ``probing`` rides alongside ``healing`` so the
                # chip shows the same "healing -> probing -> up" transition the
                # other regenerate paths produce; the post-dump _health_check
                # at the end of _check_and_heal clears it. Cleared in finally
                # so a raise can't strand it.
                lane.healing = True
                lane.probing = True
                if dump_seq > 0:
                    time.sleep(_lane_heal_stagger_s(dump_seq))
                dump_seq += 1
                try:
                    self.tor.regenerate_lane(lane, log=self.log)
                    self.log("tor-health: lane #%d's re-cooked and in the bag", idx)
                    # Stamp the loop-escalation cooldown so the burn-dump's
                    # regenerate counts toward the streak-gate, like the
                    # other Tor regenerate sites do (without this, a lane the
                    # daemon just burned and re-rolled could re-escalate
                    # inside the cooldown window and re-roll again).
                    lane.restart_attempts = 0
                    lane.last_regenerate_at = time.time()
                    dumped += 1
                except Exception as exc:  # noqa: BLE001
                    self._log.warning("tor-health: post-dump regenerate tanked for lane #%d: %s",
                                      idx, exc)
                finally:
                    lane.healing = False
        return dumped
