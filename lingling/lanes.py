"""Tor egress lanes: N tor.exe processes pinned to distinct exit countries."""

from __future__ import annotations

import os
import platform
import queue
import re
import shutil
import subprocess
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import bounds, exits, netutil, winjob
from .bench import (EXIT_DAY_CAP, USED_PATH, USED_TTL_S, VERIFIED_PATH,
                    BenchMixin)

Log = Callable[..., None]

TOR_DIST_URL = "https://archive.torproject.org/tor-package-archive/torbrowser/"

_STICKY_S = bounds.number("LINGLING_STICKY_S", 15, 0.0, bounds.WINDOW_S)

_FAST_S = bounds.number("LINGLING_FAST_S", 5, 0.0, bounds.WINDOW_S)

_PORT_SEARCH = 4000


def _leftover_ports(pid: int, held: Dict[int, int],
                    socks_base: int, control_base: int) -> List[int]:
    """The control ports a leftover tor of ours might still answer on"""
    ports: List[int] = []
    for port, owner in held.items():
        if owner != pid:
            continue
        if control_base <= port < control_base + _PORT_SEARCH:
            derived = port
        elif socks_base <= port < socks_base + _PORT_SEARCH:
            derived = control_base + (port - socks_base)
        else:
            continue
        if derived not in ports:
            ports.append(derived)
    return ports

_ROTATION_MEMORY = 8

CONFLUX = os.environ.get("LINGLING_CONFLUX", "0")

_DRAIN_S = 5.0
_DEFER_MAX_S = 120.0

_MOVE_GUARD_S = 10.0

_STREAM_STALL_S = 120.0

EXPAND_COUNTRIES = [
    "de", "nl", "us", "fr", "se", "ch", "at", "ca", "gb", "ro",
    "fi", "pl", "cz", "hu", "bg", "lt", "lv", "ee", "dk", "no",
    "ie", "es", "it", "pt", "gr", "tr", "ua", "md", "rs", "hr",
    "si", "sk", "is", "lu", "be", "au", "nz", "jp", "kr", "sg",
    "hk", "tw", "th", "in", "id", "my", "za", "br", "ar", "cl",
    "mx", "il", "ae",
]


def _stem() -> Any:
    try:
        import stem  # noqa: F401
        import stem.connection  # noqa: F401
        import stem.control  # noqa: F401
        import stem.process  # noqa: F401
        return stem
    except ImportError:
        return None


@dataclass
class Lane:
    """One tor.exe process + its local SOCKS5 entry point."""
    index: int
    socks_port: int
    control_port: int
    exit_country: str
    data_dir: Path
    process: Optional[subprocess.Popen] = None
    exit_ip: str = ""
    boot_ok: bool = False
    healthy: Optional[bool] = None
    wanted: bool = False
    healing: bool = False
    last_circuit_built_ts: float = 0.0
    asked: bool = False
    probe_code: int = -1
    probe_note: str = ""
    probe_reason: str = ""
    down_reported: bool = False
    active: int = 0
    streams: Dict[int, tuple] = field(default_factory=dict)
    deferred_at: float = 0.0
    retired_at: float = 0.0
    retired_exit: str = ""
    last_used_at: float = 0.0
    last_real_at: float = 0.0
    limited_until: float = 0.0
    limit_reason: str = ""
    exit_fingerprint: str = ""
    exit_since_ts: float = 0.0
    recent_countries: List[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def forget_limit(self) -> None:
        """The lane rides another relay, so the old exit's clock is not ours.

        A limited lane is one whose *exit* was written off, not one whose tor
        is broken.  Every path that hands the lane a different relay has to
        say so, or the lane keeps sitting out a wall that was never about the
        relay it is on now -- which is how a lane can be benched for a whole
        session by a single 429.
        """
        self.limited_until = 0.0
        self.limit_reason = ""

    def cookie_path(self) -> Path:
        return self.data_dir / "control_auth_cookie"

    def torrc_path(self) -> Path:
        return self.data_dir / "torrc"

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def track_stream(self, sock, progress: Dict, hangup=None) -> None:
        """Remember an open stream, so a dead exit can reach it and its client."""
        with self.lock:
            self.streams[id(sock)] = (sock, progress, hangup)

    def drop_stream(self, sock) -> None:
        """Forget a stream: it is over, or it went back into the pool."""
        with self.lock:
            self.streams.pop(id(sock), None)

    def cut_silent(self, floor: Optional[float] = None) -> int:
        """Hang up the clients of this lane's streams that said nothing for ``floor``."""
        quiet_for = _STREAM_STALL_S if floor is None else floor
        now = time.time()
        with self.lock:
            quiet = [self.streams.pop(k)
                     for k, (_, p, _) in list(self.streams.items())
                     if now - p.get("at", now) >= quiet_for]
        for sock, _, hangup in quiet:
            netutil.abort_quiet(sock)
            if hangup is not None:
                hangup()
        return len(quiet)


def _owes_nobody(lane: Lane) -> bool:
    """True when a written-off lane has been cut free of every stream.

    A lane still carrying live tunnels (``lane.active``) owes them: stopping
    its tor under them resets every one, and the copies that were mid-dial
    come back as a wave of connection errors.
    """
    return not lane.streams and lane.active <= 0


class StickyState:
    """Name the quickest lane of the session, and start every race on it."""

    def __init__(self, tor: "TorManager",
                 emit: Optional[Callable[[Dict], None]] = None,
                 release_s: float = _STICKY_S) -> None:
        self.tor = tor
        self._emit = emit or (lambda e: None)
        self.release_s = release_s
        self._lock = threading.Lock()
        self.phase = "routing"
        self.winner: Optional[int] = None
        self._cursor = 0
        self._announced = False

    def attach(self, emit: Callable[[Dict], None]) -> None:
        """The proof pane hears the routing and the stick."""
        self._emit = emit or (lambda e: None)

    def next_lane(self, exclude: Optional[set] = None) -> Optional[Lane]:
        """The lane this request rides, or None to let ``pick_lane`` decide."""
        with self._lock:
            now = time.time()
            if self.phase == "stuck":
                lane = self._lane(self.winner)
                if (lane is not None and lane.healthy and not lane.healing
                        and lane.limited_until <= now
                        and not (exclude and lane.index in exclude)):
                    self._touch(lane)
                    return lane
                self._wake()
            lanes = [l for l in self.tor.healthy_lanes()
                     if l.limited_until <= now
                     and not (exclude and l.index in exclude)]
            if not lanes:
                return None
            lanes.sort(key=lambda l: l.index)
            lane = lanes[self._cursor % len(lanes)]
            self._cursor += 1
            self._touch(lane)
            if not self._announced:
                self._announced = True
                self._emit({
                    "type": "lane", "kind": "sticky", "t": time.time(),
                    "lane": 0, "cc": "", "ip": "",
                    "msg": f"racing {len(lanes)} lanes -- the first answer "
                           f"takes it",
                })
            return lane

    def record(self, lane_index: int, secs: float) -> None:
        """One attempt's latency: under the threshold it wins the ride."""
        with self._lock:
            if self.phase != "routing":
                return
            if secs <= _FAST_S:
                self._stick(lane_index, secs)

    def stuck_on(self, lane_index: int) -> bool:
        """True when this lane is the one we are stuck on."""
        with self._lock:
            return self.phase == "stuck" and lane_index == self.winner

    def on_verdict(self, lane_index: int) -> None:
        """A verdict on the ridden lane sends the session back to routing."""
        with self._lock:
            if self.phase == "stuck" and lane_index == self.winner:
                self._wake()

    def _lane(self, index: Optional[int]) -> Optional[Lane]:
        if index is None:
            return None
        for lane in self.tor.lanes:
            if lane.index == index:
                return lane
        return None

    def _touch(self, lane: Lane) -> None:
        """The same bookkeeping ``pick_lane`` does for its picks."""
        lane.last_used_at = time.perf_counter_ns()
        lane.last_real_at = time.time()

    def _stick(self, lane_index: int, secs: float) -> None:
        """The fast lane is named the quickest, and leads the next round."""
        lane = self._lane(lane_index)
        if lane is None:
            return
        self.winner = lane_index
        self.phase = "stuck"
        self._cursor = 0
        self._emit({
            "type": "lane", "kind": "sticky", "t": time.time(),
            "lane": lane_index, "cc": lane.exit_country,
            "ip": lane.exit_ip,
            "msg": f"lane {lane_index} {{{lane.exit_country}}} answered in "
                   f"{secs:.1f}s -- quickest lane",
        })

    def _wake(self) -> None:
        """Back to electing: the next fast answer takes the lead."""
        fell = self.winner
        self.phase = "routing"
        self.winner = None
        self._cursor = 0
        self._announced = True
        self._emit({
            "type": "lane", "kind": "sticky", "t": time.time(),
            "lane": fell or 0, "cc": "", "ip": "",
            "msg": f"lane {fell} faltered -- re-electing the quickest "
                   f"under {_FAST_S:.0f}s",
        })


class TorManager(BenchMixin):
    """Owns the lifecycle of N local tor-backed SOCKS5 lanes."""

    def __init__(
        self,
        root_dir: Path,
        count: int = 5,
        exit_countries: Optional[List[str]] = None,
        fallback_countries: Optional[List[str]] = None,
        preferred_countries: Optional[List[str]] = None,
        socks_base: int = 52001,
        control_base: int = 52301,
        tor_exe: str = "",
        boot_timeout: int = 120,
        log: Optional[Log] = None,
        prune_orphans: bool = False,
    ) -> None:
        self.root = Path(root_dir)
        self.tools_dir = self.root / "tools"
        self.lanes_dir = self.root / "lanes"
        self.count = max(1, count)
        override = os.environ.get("LINGLING_EXPAND", "").strip().lower()
        self._expand = ([c.strip() for c in override.split(",")
                         if len(c.strip()) == 2] if override
                        else list(EXPAND_COUNTRIES))
        base = [c for c in (exit_countries or []) if c]
        if not base:
            base = exits.random_countries(self.count, self._expand)
        self._preferred = [c for c in (preferred_countries or []) if c]
        boot = self._preferred or base
        self.countries = [boot[i % len(boot)] for i in range(self.count)]
        self._quiet = list(base)
        self._fallback = [c for c in (fallback_countries or [])
                          if c not in self._quiet]
        self._score: Dict[str, int] = self._load_score()
        self._by_country: Optional[Dict[str, List[exits.Exit]]] = None
        self._limited: Dict[str, Dict] = self._load_limited()
        self._used: Dict[str, float] = self._load_stamps(USED_PATH)
        self._verified: Dict[str, float] = self._load_stamps(VERIFIED_PATH)
        self._spent: Dict[str, int] = self._load_spent()
        self.socks_base = socks_base
        self.control_base = control_base
        self.tor_exe_override = tor_exe
        self.boot_timeout = boot_timeout
        self.log: Log = log or (lambda *a, **k: None)
        self.prune_orphans = prune_orphans
        self.lanes: List[Lane] = []
        self.limit_hook: Optional[Callable[[Lane], None]] = None
        self.sticky = StickyState(self)
        self._move_locks: Dict[int, threading.Lock] = {}
        self._move_locks_lock = threading.Lock()
        self._booting: set = set()
        self._emit: Callable[[Dict], None] = lambda e: None
        self._tor_executable: Optional[Path] = None
        self._stopping = False
        self._rebuilds: List[threading.Thread] = []
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self.lanes_dir.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    def attach(self, emit: Callable[[Dict], None]) -> None:
        """The pane hears the moves this pool makes."""
        self._emit = emit or (lambda e: None)

    def _load_existing(self) -> None:
        """Re-read ports from previous torrcs, so a restart keeps the lane layout."""
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
                            if val == "*":
                                exit_cc = "*"
                        except IndexError:
                            pass
            if socks_port in seen_socks or not netutil.bindable(socks_port):
                try:
                    socks_port = netutil.find_free_port(
                        self.socks_base + i, max_offset=_PORT_SEARCH,
                        reserved=seen_socks)
                except RuntimeError:
                    pass
            seen_socks.add(socks_port)
            if control_port in seen_control or not netutil.bindable(control_port):
                try:
                    control_port = netutil.find_free_port(
                        self.control_base + i, max_offset=_PORT_SEARCH,
                        reserved=seen_control)
                except RuntimeError:
                    pass
            seen_control.add(control_port)
            self.lanes.append(Lane(
                index=i + 1, socks_port=socks_port, control_port=control_port,
                exit_country=exit_cc, data_dir=lane_dir,
            ))

    def _is_windows(self) -> bool:
        return platform.system() == "Windows"

    def _locate_tor_binary(self, root: Path) -> Optional[Path]:
        if not root.exists():
            return None
        target = "tor.exe" if self._is_windows() else "tor"
        for p in root.rglob(target):
            return p
        return None

    def _tor_path(self) -> Path:
        if self.tor_exe_override:
            return Path(self.tor_exe_override)
        if self._tor_executable is not None and self._tor_executable.exists():
            return self._tor_executable
        located = self._locate_tor_binary(self.tools_dir)
        if located is not None:
            self._tor_executable = located
            return located
        ext = ".exe" if self._is_windows() else ""
        return self.tools_dir / "tor" / ("tor" + ext)

    def _geoip_path(self) -> Optional[Path]:
        tor = self._tor_path()
        for c in (tor.parent.parent / "data" / "geoip", tor.parent / "geoip"):
            if c.is_file():
                return c
        for p in self.tools_dir.rglob("geoip"):
            if p.is_file():
                return p
        return None

    def _geoip6_path(self) -> Optional[Path]:
        g = self._geoip_path()
        if g is None:
            return None
        c = g.parent / "geoip6"
        return c if c.is_file() else None

    def tools_ready(self) -> bool:
        return self._tor_path().exists()

    def stem_available(self) -> bool:
        return _stem() is not None

    def ensure_tools(self, log: Optional[Log] = None) -> Optional[str]:
        """Locate or auto-download the Tor Expert Bundle. Returns error str or None."""
        log = log or self.log
        located = self._locate_tor_binary(self.tools_dir)
        if located and located.exists():
            self._tor_executable = located
            return None
        if self.tor_exe_override:
            if Path(self.tor_exe_override).exists():
                self._tor_executable = Path(self.tor_exe_override)
                return None
            return f"override tor_exe not found: {self.tor_exe_override}"
        if not self._is_windows():
            return "tor binary not found and auto-download is Windows-only; set LINGLING_TOR_EXE"
        log("downloading the Tor Expert Bundle (one-time, ~1-2 min) ...")
        try:
            self._download_tor_expert_bundle()
        except Exception as exc:  # noqa: BLE001
            return f"download failed: {exc}"
        located = self._locate_tor_binary(self.tools_dir)
        if not located:
            return "download finished but tor.exe was not found inside it"
        self._tor_executable = located
        return None

    def _download_tor_expert_bundle(self) -> None:
        import urllib.request

        with urllib.request.urlopen(TOR_DIST_URL, timeout=30) as r:
            listing = r.read().decode("utf-8", "replace")
        versions = re.findall(r'href="(\d+\.\d+\.\d+)/"', listing)
        if not versions:
            raise RuntimeError("could not parse any Tor versions from the archive")
        latest = max(versions, key=lambda v: tuple(int(x) for x in v.split(".")))
        name = f"tor-expert-bundle-windows-x86_64-{latest}.tar.gz"
        tmp = self.tools_dir / name
        urllib.request.urlretrieve(f"{TOR_DIST_URL}{latest}/{name}", tmp)
        with tarfile.open(tmp, "r:gz") as tf:
            root = self.tools_dir.resolve()
            for member in tf.getmembers():
                if not (root / member.name).resolve().is_relative_to(root):
                    raise RuntimeError(f"unsafe path in Tor bundle: {member.name}")
            tf.extractall(root)
        tmp.unlink(missing_ok=True)

    def _lane_config(self, lane: Lane) -> Dict[str, str]:
        cfg = {
            "SocksPort": (f"127.0.0.1:{lane.socks_port} "
                          f"IsolateSOCKSAuth KeepAliveIsolateSOCKSAuth"),
            "ControlPort": f"127.0.0.1:{lane.control_port}",
            "DataDirectory": str(lane.data_dir),
            "CookieAuthentication": "1",
            "CookieAuthFile": str(lane.cookie_path()),
            "MaxCircuitDirtiness": "600",
            "SocksTimeout": "45",
            "CircuitStreamTimeout": "20",
            "KeepalivePeriod": "60",
            "ConfluxEnabled": CONFLUX,
            "ConnectionPadding": "0",
            "RunAsDaemon": "0",
            "Log": [f"[circ,edge]info file {lane.data_dir / 'tor.log'}",
                    f"notice file {lane.data_dir / 'boot.log'}"],
        }
        geoip = self._geoip_path()
        if geoip is not None:
            cfg["GeoIPFile"] = str(geoip)
            geo6 = self._geoip6_path()
            if geo6 is not None:
                cfg["GeoIPv6File"] = str(geo6)
            if lane.exit_fingerprint:
                cfg["ExitNodes"] = "$" + lane.exit_fingerprint
                cfg["StrictNodes"] = "1"
            elif lane.exit_country == "*":
                cfg["ExitNodes"] = "*"
            else:
                cfg["ExitNodes"] = "{" + lane.exit_country + "}"
        return cfg

    def _write_torrc(self, lane: Lane) -> None:
        lane.data_dir.mkdir(parents=True, exist_ok=True)
        lines = ["# Auto-generated by lingling. Do not edit by hand."]
        for k, v in self._lane_config(lane).items():
            for item in (v if isinstance(v, list) else [v]):
                lines.append(f"{k} {item}")
        lane.torrc_path().write_text("\n".join(lines) + "\n")

    def setup_lanes(self) -> Optional[str]:
        """Download tor if needed + write all torrcs. Returns error or None."""
        if not self.stem_available():
            return "stem is not installed (pip install stem)"
        err = self.ensure_tools()
        if err:
            return err
        self._reap_orphans()
        if self.prune_orphans:
            self._prune_lane_dirs()
        for lane in self.lanes:
            self._pin(lane)
        for lane in self.lanes:
            self._write_torrc(lane)
        return None

    def start_all(self, on_lane: Optional[Callable[[Lane, str], None]] = None) -> None:
        self.start_lanes(self.lanes, on_lane=on_lane)

    def start_lanes(self, lanes: List[Lane],
                    on_lane: Optional[Callable[[Lane, str], None]] = None) -> None:
        """Launch lanes in parallel (serial costs N x boot); never raises."""
        if _stem() is None or not self.tools_ready() or not lanes:
            return
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=min(len(lanes), 5)) as pool:
            futures = {pool.submit(self._launch_lane, lane): lane
                       for lane in lanes}
            for fut in as_completed(futures):
                lane = futures[fut]
                try:
                    status = fut.result()
                except Exception:  # noqa: BLE001
                    status = "failed"
                if on_lane:
                    on_lane(lane, status)

    def _reap_orphans(self) -> int:
        """Kill our own leftover tor processes before launching any lane."""
        if os.name != "nt":
            return 0
        span = range(0, _PORT_SEARCH)
        held = netutil.pids_on_ports(
            [base + i for base in (self.socks_base, self.control_base)
             for i in span])
        killed = 0
        for pid in set(held.values()):
            if self._stop_orphan(pid, _leftover_ports(
                    pid, held, self.socks_base, self.control_base)):
                killed += 1
        return killed

    def _stop_orphan(self, pid: int,
                     ports: Optional[List[int]] = None,
                     only: Optional[Path] = None) -> bool:
        """Authenticate a saved Tor control endpoint and verify ownership"""
        stem = _stem()
        if stem is None:
            return False
        root = self.lanes_dir.resolve()
        cookies = [p for p in self.lanes_dir.glob("tor-*/control_auth_cookie")
                   if p.is_file()]
        if not cookies:
            return False
        if only is not None:
            wanted = only.resolve()
            own = wanted / "control_auth_cookie"
            cookies.sort(key=lambda p: p != own)
        if ports is None:
            ports = []
            for config in self.lanes_dir.glob("tor-*/torrc"):
                try:
                    lines = config.read_text(encoding="utf-8").splitlines()
                    control = next(line.split()[1] for line in lines
                                   if line.startswith("ControlPort "))
                    ports.append(int(control.rsplit(":", 1)[-1]))
                except (OSError, ValueError, IndexError, StopIteration):
                    continue
        for port in ports:
            for cookie in cookies:
                try:
                    with stem.control.Controller.from_port(port=port) as ctl:
                        deadline = threading.Timer(2.0, ctl.close)
                        deadline.daemon = True
                        deadline.start()
                        try:
                            stem.connection.authenticate_safecookie(
                                ctl, cookie_path=str(cookie),
                                suppress_ctl_errors=False)
                            if ctl.get_pid() != pid:
                                continue
                            actual = Path(
                                ctl.get_conf("DataDirectory")).resolve()
                            if only is not None:
                                if actual != only.resolve():
                                    continue
                            elif actual != root and root not in actual.parents:
                                continue
                            ctl.signal(stem.Signal.SHUTDOWN)
                            return True
                        finally:
                            deadline.cancel()
                except (OSError, ValueError, IndexError, StopIteration,
                        stem.ControllerError,
                        stem.connection.AuthenticationFailure):
                    continue
                except Exception as exc:  # noqa: BLE001
                    self.log("orphan probe on %d: %s", port, exc)
                    continue
        return False

    def _clear_lane_dir_owner(self, lane: Lane) -> bool:
        """Stop any tor of ours still holding this lane's data directory"""
        if os.name != "nt":
            return False
        held = netutil.pids_on_ports(
            [base + i for base in (self.socks_base, self.control_base)
             for i in range(_PORT_SEARCH)])
        cleared = False
        for pid in set(held.values()):
            ports = _leftover_ports(pid, held, self.socks_base,
                                    self.control_base)
            if self._stop_orphan(pid, ports, only=lane.data_dir):
                cleared = True
        return cleared

    def _prune_lane_dirs(self) -> int:
        """Destructive on purpose: a lane dir holds ~47 MB of downloaded state."""
        live = {lane.index for lane in self.lanes}
        try:
            entries = list(self.lanes_dir.iterdir())
        except OSError:
            return 0
        pruned = 0
        for path in entries:
            m = re.fullmatch(r"tor-(\d+)", path.name)
            if not m or int(m.group(1)) in live:
                continue
            try:
                shutil.rmtree(path)
                pruned += 1
            except OSError:
                pass
        return pruned

    def _launch_lane(self, lane: Lane) -> str:
        """Launch one tor.exe, marked in-flight for the whole attempt."""
        lane.wanted = True
        lane.healing = True
        try:
            return self._launch_lane_inner(lane)
        finally:
            lane.healing = False

    def _launch_lane_inner(self, lane: Lane) -> str:
        """Launch one tor.exe; returns a status string, never raises."""
        if lane.running():
            lane.healthy = True
            return "already_running"
        if self._stopping:
            return "skipped"
        lane.healthy = False
        if not lane.torrc_path().exists():
            self._write_torrc(lane)
        try:
            lane.data_dir.mkdir(parents=True, exist_ok=True)
            lane.data_dir.joinpath("tor.log").write_text("")
            lane.data_dir.joinpath("boot.log").write_text("")
        except OSError:
            pass
        if any(netutil.port_is_open("127.0.0.1", port)
               for port in (lane.socks_port, lane.control_port)):
            try:
                reserved = {p for item in self.lanes if item is not lane
                            for p in (item.socks_port, item.control_port)}
                lane.socks_port = netutil.find_free_port(
                    self.socks_base + lane.index - 1, reserved=reserved)
                reserved.add(lane.socks_port)
                lane.control_port = netutil.find_free_port(
                    self.control_base + lane.index - 1, reserved=reserved)
                self._write_torrc(lane)
            except RuntimeError:
                return "failed"
        if self._stopping:
            return "skipped"
        stem = _stem()
        config = self._lane_config(lane)

        def _msg(line: str) -> None:
            if "Bootstrapped 100" in line:
                lane.last_circuit_built_ts = time.time()

        try:
            lane.process = self._launch_tor_process(stem, lane, config, _msg)
        except Exception as exc:  # noqa: BLE001
            if "Failed to bind one of the listener ports" not in str(exc):
                self.log("lane #%d launch failed: %s", lane.index, exc)
                return "failed"
            try:
                taken_socks = {l.socks_port for l in self.lanes if l is not lane}
                taken_ctrl = {l.control_port for l in self.lanes if l is not lane}
                lane.socks_port = netutil.find_free_port(
                    self.socks_base + lane.index - 1, reserved=taken_socks)
                lane.control_port = netutil.find_free_port(
                    self.control_base + lane.index - 1, reserved=taken_ctrl)
                self._write_torrc(lane)
                config = self._lane_config(lane)
                lane.process = self._launch_tor_process(stem, lane, config, _msg)
            except Exception as exc2:  # noqa: BLE001
                self.log("lane #%d launch failed after port re-roll: %s",
                         lane.index, exc2)
                return "failed"

        if os.name == "nt" and not winjob.ensure_kill_job():
            self.log("kill job unavailable -- tor children may outlive us")
        if lane.process is not None:
            try:
                if os.name == "nt" and not winjob.assign(lane.process.pid):
                    self.log("lane #%d not in the kill job -- it may outlive us",
                             lane.index)
            except Exception:  # noqa: BLE001
                pass
        for _ in range(20):
            if netutil.port_is_open("127.0.0.1", lane.socks_port, timeout=0.5):
                break
            time.sleep(0.5)
        if not netutil.port_is_open("127.0.0.1", lane.socks_port, timeout=0.5):
            return "failed"
        lane.boot_ok = True
        lane.healthy = True
        return "started"

    def _launch_tor_process(self, stem: Any, lane: Lane,
                            config: Dict[str, str], msg_handler) -> Any:
        """stem's timeout uses SIGALRM, so its launcher thread is raced instead."""
        result_q: "queue.Queue" = queue.Queue()
        abandoned = threading.Event()

        def _do_launch() -> None:
            try:
                proc = stem.process.launch_tor_with_config(
                    config=config,
                    tor_cmd=str(self._tor_path()),
                    init_msg_handler=msg_handler,
                    take_ownership=True,
                    close_output=False,
                    completion_percent=0,
                )
            except BaseException as exc:  # noqa: BLE001
                result_q.put(("err", exc))
                return
            if abandoned.is_set():
                self._kill(proc)
                return
            result_q.put(("ok", proc))

        threading.Thread(target=_do_launch, daemon=True,
                         name=f"tor-launch-{lane.index}").start()
        try:
            kind, payload = result_q.get(timeout=self.boot_timeout)
        except queue.Empty:
            abandoned.set()
            try:
                kind, payload = result_q.get_nowait()
            except queue.Empty:
                pass
            else:
                if kind == "ok" and payload is not None:
                    self._kill(payload)
            for pid in set(netutil.pids_on_ports(
                    {lane.control_port, lane.socks_port}).values()):
                self._stop_orphan(pid)
            raise RuntimeError(
                f"tor lane #{lane.index} did not start within "
                f"{self.boot_timeout}s")
        if kind == "err":
            raise RuntimeError(f"tor lane #{lane.index} launch failed: {payload}")
        return payload

    def _kill(self, proc: Any) -> None:
        """End a tor we launched and no longer hold a handle to."""
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass

    def stop_all(self) -> None:
        """Stop every lane we started."""
        if self._stopping:
            return
        self._stopping = True
        for lane in self.lanes:
            self._stop_lane_process(lane)
        self._reap_orphans()
        for t in list(self._rebuilds):
            t.join(timeout=5)

    def _stop_lane_process(self, lane: Lane) -> bool:
        """Stop this lane's tor, and say whether it is really gone"""
        lane.boot_ok = False
        lane.healthy = False
        lane.exit_ip = ""
        lane.asked = False
        lane.probe_code = -1
        lane.probe_note = ""
        lane.wanted = False
        proc = lane.process
        lane.process = None
        if proc is None:
            return True
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        for _ in range(10):
            try:
                if proc.poll() is not None:
                    return True
            except Exception:  # noqa: BLE001
                return True
            time.sleep(0.2)
        self.log("lane #%d did not exit -- its directory is still locked",
                 lane.index)
        return False

    def lane_bootstrap_pct(self, lane: Lane) -> int:
        """Best-known bootstrap percent from the lane's boot.log; -1 = none."""
        try:
            lines = lane.data_dir.joinpath("boot.log").read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return -1
        for line in reversed(lines):
            if "Bootstrapped" not in line:
                continue
            m = re.search(r"Bootstrapped\s+(\d+)%", line)
            if m:
                return int(m.group(1))
        return -1

    def cache_mtime(self, lane: Lane) -> float:
        """When the descriptor cache was last touched, or 0.0 with none."""
        latest = 0.0
        try:
            for name in ("cached-microdesc-consensus", "cached-microdescs",
                         "cached-microdescs.new"):
                path = lane.data_dir / name
                if path.exists():
                    latest = max(latest, path.stat().st_mtime)
        except OSError:
            pass
        return latest

    def _drain(self, lane: Lane, timeout: float = _DRAIN_S) -> None:
        """Let in-flight requests finish before the lane is torn down."""
        deadline = time.time() + timeout
        while lane.active > 0 and time.time() < deadline:
            time.sleep(0.2)
        if lane.active > 0:
            self._emit({
                "type": "lane", "kind": "fail", "t": time.time(),
                "lane": lane.index, "cc": lane.exit_country,
                "ip": lane.exit_ip,
                "msg": f"lane {lane.index} recooked -- "
                       f"{lane.active} streams cut",
            })

    def _defer_rebuild(self, lane: Lane, repin: bool = False) -> None:
        """Recook a loaded lane once its streams are done, not under them."""
        if lane.deferred_at:
            return
        lane.deferred_at = time.time()
        self._emit({
            "type": "lane", "kind": "heal", "t": time.time(),
            "lane": lane.index, "cc": lane.exit_country, "ip": lane.exit_ip,
            "msg": f"lane {lane.index} has {lane.active} streams in flight "
                   f"-- moving it once they finish",
        })

        def watch() -> None:
            deadline = time.time() + _DEFER_MAX_S
            while (not self._stopping and lane.active > 0
                   and time.time() < deadline):
                time.sleep(0.5)
            lane.deferred_at = 0.0
            if self._stopping:
                return
            moved = self.restart_lane(lane, repin=repin)
            if moved:
                self._emit({
                    "type": "lane", "kind": "up", "t": time.time(),
                    "lane": lane.index, "cc": lane.exit_country,
                    "ip": lane.exit_ip,
                    "msg": f"lane {lane.index} recooked -- "
                           f"{{{lane.exit_country}}} once its streams "
                           f"finished",
                })

        t = threading.Thread(target=watch, name=f"defer-{lane.index}",
                             daemon=True)
        self._rebuilds = [x for x in self._rebuilds if x.is_alive()]
        self._rebuilds.append(t)
        t.start()

    def _move_lock_for(self, lane: Lane) -> threading.Lock:
        """The one lock every move on this lane is serialised by."""
        with self._move_locks_lock:
            return self._move_locks.setdefault(lane.index, threading.Lock())

    def claim_move(self, lane: Lane) -> bool:
        """One burst on one lane is one move: the first verdict owns it.

        Ten copies of one dead call all notice the same dead exit within a
        second, and one 429 fans out to every request in flight.  Each of
        those used to write the exit off, rotate the country and recook the
        lane on its own, so a single bad relay burned a dozen tor restarts
        and reset every tunnel the copies were still reading.

        The window is per exit, not per lane.  A verdict about a *different*
        relay is news -- the lane was moved onto another exit and that one was
        spent too -- and refusing it would leave the lane parked on a spent
        exit with no move pending, which is how a lane can vanish from the
        race for the rest of the day.
        """
        lock = self._move_lock_for(lane)
        if not lock.acquire(blocking=False):
            return False
        try:
            now = time.time()
            exit_now = lane.exit_fingerprint
            if lane.healing:
                return False
            if (now - lane.retired_at < _MOVE_GUARD_S
                    and lane.retired_exit == exit_now):
                return False
            lane.retired_at = now
            lane.retired_exit = exit_now
            return True
        finally:
            lock.release()

    def restart_lane(self, lane: Lane, repin: bool = False,
                     now: bool = False) -> bool:
        """Re-cook a lane, re-pinning it first if its relay was limited.

        One move per lane: the whole teardown holds this lane's move lock, so
        two retirees can never pull the same tor down together and cut each
        other's live streams.

        The torrc is written every time, because the relay a lane rides is not
        only changed here: the refusal path re-pins a lane before the recook
        even starts, so re-picking the same relay here says nothing about what
        the torrc on disk still names.  Skipping the write on that comparison
        left tor launched on the relay the lane had just been moved off -- the
        lane read as freshly pinned while every last request still went out
        through the exit that had just refused it, and the far end's 429s were
        charged to the innocent relay now written on the lane.
        """
        if self._stopping:
            return False
        with self._move_lock_for(lane):
            if lane.active > 0 and not now:
                self._defer_rebuild(lane, repin)
                return False
            self._drain(lane)
            if repin or lane.limited_until > time.time():
                self._pin(lane)
            lane.forget_limit()
            self._stop_lane_process(lane)
            self._clear_lane_dir_owner(lane)
            self._write_torrc(lane)
            return self._launch_lane(lane) in ("started", "already_running")

    def regenerate_lane(self, lane: Lane) -> bool:
        """Wipe DataDirectory (fresh guards/consensus/exit) and relaunch."""
        if self._stopping:
            return False
        self._stop_lane_process(lane)
        self._clear_lane_dir_owner(lane)
        if lane.data_dir.exists():
            shutil.rmtree(lane.data_dir, ignore_errors=True)
        lane.data_dir.mkdir(parents=True, exist_ok=True)
        lane.exit_ip = ""
        lane.exit_since_ts = 0.0
        lane.last_circuit_built_ts = 0.0
        lane.boot_ok = False
        try:
            taken_socks = {l.socks_port for l in self.lanes if l is not lane}
            taken_ctrl = {l.control_port for l in self.lanes if l is not lane}
            lane.socks_port = netutil.find_free_port(
                self.socks_base + lane.index - 1, reserved=taken_socks)
            lane.control_port = netutil.find_free_port(
                self.control_base + lane.index - 1, reserved=taken_ctrl)
        except RuntimeError:
            pass
        self._write_torrc(lane)
        return self.restart_lane(lane)

    def _control(self, lane: Lane):
        """A live control connection to this lane's tor, or None."""
        stem = _stem()
        if stem is None or not lane.control_port:
            return None
        try:
            ctl = stem.control.Controller.from_port(port=lane.control_port)
        except Exception as exc:  # noqa: BLE001
            self.log("lane #%d has no control port: %s", lane.index, exc)
            return None
        try:
            stem.connection.authenticate_safecookie(
                ctl, cookie_path=str(lane.cookie_path()),
                suppress_ctl_errors=False)
        except Exception as exc:  # noqa: BLE001
            self.log("lane #%d refused the cookie: %s", lane.index, exc)
            ctl.close()
            return None
        return ctl

    def move_exit(self, lane: Lane, relay: exits.Exit) -> bool:
        """Pin a lane to one relay and build a fresh circuit, without a boot."""
        stem = _stem()
        ctl = self._control(lane)
        if stem is None or ctl is None:
            return False
        try:
            ctl.set_conf("ExitNodes", "$" + relay.fingerprint)
            ctl.set_conf("StrictNodes", "1")
            ctl.signal(stem.Signal.NEWNYM)
        except Exception as exc:  # noqa: BLE001
            self.log("lane #%d could not move: %s", lane.index, exc)
            return False
        finally:
            ctl.close()
        lane.exit_fingerprint = relay.fingerprint
        lane.exit_ip = relay.ip
        lane.exit_since_ts = time.time()
        lane.forget_limit()
        lane.asked = False
        lane.probe_code = -1
        lane.probe_note = ""
        lane.probe_reason = ""
        self._used[relay.fingerprint] = time.time() + USED_TTL_S
        self._save_stamps(USED_PATH, self._used)
        self._write_torrc(lane)
        return True

    def note_timeout(self, lane: Lane) -> Optional[str]:
        """A timeout means a dead exit: write it off and move the lane at once."""
        if not self.claim_move(lane):
            return None
        if lane.limited_until <= time.time():
            self.note_limited(lane)
        lane.cut_silent()
        moved = self.rotate_exit_country(lane)
        self._rebuild_async(lane, now=_owes_nobody(lane))
        return (f"lane {lane.index} timed out -- moved to "
                f"{{{moved or lane.exit_country}}}")

    def note_ssl_error(self, lane: Lane) -> Optional[str]:
        """An SSL cut means a dead exit: the timeout verdict on the first hit."""
        if not self.claim_move(lane):
            return None
        if lane.limited_until <= time.time():
            self.note_limited(lane)
        lane.cut_silent()
        moved = self.rotate_exit_country(lane)
        self._rebuild_async(lane, now=_owes_nobody(lane))
        return (f"lane {lane.index} ssl-failed -- moved to "
                f"{{{moved or lane.exit_country}}}")

    def note_slow_exit(self, lane: Lane, elapsed: float = 0.0) -> Optional[str]:
        """A first event over the ceiling means a bad exit: the timeout verdict."""
        if not self.claim_move(lane):
            return None
        if lane.limited_until <= time.time():
            self.note_limited(lane)
        lane.cut_silent()
        moved = self.rotate_exit_country(lane)
        self._rebuild_async(lane, now=_owes_nobody(lane))
        return (f"lane {lane.index} first event {elapsed:.0f}s -- moved to "
                f"{{{moved or lane.exit_country}}}")

    def _rebuild_async(self, lane: Lane, repin: bool = False,
                       now: bool = False) -> None:
        """Re-cook a lane on its own thread, tracked so shutdown can join it."""
        t = threading.Thread(
            target=self.restart_lane, args=(lane,),
            kwargs={"repin": repin, "now": now},
            name=f"rebuild-{lane.index}", daemon=True)
        self._rebuilds = [x for x in self._rebuilds if x.is_alive()]
        self._rebuilds.append(t)
        t.start()

    def _load_exits(self) -> bool:
        """Load the relay list once. Empty until a lane has cached one."""
        if self._by_country is not None:
            return bool(self._by_country)
        consensus = exits.find_consensus(self.lanes_dir)
        geoip = self._geoip_path()
        if consensus is None or geoip is None:
            self._by_country = {}
            return False
        try:
            ranges = exits.load_geoip(geoip)
            self._by_country = exits.load_relays(consensus, ranges)
        except Exception:  # noqa: BLE001
            self._by_country = {}
        return bool(self._by_country)

    def _held(self, lane: Optional[Lane] = None) -> Tuple[List[str], List[str]]:
        """What the other lanes are holding: their exits, and their exit IPs.

        The far end counts its day against the exit's address, so two relays
        behind one address are one allowance.  A lane must not share either.
        """
        others = [l for l in self.lanes if l is not lane]
        fingerprints = [l.exit_fingerprint for l in others if l.exit_fingerprint]
        ips = [l.exit_ip for l in others if l.exit_ip and l.exit_ip != "?"]
        return fingerprints, ips

    def _headroom(self, country: str) -> int:
        """How many exits this country can still give us today."""
        if not self._load_exits():
            return 0
        fingerprints, ips = self._held()
        return exits.headroom(self._by_country or {}, country, fingerprints,
                              self._limited, self._spent, time.time(), ips,
                              EXIT_DAY_CAP)

    def _census(self) -> List[str]:
        """Every country the cached consensus has an exit in, lowercased.

        The configured ladder is a preference, not a cage.  Once it is spent
        through, the hunt goes on across the whole consensus instead of
        settling for an exit with nothing left to give.
        """
        if not self._load_exits():
            return []
        return sorted(cc.lower() for cc in (self._by_country or {}))

    def _pin(self, lane: Lane) -> bool:
        """Give a lane its own relay so no two lanes share an exit IP."""
        if not self._load_exits():
            lane.exit_fingerprint = ""
            return True
        avoid, avoid_ip = self._held(lane)
        picked = exits.claim(self._by_country or {}, lane.exit_country,
                             avoid, self._limited, self._used, time.time(),
                             self._verified, self._spent, avoid_ip,
                             EXIT_DAY_CAP)
        if picked is None:
            lane.exit_fingerprint = ""
            return False
        lane.exit_fingerprint = picked.fingerprint
        lane.exit_ip = picked.ip
        lane.exit_since_ts = time.time()
        self._used[picked.fingerprint] = time.time() + USED_TTL_S
        self._save_stamps(USED_PATH, self._used)
        return True

    def rotate_exit_country(self, lane: Lane) -> Optional[str]:
        """Move the lane to the best country that still has an exit to give.

        Where a country scored earlier today is the last thing consulted: the
        first question is whether it has anything left to hand us.  Ranking by
        who just answered 200 is how every lane ended up on the same handful of
        countries until they were spent, all at once.  When every configured
        country is spent the hunt widens to the whole consensus, because the
        alternative -- an exit with nothing left, or no exit at all -- is worse
        than the search taking longer.

        A pool we know is spent and a pool we cannot see are different things:
        a lane is only left to tor's own choosing when this machine knows of no
        relays at all.  With a consensus in hand and nothing left in it, the
        answer is None -- no move -- and the sweep comes back to try again.
        Every country that comes up empty leaves the lane exactly as it found
        it, pin and fingerprint both, so the bookkeeping never claims a move
        that tor was never asked to make.
        """
        leaving = lane.exit_country
        ladders = (self._preferred, self._quiet, self._fallback, self._expand,
                   self._census())
        others = {l.exit_country for l in self.lanes if l is not lane}
        for exhausted in (False, True):
            if exhausted:
                lane.recent_countries.clear()
            for spread in (True, False):
                for pool in ladders:
                    ranked = sorted(
                        [c for c in dict.fromkeys(pool)
                         if c and c != leaving
                         and c not in lane.recent_countries],
                        key=lambda c: (-self._headroom(c),
                                       -self._score.get(c, 0)))
                    for cc in ranked:
                        if spread and cc in others:
                            continue
                        was = (lane.exit_country, lane.exit_fingerprint)
                        lane.exit_country = cc
                        if self._pin(lane):
                            lane.recent_countries.append(cc)
                            del lane.recent_countries[:-_ROTATION_MEMORY]
                            return cc
                        lane.exit_country, lane.exit_fingerprint = was
        if self._census():
            return None
        lane.exit_country = "*"
        lane.exit_fingerprint = ""
        return "*"

    def healthy_lanes(self) -> List[Lane]:
        return [l for l in self.lanes if l.healthy and not l.healing]

    def idle_lanes(self) -> List[Lane]:
        """Healthy lanes carrying nothing, the least used one first."""
        idle = [l for l in self.healthy_lanes()
                if not l.streams and l.active <= 0 and not l.deferred_at]
        return sorted(idle, key=lambda l: l.last_used_at)

    def sweep_candidates(self, country: str) -> List[exits.Exit]:
        """Relays in one country worth asking. Empty until a consensus caches."""
        if not self._load_exits():
            return []
        avoid, avoid_ip = self._held()
        return exits.sweep_order(self._by_country or {}, country, avoid,
                                 self._limited, self._used, self._verified,
                                 time.time(), self._spent, avoid_ip,
                                 EXIT_DAY_CAP)
