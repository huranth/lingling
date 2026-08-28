"""Tor egress lanes -- genuine exit-IP diversity from a single family.

Each lane is a separate ``tor.exe`` pinned to a *different* exit country via
``StrictNodes 1`` + disjoint ``ExitNodes {cc}``, so exit IPs are guaranteed
distinct by construction. No shared anycast colo can collapse them the way a
single tunnelling vendor's identities did.

Lanes register into Lingling's shared ProxyPool as first-class members, so
least-loaded-first rotates across the distinct lanes. Tor's higher latency
means it naturally soaks overflow once a hot lane throttles -- never the fast
path, always the softener.

A manager owns N lanes, each a local SOCKS5 proxy on 127.0.0.1:52001+.
``stem`` is imported lazily -- if it (or tor.exe) is absent the gateway still
serves directly; the lane status surfaces "Tor unavailable" rather than
crashing.

Architecture (all on 127.0.0.1):

    Lingling ProxyPool picks one of:
        socks5h://127.0.0.1:52001  --> tor #1 --> us exit  --> opencode.ai
        socks5h://127.0.0.1:52002  --> tor #2 --> de exit  --> opencode.ai
        ...

``socks5h://`` makes the proxy resolve remote DNS over the tunnel (the form
torproject's own docs recommend) so the exit IP is the lane's, not the host's.

NEWNYM (``torspec/control-spec.txt §3.7``) -- "switch to clean circuits" -- is
the quick-heal the dashboard's "Renew" button fires; it is ~10s-rate-limited and
does not guarantee a *new* IP, so ``rebuild_circuits`` (close general circuits
+ ``new_circuit(await_build=True)``) is the reliable primary underneath it.
"""

from __future__ import annotations

import logging
import platform
import queue
import re
import shutil
import subprocess
import tarfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

# Shared loopback/process helpers live in core (portable, no vendor baggage).
# No reason to duplicate orphan-control code: tor.exe children are exactly the
# same "orphan holds the port" hazard as wireproxy.exe was, and the kill-on-close
# job arms itself idempotently regardless of which sibling spawned first.
from core import job
from core.egress_helpers import (
    _bindable, _find_free_port, _kill_pid, _pid_on_port, _port_is_open,
)

# Module-level logger so methods defaulting ``log=`` to it (instead of bare
# ``print``) route internal calls through ``uvicorn.error.info``. Manager
# methods like ``restart_lane`` invoke ``_launch_lane`` without an explicit
# ``log=``, and without this default those fall through to ``print()`` and
# dump unsanitised lines straight to stdout -- bypassing uvicorn's INFO
# pipeline (the "junk on startup" the user reported). Same ``uvicorn.error``
# root the daemon's own ``self._log`` is bound to, mirroring
# The manager owns N lanes on distinct loopback ports.
_log = logging.getLogger("uvicorn.error")

# The Tor Expert Bundle archive. The directory index lists version
# subdirectories (e.g. ``15.0.20/``); inside each there's a
# ``tor-expert-bundle-windows-x86_64-<version>.tar.gz`` whose top-level
# layout is ``tor/tor.exe`` (the binary) and ``data/geoip`` + ``data/geoip6``
# (sibling to ``tor/``, not next to the binary -- see ``_geoip_path``).
# We pick the highest 3-part version on the page -- the bundle tracks the
# Tor Browser release line and staying on latest keeps us on a maintained
# tor; no value in pinning to a specific version since the runtime flags
# guarantee they all expose ``Bootstrapped 100`` the same way.
#
# Historically these bundles lived under ``dist.torproject.org/torbrowser/``
# as ``.zip``; the Tor Project relocated them to the archive host and
# switched to ``.tar.gz``, so both the host and the unpacker have to match.
TOR_DIST_URL = "https://archive.torproject.org/tor-package-archive/torbrowser/"


def _stem() -> Any:
    """Lazily import stem + the submodules we touch. Returns the module or None.

    Importing stem eagerly at module load would make the gateway refuse to
    start when stem isn't installed. Every call site asks ``_stem()`` first
    and degrades to a "Tor unavailable" status when it returns None, so a
    missing stem never blocks the gateway.
    """
    try:
        import stem  # noqa: F401
        import stem.connection  # noqa: F401
        import stem.control  # noqa: F401
        import stem.process  # noqa: F401
        return stem
    except ImportError:
        return None


@dataclass
class TorLane:
    """One ``tor.exe`` process + its local SOCKS5 entry proxy.

    A single managed local SOCKS5 proxy on a fixed
    loopback port, with an optional tracked process handle. ``socks5h://``
    (remote DNS) is intentional -- the exit IP must be the lane's, not the
    host's, which plain ``socks5://`` does not guarantee.
    """
    index: int
    socks_port: int
    control_port: int
    exit_country: str
    data_dir: Path
    proxy_url: str
    process: Optional[subprocess.Popen] = None
    #: Filled by the health probe via check.torproject.org; surfaced in the
    #: dashboard so "5 lanes" truthfully reports 5 distinct IPs.
    exit_ip: str = ""
    #: Last time a circuit was BUILT or NEWNYM fired. Used by health to spot a
    #: lane that's up but no longer building circuits (stuck, not just slow).
    last_circuit_built_ts: float = 0.0
    #: Transient flag while a NEWNYM / rebuild is in flight -- drives the
    #: dashboard's 1s polling acceleration.
    renewing: bool = False
    #: True once launch_tor saw ``Bootstrapped 100`` for this lane.
    boot_ok: bool = False
    #: True while the health daemon is actively restarting or regenerating this
    #: lane (different from ``renewing``, which is the dashboard's NEWNYM /
    #: circuit-rebuild sweep). Surfaced in ``status()`` so the dashboard can
    #: show a "healing" chip -- distinguishes "down and being worked on" from
    #: plain down. Set/cleared with try/finally in ``TorHealthDaemon`` so an
    #: exception during heal doesn't leave the lane stuck "healing" forever.
    healing: bool = False
    #: True until the first probe on this lane has actually run (i.e. while the
    #: health daemon is still in warmup, or right after a restart/regenerate).
    #: A freshly-launched tor with an open
    #: SOCKS port has *not* been proven to carry real traffic, so the dashboard
    #: must show ``probing`` (we don't know yet) rather than the misleading
    #: ``up`` that ``port_open`` alone implies. Cleared by the health daemon
    #: once a real probe runs; re-raised alongside ``healing`` whenever the
    #: lane is restarted or fully regenerated.
    probing: bool = True
    #: Unix timestamp of the most recent health probe on this lane that
    #: ended with ``healthy=True``. Stays ``0.0`` until the lane has been
    #: verified at least once. The chip ladder uses ``verified_at > 0`` to
    #: distinguish the cold-start ``probing`` state (never been verified
    #: yet -- a real unknown) from the re-verifying state (was previously
    #: verified, then bounced via a tor.exe restart or NEWNYM freeze, and
    #: the next probe will re-confirm).
    #: one-to-one so the dashboard's chip ladder renders consistently for
    #: both pools.
    verified_at: float = 0.0
    #: First cycle (unix timestamp) at which this lane entered the
    #: "probing-but-not-yet-verified" state and stayed there. Debounce-set
    #: (``0.0 -> now`` on first probe-fail-with-probing-true); NOT bumped on
    #: subsequent heal/regenerate re-raises so the timer's wall-clock anchor
    #: is the lane's *transitions into failure*, not every restart attempt.
    #: Reset to 0 the moment the probing-clear gate (in
    #: ``tor/health.py::_health_check``) fires -- i.e. the lane went through
    #: a passing probe, or post-warmup a non-passing probe still let the
    #: gate decide ``probing=False``. Read by the heal loop's fast-fail
    #: branch: a lane stuck in probing-failed for longer than
    #: ``_FAST_FAIL_PROBING_S`` is sidelined without waiting for the rest
    #: of the heal ladder (3 cheap-restart cycles + 1st-regen + 1200s
    #: cooldown + 2 more regens) to converge on a permanently-blocked exit.
    #: Surfaced through ``status()`` as ``probing_for_seconds`` so the
    #: dashboard chip ladder can render "probing <N>m" with a warn color
    #: past the threshold -- a permanently-blocked lane now visibly climbs
    #: to "sidelined" within ~4-5 minutes instead of ~43.
    probing_since: float = 0.0
    #: Number of consecutive daemon cycles in which the lane's
    #: ``_health_check`` returned ``healthy=False``. Reset to 0 the moment a
    #: probe returns ``healthy=True``;``regenerate_lane`` resets don't touch
    #: it (the lane's blockage is unchanged across DataDirectory wipes -- a
    #: ``regenerate_lane`` that lands on a permanent-block exit shouldn't
    #: reset the counter, it should keep counting toward fast-fail). Read by
    #: the heal loop's fast-fail branch: ``unhealthy_cycles >=
    #: _FAST_FAIL_PROBING_CYCLES`` triggers an immediate sideline instead
    #: of waiting out the rest of the heal ladder's natural convergence
    #: (~3 cheap-restart cycles + 1st-``regenerate_lane`` + 1200 s cooldown
    #: + 2 more regens = ~43 minutes). Sized in lockstep with the
    #: ``check_interval`` (default 60 s) so the wall-clock fast-fail is
    #: ~4 minutes by default; tunable via
    #: ``LINGLING_TOR_FAST_FAIL_PROBING_CYCLES``. Surfaced through
    #: ``status()`` so operators can see "this lane's been unhealthy for
    #: N cycles" at a glance.
    unhealthy_cycles: int = 0
    #: Last sanctioned reachability verdict the health daemon wrote back here.
    #: Before any probe it stays ``None`` (no verdict yet) so a freshly-
    #: bootstrapped lane with a bound SOCKS port is *not* painted ``up`` off
    #: ``_is_running()`` alone -- the original dashboard's identity rack chip
    #: ladder used ``running`` as its only signal so a port-open-but-OpenCode-
    #: blocked Tor lane read ``up`` while every request through it failed.
    #: Surfaced through ``status()`` mirrors the daemon's ``healthy`` verdict
    #: so the same frontend chip ladder renders truthfully for both families.
    #: ``None`` (never probed) keeps the dashboard's chip on ``probing``
    #: instead of the old ``False``-default's misleading ``down``.
    healthy: Optional[bool] = None
    #: Last-known pool burn counters, frozen on pool eviction (mirrors
    #: the daemon's verdict). Tor lanes normally show ``exit_country`` in the
    #: mid cell instead of the request tally, so these matter less for the
    #: blank-cell fix than for keeping the chip ladder's rate-limit branch
    #: honest -- without the freeze, an evicted lane's counters flipped to
    #: 0 every cycle and the chip fell back to ``up``-off-``running``.
    consecutive_failures: int = 0
    total_429: int = 0
    #: Cross-cycle restart streak, bumped each cycle that ``_heal_lane``'s
    #: ``restart_lane`` reopens the SOCKS port but the post-restart probe
    #: through the tunnel still fails. Past the streak cap the heal loop
    #: escalates to a wholesale ``regenerate_lane`` (full DataDirectory wipe +
    #: consensus re-fetch + circuit rebuild) instead of cheap-restart
    #: looping forever. Reset
    #: the moment any cycle observes the lane healthy, so a transient quirk
    #: never poisons the streak.
    restart_attempts: int = 0
    #: Unix timestamp of the last ``regenerate_lane`` against this lane --
    #: gates the cooldown before the loop escalation re-rolls the
    #: DataDirectory, so a persistent block can't re-burn Tor's slow circuit
    #: bootstrap faster than once per cooldown. Sized in lockstep with the
    #: cooldown (the two pipelines mirror one another, so the constants stay
    #: close).
    last_regenerate_at: float = 0.0
    #: Circuit-breaker counter: bumped each time the escalate's wholesale
    #: ``regenerate_lane`` finished but the post-regen probe still came back
    #: unhealthy. Past ``MAX_REGEN_FAILURES`` the lane is sidelined (pulled
    #: from the pool, skipped by future heals) -- mirrors
    #: the lane's silhouette so the operator's "how broke is this
    #: lane?" mental model is the same on both families.
    regen_failures: int = 0
    #: Set by the daemon when ``regen_failures >= MAX_REGEN_FAILURES``; the
    #: lane is retired from the pool for the long cooldown so the next cycle
    #: does not re-burn Tor's slow regenerate on a known-burned lane. Held
    #: through the lifetime of the manager so the dashboard can show the
    #: truth (a sidelined slot is *not* a working one even if tor.exe is up).
    sidelined: bool = False
    #: Timestamp of the last sideline; the daemon uses ``time.time() -
    #: last_sideline_at >= SIDELINED_RECHECK_S`` as the gate to attempt a
    #: ``try_retry_sidelined`` re-probe. Cleared on a successful revive.
    last_sideline_at: float = 0.0
    #: Cross-cycle streak of "probe-passes but pool ledger says
    #: consecutive_failures is mostly 429". Distinct from
    #: ``consecutive_failures`` on the lane itself (which is mirrored from
    #: the pool and resets when pool cools down); this counter only
    #: advances when the heal-loop's destination-burn detector observes
    #: a clearly-burned persona EVEN THOUGH the lane passed its own
    #: ``_health_check`` probe. Each rotate cycle logs it and resets it,
    #: so the dashboard can show "burned Nx" and the operator can see
    #: when the lane has cycled through too many countries without the
    #: destination-side throttling catching up.
    destination_burn_count: int = 0
    #: Unix timestamp of the most recent destination-burn country rotation
    #: (``tor/health.py::_maybe_handle_destination_burn``). Gates the gap
    #: between successive rotates on the same lane via
    # ``LINGLING_TOR_DEST_BURN_ROTATE_COOLDOWN_S`` so a persistent per-
    # country burn can't re-burn Tor's slow circuit bootstrap faster than
    #: once per cooldown. ``0.0`` = never rotated yet.
    last_burn_rotate_at: float = 0.0

    def cookie_path(self) -> Path:
        return self.data_dir / "control_auth_cookie"

    def torrc_path(self) -> Path:
        return self.data_dir / "torrc"

    def _is_running(self) -> bool:
        """True if we're tracking a live tor.exe, or its SOCKS port is open.

        The port check handles
        externally-started tor (e.g. from a previous Lingling run whose
        process handle we lost on restart).
        """
        if self.process is not None and self.process.poll() is None:
            return True
        return _port_is_open("127.0.0.1", self.socks_port, timeout=0.1)

    def status(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "socks_port": self.socks_port,
            "control_port": self.control_port,
            "proxy_url": self.proxy_url,
            "exit_country": self.exit_country,
            "exit_ip": self.exit_ip,
            "running": self._is_running(),
            "control_alive": _port_is_open("127.0.0.1", self.control_port, timeout=0.2),
            "boot_ok": self.boot_ok,
            "last_circuit_built_ts": self.last_circuit_built_ts,
            "renewing": self.renewing,
            "healing": self.healing,
            "probing": self.probing,
            "verified_at": self.verified_at,
            # Wall-clock duration of the current probing-but-not-verified
            # window. ``0`` whenever ``probing`` is False (the lane has either
            # never been unhealthy, or has passed a probe and the timer was
            # reset on the latest clear). Surfaced so the dashboard's chip
            # ladder can show "probing 4m" with a warn color past the
            # fast-fail threshold instead of letting a permanently-blocked
            # exit sit on ``probing`` for ~43 minutes before the heal
            # ladder's circuit-breaker converges to ``sidelined``.
            "probing_since": self.probing_since,
            "probing_for_seconds": round(time.time() - self.probing_since, 1)
            if self.probing and self.probing_since > 0 else 0.0,
            # Cycle-based unhealthy counter -- the authoritative signal the
            # heal loop's fast-fail branch reads. Cumulative across the
            # entire unhealthy streak including across ``regenerate_lane``
            # re-rolls (the lane's *exit blockage*, not its tor state, is
            # what the gate is measuring); only a passing probe resets
            # it. Surfaced as ``unhealthy_for_seconds`` (computed against
            # the daemon cycle length) so the dashboard can render
            # ``unhealthy 4m+`` in a tabular cell alongside the chip.
            "unhealthy_cycles": self.unhealthy_cycles,
            "unhealthy_for_seconds": round(
                self.unhealthy_cycles * 60.0, 1),
            # Per-cycle reachability verdict + burn counters mirrored from
            # the daemon's transient ``results[i]`` dict (one-to-one with
            # the lane's status()). The dashboard's identity rack chip
            # ladder consults ``healthy`` so a port-open-but-tunnel-dead Tor
            # lane reads ``down`` honestly. ``consecutive_failures`` and
            # ``total_429`` freeze on pool eviction so the chip ladder keeps
            # showing the cause of the eviction instead of silently reverting
            # to zero and falling back to ``up`` off ``running``.
            "healthy": self.healthy,
            "consecutive_failures": self.consecutive_failures,
            "total_429": self.total_429,
            "restart_attempts": self.restart_attempts,
            "last_regenerate_at": self.last_regenerate_at,
            # Circuit-breaker trio surfaced for the dashboard chip ladder:
            # ``sidelined`` flips the slot to a permanent "sidelined" chip;
            # ``regen_failures`` is the running counter that eventually
            # triggers it; ``last_sideline_at`` powers the long-cooldown
            # revive.
            "regen_failures": self.regen_failures,
            # Destination-side burn counter (per-cycle streak of
            # "probe-passes but pool ledger says consecutive_failures is
            # mostly 429"). Surfaced so the dashboard's mid cell can
            # render ``burned Nx`` when the rotation path is active (the
            # pool-ledger burn-count is mirrored through
            # ``consecutive_failures`` already; this is the
            # *country-rotations* counter so the operator can see when
            # one lane has cycled through too many countries without
            # the destination-side throttling catching up).
            "destination_burn_count": self.destination_burn_count,
            # Wall-clock anchor of the most recent country rotation
            # along the destination-burn path. ``0.0`` = never rotated.
            # Powers an "age" pill on the dashboard (rendered only when
            # ``destination_burn_count > 0``) so the operator can see
            # the time since the last rotation and spot a stalled path
            # (rotation fired but pool ledger still shows 429s the next
            # cycle = the new country is also burned = next rotation
            # will hit the rotate cooldown gate).
            "last_burn_rotate_at": self.last_burn_rotate_at,
            "sidelined": self.sidelined,
            "last_sideline_at": self.last_sideline_at,
            "pid": self.process.pid if self.process else None,
        }


class TorManager:
    """Owns the lifecycle of N local tor-backed SOCKS5 proxies (Tor lanes).

    Each lane is a separate ``tor.exe`` pinned to a *different* exit country;
    lanes register into the shared ProxyPool under ``tor-N`` ids with labels
    like ``"Tor lane #N {cc}"``:
    ``setup_lanes`` (idempotent), ``start_all`` / ``stop_all``,
    ``restart_lane`` / ``regenerate_lane``, ``status``, ``urls_for_pool``,
    plus ``renew_circuit`` (NEWNYM) and ``rebuild_circuits`` (the reliable
    primary) for the dashboard's heal buttons.
    """

    def __init__(
        self,
        root_dir: Path,
        count: int = 5,
        exit_countries: Optional[List[str]] = None,
        socks_base: int = 52001,
        # Not 52101: Windows administratively excludes that block (Hyper-V/WSL
        # reserves 52093-52192 on many machines), and every lane then dies at
        # the ControlPort bind with "Failed to bind one of the listener ports".
        control_base: int = 52301,
        tor_exe: str = "",
        boot_timeout: int = 120,
        log=_log.info,
    ) -> None:
        self.root = Path(root_dir)
        self.tools_dir = self.root / "tools"
        self.lanes_dir = self.root / "lanes"
        self.count = max(1, count)
        # One country per lane, cycling through the configured list so every
        # lane still gets a *distinct* country even when count > countries.
        base = list(exit_countries) if exit_countries else ["us"]
        if not base:
            base = ["us"]
        self.countries = [base[i % len(base)] for i in range(self.count)]
        # Rotation pool is the *full, un-cycled* country list -- the
        # destination-burn rotation path (``next_unused_exit_country``/
        # ``rotate_exit_country``) draws from it so a lane whose country
        # persona just got burned can re-roll even when the active cohort
        # itself has fewer distinct countries than ``self.count``. E.g. a
        # 10-lane pool with the default
        # us,de,nl,fr,ro,gb,ca,se,pl,ch has *10* rotation candidates; a
        # 2-lane pool with the same base has *10 also* (vs the buggy
        # 2-country-cycled interpretation).
        self._rotation_countries: List[str] = list(base)
        self.socks_base = socks_base
        self.control_base = control_base
        self.tor_exe_override = tor_exe
        self.boot_timeout = boot_timeout
        self.log = log
        self.lanes: List[TorLane] = []
        self._lock = threading.Lock()
        # ``_in_flight_cond`` is a Condition (RLock underneath, so re-entrant
        # under the manager's own thread) gating ``stop_all``'s wait against
        # in-flight ``_launch_lane`` calls. ``_stopping`` flips under the same
        # lock so launchers either see it on the way in (and bail without
        # taking the counter) or get counted and gate-check again at their
        # stem-spawn checkpoint. The race this closes: a regen that kicked off
        # just before ``stop_all`` could enter ``stem.process.launch_tor_with_
        # config`` *while* we're killing lanes -- tor.exe spawns parentless on
        # our port forever (kill-on-close jobs miss it because stem hasn't
        # returned the Popen to us yet). The counter + wait lets stop_all hold
        # a brief 5s deadline for in-flight launches to commit-or-fail before
        # the port sweep reaps anything they leaked.
        self._in_flight_cond = threading.Condition()
        self._in_flight = 0
        self._stopping = False
        # Located by ensure_tools(); cached so repeated status() calls don't
        # re-walk the tools dir every cycle.
        self._tor_executable: Optional[Path] = None
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self.lanes_dir.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    # -- existing state -----------------------------------------------------
    def _load_existing(self) -> None:
        """Re-load lanes created in a previous run.

        Reads the actual SOCKS/Control ports + ExitNodes country back from each
        lane's torrc so a port migration or country override from a prior run
        survives restart, rather than resetting to ``base + index``.  Ports that
        are no longer bindable (Windows excluded ranges, stale collisions) are
        healed immediately via ``_find_free_port`` so a restart never replays a
        ``Failed to bind one of the listener ports`` (WSAEACCES) from a stale
        torrc.
        """
        seen_socks: set[int] = set()
        seen_control: set[int] = set()
        for i in range(self.count):
            exit_cc = self.countries[i]
            socks_port = self.socks_base + i
            control_port = self.control_base + i
            lane_dir = self.lanes_dir / f"tor-{i + 1}"
            torrc = lane_dir / "torrc"
            if torrc.exists():
                for line in torrc.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("SocksPort"):
                        try:
                            socks_port = int(line.split()[1].rsplit(":", 1)[-1])
                        except (ValueError, IndexError):
                            pass
                    elif line.startswith("ControlPort"):
                        try:
                            control_port = int(line.split()[1].rsplit(":", 1)[-1])
                        except (ValueError, IndexError):
                            pass
                    elif line.startswith("ExitNodes"):
                        try:
                            val = line.split(None, 1)[1].strip().strip("{}").strip()
                            if val:
                                exit_cc = val.lower()
                        except IndexError:
                            pass
            # Heal stale / excluded / colliding ports before the lane is created.
            # _bindable() is the real test: an excluded range is free per
            # _port_is_open but still WSAEACCES on bind, which is exactly the
            # "Failed to bind one of the listener ports" users see in tor.log.
            if socks_port in seen_socks or not _bindable(socks_port):
                try:
                    socks_port = _find_free_port(
                        self.socks_base + i, reserved=seen_socks,
                    )
                except RuntimeError:
                    pass  # keep original; _launch_lane will surface the bind error
            seen_socks.add(socks_port)
            if control_port in seen_control or not _bindable(control_port):
                try:
                    control_port = _find_free_port(
                        self.control_base + i, reserved=seen_control,
                    )
                except RuntimeError:
                    pass
            seen_control.add(control_port)
            lane = TorLane(
                index=i + 1,
                socks_port=socks_port,
                control_port=control_port,
                exit_country=exit_cc,
                data_dir=lane_dir,
                proxy_url=f"socks5h://127.0.0.1:{socks_port}",
            )
            # Persist healed ports immediately so the next restart doesn't re-read
            # the stale torrc.  Best-effort: a write failure shouldn't block boot.
            if torrc.exists():
                try:
                    current_socks = current_control = None
                    for line in torrc.read_text().splitlines():
                        s = line.strip()
                        if s.startswith("SocksPort"):
                            try:
                                current_socks = int(s.split()[1].rsplit(":", 1)[-1])
                            except (ValueError, IndexError):
                                pass
                        elif s.startswith("ControlPort"):
                            try:
                                current_control = int(s.split()[1].rsplit(":", 1)[-1])
                            except (ValueError, IndexError):
                                pass
                    if current_socks != socks_port or current_control != control_port:
                        self._write_torrc(lane)
                except Exception:
                    pass
            self.lanes.append(lane)

    # -- platform / tool helpers -------------------------------------------
    def _is_windows(self) -> bool:
        return platform.system() == "Windows"

    def _ext(self) -> str:
        return ".exe" if self._is_windows() else ""

    def _locate_tor_binary(self, root: Path) -> Optional[Path]:
        """Find ``tor.exe`` anywhere under ``root`` (expert bundle puts it in
        a ``tor/`` subdir; this also survives alternate layouts)."""
        if not root.exists():
            return None
        target = "tor.exe" if self._is_windows() else "tor"
        for p in root.rglob(target):
            return p
        return None

    def _tor_path(self) -> Path:
        """The tor executable to launch. Explicit override wins, then the
        located binary (cached), then a sensible default path."""
        if self.tor_exe_override:
            return Path(self.tor_exe_override)
        if self._tor_executable is not None and self._tor_executable.exists():
            return self._tor_executable
        located = self._locate_tor_binary(self.tools_dir)
        if located is not None:
            self._tor_executable = located
            return located
        # Not located yet: return the expected path so callers can .exists()
        # it; ensure_tools() will populate _tor_executable on success.
        return self.tools_dir / "tor" / ("tor" + self._ext())

    def _geoip_path(self) -> Optional[Path]:
        """Resolve the geoip database for the resolved tor.exe.

        The Tor Expert Bundle has shipped geoip in two layouts: legacy (next
        to ``tor.exe`` in the same ``tor/`` dir -- the old zip bundle) and
        current (a sibling ``data/`` dir with ``geoip`` + ``geoip6`` sitting
        next to ``torrc-defaults``). Try those, then walk the tools cache
        as a fallback for exotic layouts. Returns None if no geoip is
        found, in which case ``_lane_config_dict`` suppresses the
        ``ExitNodes`` pin -- the lane still routes via Tor (IP-diverse vs
        no country guarantee), and ``status()`` exposes
        ``geoip_available: False`` so the dashboard can show why.
        """
        tor = self._tor_path()
        for c in (
            # Current bundle layout: tor/tor.exe + data/geoip + data/geoip6.
            tor.parent.parent / "data" / "geoip",
            # Legacy bundle layout: everything next to tor.exe.
            tor.parent / "geoip",
        ):
            if c.is_file():
                return c
        for p in self.tools_dir.rglob("geoip"):
            if p.is_file():
                return p
        return None

    def _geoip6_path(self) -> Optional[Path]:
        """Resolves to ``geoip6`` next to wherever ``_geoip_path`` found
        geoip -- they're packaged together, so repeating the layout search
        is wasteful."""
        g = self._geoip_path()
        if g is None:
            return None
        candidate = g.parent / "geoip6"
        return candidate if candidate.is_file() else None

    def tools_ready(self) -> bool:
        return self._tor_path().exists()

    def stem_available(self) -> bool:
        return _stem() is not None

    def stem_ok(self) -> bool:
        return self.tools_ready() and self.stem_available()

    # -- tools: Tor Expert Bundle download ---------------------------------
    def ensure_tools(self, log=_log.info) -> Dict[str, Any]:
        """Locate or auto-download the Tor Expert Bundle. Returns a status dict.

        On Windows the expert bundle is fetched from dist.torproject.org and
        extracted in place. On POSIX we don't download -- the user points
        ``LINGLING_TOR_EXE`` at their distro's ``tor``. ``stem`` presence is reported
        separately so a missing library is visible without conflating it with a
        missing binary.
        """
        results: Dict[str, Any] = {"tor": None, "stem_available": self.stem_available(), "downloaded": []}

        located = self._locate_tor_binary(self.tools_dir)
        if located and located.exists():
            self._tor_executable = located
            results["tor"] = str(located)
            return results

        if self.tor_exe_override:
            p = Path(self.tor_exe_override)
            if p.exists():
                self._tor_executable = p
                results["tor"] = str(p)
                return results
            results["error"] = f"override tor_exe not found: {p}"
            return results

        if not self._is_windows():
            results["error"] = ("tor binary not found and auto-download is "
                                "Windows-only; set LINGLING_TOR_EXE to your tor")
            return results

        log("tor: cooking Tor Expert Bundle (one-time, ~1-2 min) ...")
        try:
            self._download_tor_expert_bundle()
        except Exception as exc:  # noqa: BLE001
            results["error"] = f"download failed: {exc}"
            return results

        located = self._locate_tor_binary(self.tools_dir)
        if located:
            self._tor_executable = located
            results["tor"] = str(located)
            results["downloaded"].append("tor")
        else:
            results["error"] = "download finished but tor.exe was not found inside it"
        return results

    def _download_tor_expert_bundle(self) -> None:
        import urllib.request

        with urllib.request.urlopen(TOR_DIST_URL, timeout=30) as r:
            listing = r.read().decode("utf-8", "replace")
        versions = re.findall(r'href="(\d+\.\d+\.\d+)/"', listing)
        if not versions:
            raise RuntimeError("could not parse any Tor versions from the archive")

        def vkey(v: str):
            return tuple(int(x) for x in v.split("."))

        latest = max(versions, key=vkey)
        # The archive serves the bundle as ``.tar.gz`` (it was ``.zip`` on the
        # old dist.torproject.org host). Top-level layout once unpacked is
        # ``tor/tor.exe`` + ``data/geoip`` + ``data/geoip6`` -- see
        # ``_geoip_path`` for how we resolve the geoip files.
        archive_name = f"tor-expert-bundle-windows-x86_64-{latest}.tar.gz"
        url = f"{TOR_DIST_URL}{latest}/{archive_name}"
        tmp = self.tools_dir / archive_name
        urllib.request.urlretrieve(url, tmp)
        with tarfile.open(tmp, "r:gz") as tf:
            tf.extractall(self.tools_dir)
        tmp.unlink(missing_ok=True)

    # -- per-lane torrc ------------------------------------------------------
    def _lane_config_dict(self, lane: TorLane) -> Dict[str, str]:
        """The torrc config dict for one lane. Also serialised to disk so a
        restart can read the exact ports/country back (see ``_load_existing``)."""
        cfg = {
            "SocksPort": f"127.0.0.1:{lane.socks_port}",
            "ControlPort": f"127.0.0.1:{lane.control_port}",
            "DataDirectory": str(lane.data_dir),
            "CookieAuthentication": "1",
            "CookieAuthFile": str(lane.cookie_path()),
            # StrictNodes + disjoint ExitNodes is what guarantees a distinct
            # country (and therefore a distinct exit IP) per lane. Without
            # StrictNodes tor would happily use any exit that won the race.
            "StrictNodes": "0",
            "MaxCircuitDirtiness": "600",
            "CircuitBuildTimeout": "60",
            "RunAsDaemon": "0",
            "Log": f"notice file {str(lane.data_dir / 'tor.log')}",
        }
        # ExitNodes country pinning needs geoip files to resolve the codes.
        # The expert bundle ships them next to tor.exe; without them a lane
        # would fail to build any StrictNodes circuit, so we only pin when
        # geoip is present and let the lane run as a generic (un-pinned) Tor
        # exit otherwise -- still IP-diverse, just not country-pinned.
        geoip = self._geoip_path()
        if geoip is not None:
            cfg["GeoIPFile"] = str(geoip)
            geo6 = self._geoip6_path()
            if geo6 is not None:
                cfg["GeoIPv6File"] = str(geo6)
            cfg["ExitNodes"] = "{" + lane.exit_country + "}"
        return cfg

    def _write_torrc(self, lane: TorLane) -> None:
        """Persist the lane's torrc. Used both by setup_lanes and as the
        reload source for ``_load_existing`` on the next start."""
        cfg = self._lane_config_dict(lane)
        lines = ["# Auto-generated by Lingling TorManager. Do not edit by hand."]
        for k, v in cfg.items():
            lines.append(f"{k} {v}")
        # Ensure the lane's data dir exists. ``setup_lanes`` does this on its
        # own path, but ``_launch_lane`` falls back to here when the torrc is
        # missing (e.g. a first start that skipped /api/tor/setup), and a bare
        # ``write_text`` against ``lanes/tor-N/`` with no parent dir crashes
        # with FileNotFoundError -- masking the real "couldn't launch tor"
        # error in the start_all detail record.
        lane.data_dir.mkdir(parents=True, exist_ok=True)
        lane.torrc_path().write_text("\n".join(lines) + "\n")

    # -- setup (idempotent) -------------------------------------------------
    def setup_lanes(self, log=_log.info) -> Dict[str, Any]:
        """Write per-lane torrc + ensure data dirs exist. Idempotent.

        Does NOT launch tor -- call ``start_all`` for that. Downloads tor on
        first call so the GeoIPFile path can be resolved for the ExitNodes
        pin. A missing ``stem`` or tor binary is reported, not raised, so
        the gateway still serves directly.
        """
        if not self.stem_available():
            log("tor: stem's not installed -- Tor lanes sit this one out")
        # Resolve the tor binary so GeoIPFile is known before we write torrc --
        # this is where the one-time expert-bundle download happens.
        self.ensure_tools(log=log)
        results: List[Dict[str, Any]] = []
        for lane in self.lanes:
            try:
                lane.data_dir.mkdir(parents=True, exist_ok=True)
                self._write_torrc(lane)
                results.append({
                    "index": lane.index,
                    "status": "configured",
                    "socks_port": lane.socks_port,
                    "exit_country": lane.exit_country,
                })
            except Exception as exc:  # noqa: BLE001
                results.append({"index": lane.index, "status": "error", "error": str(exc)})
        return {
            "results": results,
            "tor_detected": self.tools_ready(),
            "stem_available": self.stem_available(),
        }

    # -- process lifecycle --------------------------------------------------
    def start_all(self, log=_log.info) -> Dict[str, Any]:
        """Launch a tor.exe for every lane. Lanes bootstrap in parallel.

        stem's ``launch_tor_with_config`` blocks per-lane until bootstrap (or
        timeout), so serial launch would be N x boot time. With 5 lanes that
        can be minutes; parallel threads cut it to ~one boot time.

        Returns the same shape as the manager's ``start_all``: started/skipped/failed +
        the manager's status. A missing stem or tor binary is a graceful
        "couldn't start" rather than an exception, so the bootstrap thread
        doesn't tear down the gateway.
        """
        # A prior ``stop_all`` flipped ``_stopping`` (and counted any in-flight
        # launches drain). A subsequent ``start_all`` is the contract for "back
        # in business" -- reset both so the per-lane launch threads can spawn
        # stem again.
        with self._in_flight_cond:
            self._stopping = False
            self._in_flight = 0
        stem = _stem()
        if stem is None:
            return {
                "started": False, "skipped": 0, "failed": 0,
                "stem_available": False, "reason": "stem not installed",
                "details": [], **self.status(),
            }
        self.ensure_tools(log=log)
        if not self.tools_ready():
            return {
                "started": False, "skipped": 0, "failed": 0,
                "stem_available": True,
                "reason": "tor binary not found (set LINGLING_TOR_EXE or run setup)",
                "details": [], **self.status(),
            }

        from concurrent.futures import ThreadPoolExecutor, as_completed
        started = skipped = failed = 0
        details: List[Dict[str, Any]] = []
        max_workers = min(len(self.lanes), 5)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._launch_lane, lane, log): lane for lane in self.lanes}
            for fut in as_completed(futures):
                lane = futures[fut]
                try:
                    detail = fut.result()
                    if detail["status"] == "started":
                        started += 1
                        lane.boot_ok = True
                    elif detail["status"] == "already_running":
                        skipped += 1
                    else:
                        failed += 1
                    details.append(detail)
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    details.append({"index": lane.index, "status": "failed", "error": str(exc)})

        return {
            "started": started, "skipped": skipped, "failed": failed,
            "stem_available": True, "details": details, **self.status(),
        }

    def _launch_lane(self, lane: TorLane, log=_log.info) -> Dict[str, Any]:
        """Launch one tor.exe via stem, wait for its SOCKS port, arm the job.

        Raises on failure so the caller (start_all via ThreadPoolExecutor) can
        record a failed detail without taking down the whole batch.
        """
        if lane._is_running():
            return {"index": lane.index, "status": "already_running", "socks_port": lane.socks_port}

        # Atomic gate: stop-flag check + in-flight inc under the same lock, so a
        # concurrent ``stop_all`` flipping ``_stopping`` either sees no in-flight
        # work (and proceeds to wait) or sees ours already counted. Both sides
        # of the race close: either launch bails here, or it commits and the
        # ``finally`` clause at the bottom will notify so stop_all's ``wait``
        # resumes and its sweep reaps whatever the launcher just spawned. The
        # ``with self._in_flight_cond:` re-entrant lock (RLock) makes the nested
        # second-checkpoint block at line ~728 below safe to take again.
        with self._in_flight_cond:
            if self._stopping:
                log("tor: launch lane #%d: bailing -- pool tearing down", lane.index)
                return {"index": lane.index, "status": "skipped_stopping"}
            self._in_flight += 1
        try:
            if not lane.torrc_path().exists():
                self._write_torrc(lane)

            # Evict orphans on both ports (a tor.exe from a previous Lingling run
            # that the kill job missed.
            for port in (lane.socks_port, lane.control_port):
                if _port_is_open("127.0.0.1", port):
                    pid = _pid_on_port(port)
                    if pid:
                        _kill_pid(pid, grace_s=2)
                        time.sleep(0.2)

            # Second checkpoint: bail right before stem spawns tor.exe. ``stop_all``
            # may have arrived during the heavy pre-prep above; the tor.exe stem is
            # about to launch would be parentless the instant it boots (stem hasn't
            # returned the Popen yet, so neither our kill-on-close job nor
            # ``stop_all``'s ``inst.process`` sweep can reach it).
            with self._in_flight_cond:
                if self._stopping:
                    log("tor: launch lane #%d bailing -- pool tearing down before stem spawn",
                        lane.index)
                    return {"index": lane.index, "status": "skipped_stopping"}

            stem = _stem()
            config = self._lane_config_dict(lane)

            def _msg(line: str) -> None:
                # stem invokes this once per boot notice; capture the moment the
                # lane finished bootstrapping so health can spot a stuck lane.
                if "Bootstrapped 100" in line:
                    lane.last_circuit_built_ts = time.time()

            log("tor: cooking lane #%d (%s) on 127.0.0.1:%d ...",
                lane.index, lane.exit_country, lane.socks_port)
            try:
                lane.process = self._launch_tor_process(stem, lane, config, _msg)
            except Exception as exc:
                lane.boot_ok = False
                # Self-heal the classic Windows excluded-range bind failure:
                # "Failed to bind one of the listener ports" means the torrc's
                # SocksPort/ControlPort is in a Windows-administered exclusion
                # (Hyper-V/WSL) or collides with another process.  Retry once on
                # fresh bindable ports so one stale torrc doesn't permanently
                # brick the lane.
                msg = str(exc)
                if "Failed to bind one of the listener ports" in msg:
                    log("tor: lane #%d bind failed on %d/%d -- re-rolling ports and retrying",
                        lane.index, lane.socks_port, lane.control_port)
                    # Evict whatever holds the old ports, then pick new ones.
                    for port in (lane.socks_port, lane.control_port):
                        pid = _pid_on_port(port)
                        if pid:
                            _kill_pid(pid, grace_s=1)
                    try:
                        taken_socks = {l.socks_port for l in self.lanes if l is not lane}
                        taken_control = {l.control_port for l in self.lanes if l is not lane}
                        lane.socks_port = _find_free_port(self.socks_base + lane.index - 1, reserved=taken_socks)
                        lane.control_port = _find_free_port(self.control_base + lane.index - 1, reserved=taken_control)
                        lane.proxy_url = f"socks5h://127.0.0.1:{lane.socks_port}"
                        self._write_torrc(lane)
                        config = self._lane_config_dict(lane)
                        log("tor: lane #%d retrying on %d/%d ...", lane.index, lane.socks_port, lane.control_port)
                        lane.process = self._launch_tor_process(stem, lane, config, _msg)
                    except Exception:
                        raise
                else:
                    raise
            # Belt + braces: the stem ownership signals tor to exit when our PID
            # dies, but on Windows a window-close kills the Python process before
            # tor can react, so the kill-on-close job is the actual backstop.
            # Idempotent + safe to call per lane.
            job.ensure_kill_job()
            if lane.process is not None:
                try:
                    if not job.assign(lane.process.pid):
                        log("tor: heads-up -- lane #%d tor.exe couldn't join the kill-job, will orphan on exit", lane.index)
                except Exception:  # noqa: BLE001
                    pass

            # launch_tor should have blocked until bootstrap, but give the SOCKS
            # listener a moment to materialise before declaring success.
            for _ in range(20):
                if _port_is_open("127.0.0.1", lane.socks_port, timeout=0.5):
                    break
                time.sleep(0.5)
            if not _port_is_open("127.0.0.1", lane.socks_port, timeout=0.5):
                raise RuntimeError(f"tor lane #{lane.index} SOCKS port {lane.socks_port} did not open")
            lane.boot_ok = True
            return {"index": lane.index, "status": "started", "socks_port": lane.socks_port,
                    "pid": lane.process.pid if lane.process else None}
        finally:
            # Mark our launch slot free + wake stop_all (whose 5s wait window
            # wants to see _in_flight drop to 0 before tearing everything down).
            with self._in_flight_cond:
                if self._in_flight > 0:
                    self._in_flight -= 1
                self._in_flight_cond.notify_all()

    # -- process launch watchdog -------------------------------------------
    # ``stem.process.launch_tor_with_config`` accepts a ``timeout`` argument,
    # but its implementation uses SIGALRM -- POSIX main-thread only. stem's
    # own source raises ``OSError("You cannot launch tor with a timeout on
    # Windows")`` for any non-default timeout on Windows, and silently no-ops
    # the timeout whenever the caller isn't in the main thread (which is
    # always our case: ``start_all`` uses a ThreadPoolExecutor, and
    # ``/api/tor/start`` runs in Starlette's anyio workers).
    #
    # So we never pass ``timeout`` to stem. Instead we run stem's launcher in
    # a daemon thread and race it against ``self.boot_timeout``: if tor
    # hasn't bootstrapped in time we kill the booting tor.exe so stem (blocked
    # reading tor's stdout pipe) raises promptly and the launcher thread
    # resolves -- without that kill the worker would leak forever holding the
    # port. The kill is found by port, not Popen, because stem hasn't returned
    # the Popen to us yet.
    def _launch_tor_process(self, stem, lane: TorLane, config: Dict[str, str],
                           msg_handler) -> Any:
        """Launch tor.exe via stem under our own watchdog timeout. Returns the
        ``subprocess.Popen`` stem yields on bootstrap success; raises
        ``RuntimeError`` with a lane-indexed message on timeout or launch
        failure (the caller records the failed detail rather than aborting
        the whole ``start_all`` batch)."""
        result_q: "queue.Queue" = queue.Queue()

        def _do_launch() -> None:
            try:
                proc = stem.process.launch_tor_with_config(
                    config=config,
                    tor_cmd=str(self._tor_path()),
                    init_msg_handler=msg_handler,
                    take_ownership=True,
                    close_output=False,
                )
                result_q.put(("ok", proc))
            except BaseException as exc:  # noqa: BLE001 -- surfaced via queue
                result_q.put(("err", exc))

        threading.Thread(
            target=_do_launch, daemon=True,
            name=f"tor-launch-{lane.index}").start()

        kind, payload = self._await_launch(result_q, lane)
        if kind == "err":
            raise RuntimeError(
                f"tor lane #{lane.index} launch failed: {payload}")
        return payload

    def _await_launch(self, result_q: "queue.Queue", lane: TorLane):
        """Block on the launcher thread's result, killing the booting tor.exe
        if our watchdog fires first. Returns ``(kind, payload)`` once stem
        has returned (``kind`` is ``"ok"`` for a Popen, ``"err"`` for the
        exception stem raised)."""
        try:
            return result_q.get(timeout=self.boot_timeout)
        except queue.Empty:
            pass

        # Watchdog won the race. SOCKS coming up *just before* the timeout is
        # a real race -- the listener frequently precedes the "Bootstrapped
        # 100" notice stem waits for, so a healthy tor that booted in the
        # last instant must not be killed. Let stem finish naturally then.
        if _port_is_open("127.0.0.1", lane.socks_port, timeout=0.2):
            try:
                return result_q.get(timeout=10)
            except queue.Empty:
                self._kill_booting_tor(lane)
                raise RuntimeError(
                    f"tor lane #{lane.index} SOCKS up but stem didn't return "
                    f"within {self.boot_timeout + 10}s")

        # SOCKS still closed: tor is genuinely stuck. Kill it so stem returns
        # (its stdout read hits EOF), then surface a clear timeout error.
        self._kill_booting_tor(lane)
        try:
            kind, payload = result_q.get(timeout=5)
        except queue.Empty:
            raise RuntimeError(
                f"tor lane #{lane.index} did not bootstrap within "
                f"{self.boot_timeout}s")
        if kind == "err":
            raise RuntimeError(
                f"tor lane #{lane.index} failed to bootstrap within "
                f"{self.boot_timeout}s: {payload}")
        return kind, payload

    def _kill_booting_tor(self, lane: TorLane) -> None:
        """Terminate a tor.exe that's still bootstrapping (we have no Popen
        yet -- stem is blocked inside ``launch_tor_with_config`` reading the
        process's stdout pipe). The surest way to make stem return is to kill
        the process; tor binds both its SOCKS + control listeners long before
        "Bootstrapped 100", so finding it by port works even mid-bootstrap."""
        for port in (lane.control_port, lane.socks_port):
            pid = _pid_on_port(port)
            if pid:
                _kill_pid(pid, grace_s=1)

    def stop_all(self) -> Dict[str, Any]:
        """Stop all tracked tor.exe processes + evict orphans from our ports."""
        # Tell in-flight launches we're tearing down and give them up to 5s to
        # either spawn+register (which our sweep below then reaps) or bail at
        # their second checkpoint (which is the whole point of the gate). The
        # wait is bounded because stem's launch is 10-30s for bootstrap -- we
        # can't block forever on a thread that's happily building a circuit --
        # and the orphan-by-port sweep below reaps anything that escaped past
        # the deadline anyway.
        with self._in_flight_cond:
            self._stopping = True
            deadline = time.time() + 5.0
            while self._in_flight > 0:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._in_flight_cond.wait(timeout=remaining)
        stopped = 0
        for lane in self.lanes:
            if lane.process is not None and lane._is_running():
                try:
                    # stem spawns tor without CREATE_NEW_PROCESS_GROUP, so
                    # CTRL_BREAK_EVENT isn't deliverable; terminate() works
                    # on both platforms (TerminateProcess on Windows).
                    lane.process.terminate()
                    lane.process.wait(timeout=5)
                    stopped += 1
                except Exception:  # noqa: BLE001
                    try:
                        lane.process.kill()
                        stopped += 1
                    except Exception:  # noqa: BLE001
                        pass
            lane.process = None
            lane.boot_ok = False

        # Evict orphan tor.exe still holding one of our ports (e.g. a previous
        # Lingling run whose kill job never armed on this restart).
        for lane in self.lanes:
            for port in (lane.socks_port, lane.control_port):
                if _port_is_open("127.0.0.1", port):
                    pid = _pid_on_port(port)
                    if pid:
                        _kill_pid(pid, grace_s=2)
                        stopped += 1

        # Fallback sweep on Windows: refresh should never leave stray tor.exe.
        if self._is_windows():
            try:
                subprocess.run(["taskkill", "/F", "/IM", "tor.exe"],
                               capture_output=True, text=True, timeout=10)
            except Exception:  # noqa: BLE001
                pass

        # Let the OS reclaim the ports before a subsequent start_all().
        time.sleep(0.3)
        return {"stopped": stopped, **self.status()}

    # -- single-lane lifecycle ---------------------------------------------
    def restart_lane(self, lane: TorLane, log=_log.info) -> bool:
        """Restart tor.exe for a single lane. Returns True once its SOCKS port
        reopens."""
        # Early bail: a tearing-down pool shouldn't start a new tor.exe with
        # fresh circuits. ``_launch_lane`` has the same check at its top (and a
        # second before stem) -- this short-circuits the heavy pre-prep the
        # caller (e.g. ``restart_lane`` daemon thread) would do otherwise.
        with self._in_flight_cond:
            if self._stopping:
                log("tor: restart_lane #%d: bailing -- pool tearing down", lane.index)
                return False
        self._stop_lane_process(lane)
        for port in (lane.socks_port, lane.control_port):
            if _port_is_open("127.0.0.1", port):
                pid = _pid_on_port(port)
                if pid:
                    _kill_pid(pid, grace_s=2)
                time.sleep(0.2)
        if not lane.torrc_path().exists():
            self._write_torrc(lane)
        try:
            detail = self._launch_lane(lane, log=log)
            return detail.get("status") == "started"
        except Exception as exc:  # noqa: BLE001
            log("tor: restart_lane #%d flamed out: %s", lane.index, exc)
            return False

    def regenerate_lane(self, lane: TorLane, log=_log.info) -> bool:
        """Fully regenerate a lane: wipe its DataDirectory, write a fresh torrc
        (re-rolling country pin + ports), and restart tor. Mirrors
        the manager's regenerate path.

        Wiping the DataDirectory is the Tor way to "burn an exit": it drops
        cached directory info and the auth_cookie, so the next tor starts from
        scratch and builds new circuits through new guards/exits.
        """
        log("tor: re-cooking lane #%d from scratch ...", lane.index)
        # Early bail: same reason as ``restart_lane``. Saves the rmtree +
        # port re-roll + torrc rewrite + stem launch this would do on the way
        # out -- 30+ seconds of waste work that's about to be killed by
        # ``stop_all``'s sweep anyway.
        with self._in_flight_cond:
            if self._stopping:
                log("tor: regenerate_lane #%d: bailing -- pool tearing down", lane.index)
                return False
        self._stop_lane_process(lane)
        for port in (lane.socks_port, lane.control_port):
            if _port_is_open("127.0.0.1", port):
                pid = _pid_on_port(port)
                if pid:
                    _kill_pid(pid, grace_s=2)

        if lane.data_dir.exists():
            shutil.rmtree(lane.data_dir)
        lane.data_dir.mkdir(parents=True, exist_ok=True)
        lane.exit_ip = ""
        lane.last_circuit_built_ts = 0.0
        lane.renewing = False
        lane.boot_ok = False

        # Re-roll ports away from siblings'. A sibling that happens to be down
        # still owns its loopback port; taking it would give two lanes the same
        # listener. Socks and control ranges are 100 apart so they're disjoint
        # and reserved independently.
        try:
            socks_taken = {l.socks_port for l in self.lanes if l is not lane}
            control_taken = {l.control_port for l in self.lanes if l is not lane}
            lane.socks_port = _find_free_port(
                self.socks_base + lane.index - 1, reserved=socks_taken)
            lane.control_port = _find_free_port(
                self.control_base + lane.index - 1, reserved=control_taken)
            lane.proxy_url = f"socks5h://127.0.0.1:{lane.socks_port}"
        except RuntimeError:
            pass  # leave ports alone; sibling ports are full -- rare

        self._write_torrc(lane)
        return self.restart_lane(lane, log=log)

    def _stop_lane_process(self, lane: TorLane) -> None:
        if lane.process is None:
            return
        try:
            lane.process.terminate()
            lane.process.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                lane.process.kill()
            except Exception:  # noqa: BLE001
                pass
        lane.process = None
        lane.boot_ok = False

    # -- circuit control (NEWNYM quick-heal + reliable rebuild) -------------
    def renew_circuit(self, lane: TorLane, log=_log.info) -> Dict[str, Any]:
        """NEWNYM quick-heal (``SIGNAL NEWNYM``).

        ~10s-rate-limited *per tor process* and does NOT guarantee a new exit
        IP -- tor's spec is explicit about that. It's the sweep the dashboard
        "Renew" button fires for an immediate refresh; the reliable primary is
        ``rebuild_circuits`` below. Returns a small status dict.
        """
        if lane.renewing:
            return {"ok": False, "reason": "already_renewing", "index": lane.index}
        stem = _stem()
        if stem is None:
            return {"ok": False, "reason": "stem unavailable", "index": lane.index}
        if not _port_is_open("127.0.0.1", lane.control_port, timeout=0.3):
            return {"ok": False, "reason": "control port dead", "index": lane.index}

        lane.renewing = True
        try:
            from stem import Signal
            from stem.control import Controller
            import stem.connection

            with Controller.from_port(port=lane.control_port) as controller:
                stem.connection.authenticate_cookie(
                    controller, str(lane.cookie_path()))
                wait = controller.get_newnym_wait()
                if wait and wait > 0:
                    return {"ok": False, "reason": "rate_limited",
                            "wait_s": float(wait), "index": lane.index}
                controller.signal(Signal.NEWNYM)
                lane.last_circuit_built_ts = time.time()
                log("tor: lane #%d got a NEWNYM kick", lane.index)
                return {"ok": True, "wait_s": 0.0, "index": lane.index,
                        "exit_country": lane.exit_country}
        except Exception as exc:  # noqa: BLE001
            log("tor: lane #%d renew flamed out: %s", lane.index, exc)
            return {"ok": False, "reason": str(exc), "index": lane.index}
        finally:
            lane.renewing = False

    def rebuild_circuits(self, lane: TorLane, log=_log.info) -> Dict[str, Any]:
        """Reliable heal: close general circuits + build a fresh one with
        ``await_build=True``. Unlike NEWNYM this blocks until a new circuit is
        BUILT, so the next request actually leaves through a rebuilt path.
        """
        if lane.renewing:
            return {"ok": False, "reason": "already_renewing", "index": lane.index}
        stem = _stem()
        if stem is None:
            return {"ok": False, "reason": "stem unavailable", "index": lane.index}
        if not _port_is_open("127.0.0.1", lane.control_port, timeout=0.3):
            return {"ok": False, "reason": "control port dead", "index": lane.index}

        lane.renewing = True
        try:
            from stem.control import Controller
            import stem.connection

            with Controller.from_port(port=lane.control_port) as controller:
                stem.connection.authenticate_cookie(
                    controller, str(lane.cookie_path()))
                closed = 0
                for circ in controller.get_circuits():
                    # Skip internal / one-hop (controller) circuits: closing
                    # them risks our own control channel. ``build_flags`` is a
                    # frozenset of strings like {"FAST", "GUARD"} -- internal
                    # ones carry "INTERNAL" or "ONEHOP_TUNNEL".
                    flags = set(circ.build_flags or ())
                    if flags & {"INTERNAL", "ONEHOP_TUNNEL"}:
                        continue
                    try:
                        controller.close_circuit(circ.id)
                        closed += 1
                    except Exception:  # noqa: BLE001
                        pass
                new_id = controller.new_circuit(await_build=True)
                lane.last_circuit_built_ts = time.time()
                lane.exit_ip = ""  # probe will refill on the next health cycle
                log("tor: lane #%d rebuilt %d circuits, new=%s", lane.index, closed, new_id)
                return {"ok": True, "closed": closed, "new_circuit": new_id,
                        "index": lane.index, "exit_country": lane.exit_country}
        except Exception as exc:  # noqa: BLE001
            log("tor: lane #%d rebuild flamed out: %s", lane.index, exc)
            return {"ok": False, "reason": str(exc), "index": lane.index}
        finally:
            lane.renewing = False

    # -- introspection -----------------------------------------------------
    def status(self) -> Dict[str, Any]:
        with self._lock:
            running = sum(1 for l in self.lanes if l._is_running())
            stem_ok = self.stem_available()
            tor_ok = self.tools_ready()
            return {
                "count": self.count,
                "tor_ready": tor_ok and stem_ok,
                "stem_available": stem_ok,
                "tor_binary": str(self._tor_path()) if tor_ok else None,
                "geoip_available": self._geoip_path() is not None,
                "lanes_running": running,
                "socks_base_port": self.socks_base,
                "control_base_port": self.control_base,
                "exit_countries": list(self.countries),
                "proxy_urls": [l.proxy_url for l in self.lanes if l._is_running()],
                "lanes": [l.status() for l in self.lanes],
            }

    def urls_for_pool(self) -> List[str]:
        """The socks5h:// URLs to register into Lingling's ProxyPool."""
        return [l.proxy_url for l in self.lanes if l._is_running()]

    def next_unused_exit_country(self, lane: TorLane) -> Optional[str]:
        """Pick an exit country from ``self.countries`` that no *active* lane
        currently uses and that is also different from the candidate's own
        current ``exit_country``, for the destination-burn rotation path.

        Returns ``None`` only if there is no country different from the
        candidate's own -- i.e. the pool is a single country and there is
        genuinely nowhere to rotate to.

        The heal daemon calls this when it detects a destination-side burn
        (lane's probe passes ``check.torproject.org`` fine but its pool
        ledger is recording consecutive 429s from the real egress target).
        Re-rolling within the same country is wasted Tor bootstrap -- the
        destination has the country persona throttled, not just one exit IP --
        so we cycle to a different country. The next regenerate carries the
        new ``ExitNodes {cc}`` pin and Tor picks a fresh exit IP from the
        replacement country.

        An unused country is preferred, but when every configured country is
        already in use (the common case: ``count`` lanes over ``count``
        countries) we still rotate to a *different* occupied country rather
        than hold forever. The regenerated circuit exits through a different
        relay than the burned one -- a new IP persona, which is what the
        destination is throttling -- and the sister lane already on that
        country is unaffected (Tor pins per-lane circuits, not per-country
        singletons).

        Note: this deliberately does NOT consult ``sidelined`` lanes'
        previous countries -- once a lane has been sidelined and rotated out
        of the pool, its previous country is fair game again on the next
        revival, exactly because the burn is destination-side and a long
        pause usually clears it.
        """
        in_use = {
            l.exit_country for l in self.lanes
            if l is not lane and not getattr(l, "sidelined", False)
        }
        # Draw from the full, un-cycled ``_rotation_countries`` rather
        # than the per-lane ``self.countries`` (which is only as long as
        # ``self.count`` and gives no room to rotate when count >=
        # distinct countries).
        candidates = [
            c for c in self._rotation_countries
            if c not in in_use and c != lane.exit_country
        ]
        if candidates:
            return candidates[0]
        # All countries occupied: fall back to any country different from
        # the candidate's own. A fresh circuit still yields a new exit IP
        # persona, which is what the destination is throttling.
        fallback = [
            c for c in self._rotation_countries if c != lane.exit_country
        ]
        return fallback[0] if fallback else None

    def rotate_exit_country(self, lane: TorLane) -> Optional[str]:
        """Mutate ``lane.exit_country`` to a different unused country from
        ``self.countries``. Returns the new country, or ``None`` if every
        country is already in use (the heal daemon then backs off rather
        than regenerate onto the same persona).

        Called from the destination-burn branch in
        ``TorHealthDaemon._check_and_heal`` before
        ``regenerate_lane``; the next torrc write pulls the new country
        through ``_lane_config_dict``'s ``ExitNodes`` line so the regenerated
        Tor process opens circuits in the replacement country only. Doesn't
        start a regenerate itself -- the caller decides whether to fire one
        immediately or wait for the next cycle's escalate.
        """
        new_cc = self.next_unused_exit_country(lane)
        if new_cc is None:
            return None
        lane.exit_country = new_cc
        return new_cc

    # -- circuit-breaker revival -------------------------------------------
    def try_retry_sidelined(self, lane: TorLane, pool: Any, log=_log.info) -> bool:
        """Long-cooldown revival attempt: re-probe a sidelined Tor lane.

        Called from ``TorHealthDaemon._maybe_unsideline_stale`` once
        ``time.time() - lane.last_sideline_at >= SIDELINED_RECHECK_S``. The
        re-probe runs first (cheap tunnel CONNECT only -- no regenerate):
        if the upstream blockage has lifted the lane is un-sidelined and
        re-registered in ``pool``; otherwise the sideline stamp stays put
        and ``last_sideline_at`` is bumped to now so the lane waits another
        full cooldown. Returns True on a successful revive, False otherwise.
        No-op when the lane was never sidelined.
        """
        if not lane.sidelined:
            return True
        # Lazy imports dodge the top-level cycle (``tor.health`` imports the
        # manager; the daemon, which calls this method, has already loaded
        # ``tor.health`` so the probe + RECHECK constants are in place by the
        # time we reach this body).
        from core.egress_helpers import _socks5_http_probe
        from tor.health import _probe_url, TOR_HTTP_PROBE_TIMEOUT, SIDELINED_RECHECK_S
        proxy_ok = False
        try:
            proxy_ok = _socks5_http_probe(
                _probe_url(lane.proxy_url), timeout=TOR_HTTP_PROBE_TIMEOUT)
        except Exception:  # noqa: BLE001
            proxy_ok = False
        if not proxy_ok:
            lane.last_sideline_at = time.time()
            log("tor: lane #%d's still sidelined -- will re-test in ~%.0fm",
                lane.index, SIDELINED_RECHECK_S / 60.0)
            return False
        pid = f"tor-{lane.index}"
        if pool.get_by_id(pid) is None:
            pool.add(
                lane.proxy_url,
                proxy_id=pid,
                label=f"Tor lane #{lane.index} {{{lane.exit_country}}}",
            )
        lane.sidelined = False
        lane.last_sideline_at = 0.0
        lane.regen_failures = 0
        log("tor: lane #%d revived from sideline", lane.index)
        return True
