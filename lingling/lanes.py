"""Tor egress lanes: N tor.exe processes pinned to distinct exit countries (StrictNodes + ExitNodes ..."""

from __future__ import annotations

import json
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
from typing import Any, Callable, Dict, List, Optional

from . import exits, netutil, winjob

Log = Callable[..., None]

TOR_DIST_URL = "https://archive.torproject.org/tor-package-archive/torbrowser/"

#: no retry-after
LIMITED_FALLBACK_S = 600.0

#: limited exits
LIMITED_PATH = "limited-exits.json"

#: used exits
USED_PATH = "used-exits.json"

#: country scores
SCORE_PATH = "country-scores.json"

#: freshness window
USED_TTL_S = float(os.environ.get("LINGLING_FRESH_S", "86400"))

#: port search
_PORT_SEARCH = 4000

#: rotation cursor
_ROTATION_MEMORY = 8

#: conflux legs
CONFLUX = os.environ.get("LINGLING_CONFLUX", "0")

#: drain grace
_DRAIN_S = 5.0

#: expansion pool
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
    #: daemon verdict
    healthy: Optional[bool] = None
    #: wants tor
    wanted: bool = False
    #: healing now
    healing: bool = False
    last_circuit_built_ts: float = 0.0
    #: asked
    asked: bool = False
    #: last probe
    probe_code: int = -1
    #: live tunnels
    active: int = 0
    #: last pick
    last_used_at: float = 0.0
    #: last real
    last_real_at: float = 0.0
    #: server reset
    limited_until: float = 0.0
    #: pinned relay
    exit_fingerprint: str = ""
    #: rotation cursor
    recent_countries: List[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def cookie_path(self) -> Path:
        return self.data_dir / "control_auth_cookie"

    def torrc_path(self) -> Path:
        return self.data_dir / "torrc"

    def running(self) -> bool:
        if self.process is not None and self.process.poll() is None:
            return True
        return netutil.port_is_open("127.0.0.1", self.socks_port, timeout=0.1)


class TorManager:
    """Owns the lifecycle of N local tor-backed SOCKS5 lanes."""

    def __init__(
        self,
        root_dir: Path,
        count: int = 5,
        exit_countries: Optional[List[str]] = None,
        fallback_countries: Optional[List[str]] = None,
        preferred_countries: Optional[List[str]] = None,
        socks_base: int = 52001,
        # avoid 52101
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
        base = list(exit_countries) if exit_countries else ["us"]
        if not base:
            base = ["us"]
        # preferred pool
        self._preferred = [c for c in (preferred_countries or []) if c]
        boot = self._preferred or base
        self.countries = [boot[i % len(boot)] for i in range(self.count)]
        # rotation order
        self._quiet = list(base)
        self._fallback = [c for c in (fallback_countries or [])
                          if c not in self._quiet]
        # expansion pool
        override = os.environ.get("LINGLING_EXPAND", "").strip().lower()
        self._expand = ([c.strip() for c in override.split(",")
                         if len(c.strip()) == 2] if override
                        else list(EXPAND_COUNTRIES))
        #: country score
        self._score: Dict[str, int] = self._load_score()
        #: relay pool
        self._by_country: Optional[Dict[str, List[exits.Exit]]] = None
        #: retired relays
        self._limited: Dict[str, float] = self._load_stamps(LIMITED_PATH)
        #: recent exits
        self._used: Dict[str, float] = self._load_stamps(USED_PATH)
        self.socks_base = socks_base
        self.control_base = control_base
        self.tor_exe_override = tor_exe
        self.boot_timeout = boot_timeout
        self.log: Log = log or (lambda *a, **k: None)
        #: prune opt-in
        self.prune_orphans = prune_orphans
        self.lanes: List[Lane] = []
        #: limit hook
        self.limit_hook: Optional[Callable[[Lane], None]] = None
        self._tor_executable: Optional[Path] = None
        self._stopping = False
        #: rebuild threads
        self._rebuilds: List[threading.Thread] = []
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self.lanes_dir.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    # setup
    def _load_existing(self) -> None:
        """Re-read ports from previous torrcs so a restart keeps the same lane layout; heal unbindable ports ..."""
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
                                exit_cc = "*"  # keep state
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
            # traversal guard
            root = self.tools_dir.resolve()
            for member in tf.getmembers():
                if not (root / member.name).resolve().is_relative_to(root):
                    raise RuntimeError(f"unsafe path in Tor bundle: {member.name}")
            tf.extractall(root)
        tmp.unlink(missing_ok=True)

    def _lane_config(self, lane: Lane) -> Dict[str, str]:
        cfg = {
            # isolate slots
            "SocksPort": (f"127.0.0.1:{lane.socks_port} "
                          f"IsolateSOCKSAuth KeepAliveIsolateSOCKSAuth"),
            "ControlPort": f"127.0.0.1:{lane.control_port}",
            "DataDirectory": str(lane.data_dir),
            "CookieAuthentication": "1",
            "CookieAuthFile": str(lane.cookie_path()),
            "MaxCircuitDirtiness": "600",
            # connect fails
            "SocksTimeout": "45",
            "CircuitStreamTimeout": "20",
            # idle circuits
            "KeepalivePeriod": "60",
            # conflux legs
            "ConfluxEnabled": CONFLUX,
            # throughput
            "ConnectionPadding": "0",
            "RunAsDaemon": "0",
            # teardown reasons
            "Log": [f"[circ,edge]info file {lane.data_dir / 'tor.log'}",
                    # visible progress
                    f"notice file {lane.data_dir / 'boot.log'}"],
        }
        # geoip pin
        geoip = self._geoip_path()
        if geoip is not None:
            cfg["GeoIPFile"] = str(geoip)
            geo6 = self._geoip6_path()
            if geo6 is not None:
                cfg["GeoIPv6File"] = str(geo6)
            if lane.exit_fingerprint:
                # pinned relay
                cfg["ExitNodes"] = "$" + lane.exit_fingerprint
                cfg["StrictNodes"] = "1"
            elif lane.exit_country == "*":
                cfg["ExitNodes"] = "*"  # any exit
            else:
                cfg["ExitNodes"] = "{" + lane.exit_country + "}"
        return cfg

    def _write_torrc(self, lane: Lane) -> None:
        lane.data_dir.mkdir(parents=True, exist_ok=True)
        lines = ["# Auto-generated by lingling. Do not edit by hand."]
        for k, v in self._lane_config(lane).items():
            # repeated directives
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
        # own ports
        self._reap_orphans()
        """
        Lane dirs are deleted only when the caller asked for it. Booting a
        one-lane pool must never wipe the caches of a five-lane install, so
        the product opts in and everything else -- a probe, a verify suite,
        a stray REPL -- is read-only by construction.
        """
        if self.prune_orphans:
            self._prune_lane_dirs()
        # own relay
        for lane in self.lanes:
            self._pin(lane)
        for lane in self.lanes:
            self._write_torrc(lane)
        return None

    # lifecycle
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
            if netutil.kill_pid(pid, grace_s=1):
                killed += 1
        return killed

    def _prune_lane_dirs(self) -> int:
        """Delete the data directories of lanes this pool does not have.

        Destructive on purpose: a lane dir holds ~47 MB of guard and
        consensus state that came over the network. Only the product calls
        this, via `prune_orphans=True`; a smaller pool built by a probe or
        a verify suite leaves the other lanes' caches alone.
        """
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
        # fresh logs
        try:
            lane.data_dir.mkdir(parents=True, exist_ok=True)
            lane.data_dir.joinpath("tor.log").write_text("")
            lane.data_dir.joinpath("boot.log").write_text("")
        except OSError:
            pass
        for port in (lane.socks_port, lane.control_port):
            if netutil.port_is_open("127.0.0.1", port):
                pid = netutil.pid_on_port(port)
                if pid:
                    netutil.kill_pid(pid, grace_s=2)
                    time.sleep(0.2)
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
            # port heal
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

        # say so
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
        # the fact
        lane.healthy = True
        return "started"

    def _launch_tor_process(self, stem: Any, lane: Lane,
                            config: Dict[str, str], msg_handler) -> Any:
        """stem's timeout uses SIGALRM (POSIX only), so we race its launcher thread against ``boot_timeout`` ..."""
        result_q: "queue.Queue" = queue.Queue()

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
                result_q.put(("ok", proc))
            except BaseException as exc:  # noqa: BLE001
                result_q.put(("err", exc))

        threading.Thread(target=_do_launch, daemon=True,
                         name=f"tor-launch-{lane.index}").start()
        try:
            kind, payload = result_q.get(timeout=self.boot_timeout)
        except queue.Empty:
            # no bootstrap
            for port in (lane.control_port, lane.socks_port):
                pid = netutil.pid_on_port(port)
                if pid:
                    netutil.kill_pid(pid, grace_s=1)
            raise RuntimeError(
                f"tor lane #{lane.index} did not start within "
                f"{self.boot_timeout}s")
        if kind == "err":
            raise RuntimeError(f"tor lane #{lane.index} launch failed: {payload}")
        return payload

    def stop_all(self) -> None:
        """Stop every lane we started."""
        if self._stopping:
            return
        self._stopping = True
        """
        Tor dies first: joining rebuild threads before the kill left every
        tor.exe visibly alive for up to a minute after Ctrl+C, which read
        as "lingling never stops tor". Lanes go down in a second or two;
        the threads see _stopping and bail; the port-range reap sweeps up
        anything a mid-flight rebuild resurrected.
        """
        for lane in self.lanes:
            self._stop_lane_process(lane)
        # whole range
        self._reap_orphans()
        for t in list(self._rebuilds):
            t.join(timeout=5)

    def _stop_lane_process(self, lane: Lane) -> None:
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
        lane.healthy = False
        lane.exit_ip = ""
        lane.asked = False
        # on purpose
        lane.wanted = False

    def lane_bootstrap_pct(self, lane: Lane) -> int:
        """Best-known bootstrap percent from the lane's boot.log; -1 = no evidence of progress recorded."""
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

    def unpin_lane(self, lane: Lane) -> bool:
        """Drop the ExitNodes country pin and relaunch."""
        if self._stopping:
            return False
        if self._geoip_path() is None or not lane.torrc_path().exists():
            return False
        self._stop_lane_process(lane)
        for port in (lane.socks_port, lane.control_port):
            if netutil.port_is_open("127.0.0.1", port):
                pid = netutil.pid_on_port(port)
                if pid:
                    netutil.kill_pid(pid, grace_s=2)
        old = lane.exit_country
        lane.exit_country = "*"
        try:
            self._write_torrc(lane)
            return self._launch_lane(lane) in ("started", "already_running")
        except BaseException:  # noqa: BLE001
            lane.exit_country = old
            return False

    def _drain(self, lane: Lane, timeout: float = _DRAIN_S) -> None:
        """Let in-flight requests finish before the lane is torn down."""
        deadline = time.time() + timeout
        while lane.active > 0 and time.time() < deadline:
            time.sleep(0.2)

    def restart_lane(self, lane: Lane, repin: bool = False) -> bool:
        """Re-cook a lane, re-pinning it first if its relay was limited."""
        if self._stopping:
            return False
        # live requests
        self._drain(lane)
        repinned = False
        if repin or lane.limited_until > time.time():
            before = lane.exit_fingerprint
            self._pin(lane)
            repinned = lane.exit_fingerprint != before
        # fresh exit
        lane.limited_until = 0.0
        self._stop_lane_process(lane)
        for port in (lane.socks_port, lane.control_port):
            if netutil.port_is_open("127.0.0.1", port):
                pid = netutil.pid_on_port(port)
                if pid:
                    netutil.kill_pid(pid, grace_s=2)
                time.sleep(0.2)
        if repinned or not lane.torrc_path().exists():
            self._write_torrc(lane)
        return self._launch_lane(lane) in ("started", "already_running")

    def regenerate_lane(self, lane: Lane) -> bool:
        """Wipe DataDirectory (fresh guards/consensus/exit) and relaunch."""
        if self._stopping:
            return False
        self._stop_lane_process(lane)
        for port in (lane.socks_port, lane.control_port):
            if netutil.port_is_open("127.0.0.1", port):
                pid = netutil.pid_on_port(port)
                if pid:
                    netutil.kill_pid(pid, grace_s=2)
        if lane.data_dir.exists():
            shutil.rmtree(lane.data_dir, ignore_errors=True)
        lane.data_dir.mkdir(parents=True, exist_ok=True)
        lane.exit_ip = ""
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

    def score_of(self, country: str) -> int:
        """How this country has actually done: 200s minus everything else."""
        return self._score.get(country, 0)

    def note_timeout(self, lane: Lane) -> Optional[str]:
        """A timeout means a dead exit: write it off and move the lane at once.

        The same action a real 429 takes in ``health.on_refused`` -- retire the
        exit, pin a fresh relay, change the country -- with no second strike.
        """
        if lane.limited_until <= time.time():
            self.note_limited(lane)
        moved = self.rotate_exit_country(lane)
        self._rebuild_async(lane)
        return (f"lane {lane.index} timed out -- moved to "
                f"{{{moved or lane.exit_country}}}")

    def _rebuild_async(self, lane: Lane, repin: bool = False) -> None:
        """Re-cook a lane on its own thread, tracked so shutdown can join it."""
        t = threading.Thread(
            target=self.restart_lane, args=(lane,), kwargs={"repin": repin},
            name=f"rebuild-{lane.index}", daemon=True)
        self._rebuilds = [x for x in self._rebuilds if x.is_alive()]
        self._rebuilds.append(t)
        t.start()

    def note_result(self, country: str, status: int) -> None:
        """Score a country by what the EXIT did: a 200, or a 429."""
        if not country or country == "*":
            return
        if status not in (200, 429):
            # exit signals
            return
        self._score[country] = self._score.get(country, 0) + (
            1 if status == 200 else -1)
        self._save_score()

    def _save_score(self) -> None:
        """Best effort: a restart should not forget which countries worked."""
        self._write_json(SCORE_PATH, self._score)

    def _load_score(self) -> Dict[str, int]:
        """Integer running totals, so a restart keeps the ranking."""
        try:
            raw = json.loads((self.root / SCORE_PATH).read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, int] = {}
        for cc, val in raw.items():
            try:
                out[str(cc)] = int(val)
            except (TypeError, ValueError):
                continue
        return out

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

    def _pin(self, lane: Lane) -> bool:
        """Give a lane its own relay so no two lanes share an exit."""
        if not self._load_exits():
            lane.exit_fingerprint = ""
            return True  # country-only
        avoid = [l.exit_fingerprint for l in self.lanes
                 if l is not lane and l.exit_fingerprint]
        picked = exits.claim(self._by_country or {}, lane.exit_country,
                             avoid, self._limited, self._used, time.time())
        if picked is None:
            lane.exit_fingerprint = ""
            return False
        lane.exit_fingerprint = picked.fingerprint
        #: known exit
        lane.exit_ip = picked.ip
        #: freshness
        self._used[picked.fingerprint] = time.time() + USED_TTL_S
        self._save_stamps(USED_PATH, self._used)
        return True

    def note_limited(self, lane: Lane, retry_after: float = 0.0) -> float:
        """Record that this lane's exit has hit the free tier, and until when."""
        if not lane.exit_fingerprint:
            return 0.0
        # window reset
        span = LIMITED_FALLBACK_S
        if 0 < retry_after < LIMITED_FALLBACK_S:
            span = retry_after
        until = time.time() + span
        lane.limited_until = until
        self._limited[lane.exit_fingerprint] = until
        self._save_stamps(LIMITED_PATH, self._limited)
        return until

    def _load_stamps(self, name: str) -> Dict[str, float]:
        """``{fingerprint: valid_until}`` from disk, dropping expired entries."""
        try:
            raw = json.loads((self.root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        now = time.time()
        fresh = {}
        for fp, until in raw.items():
            try:
                until = float(until)
            except (TypeError, ValueError):
                continue
            if until > now:
                fresh[str(fp)] = until
        return fresh

    def _write_json(self, name: str, payload) -> None:
        """Best effort: never let bookkeeping break a run."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(
                json.dumps(payload), encoding="utf-8")
        except OSError:
            pass

    def _save_stamps(self, name: str, stamps: Dict[str, float]) -> None:
        """Best effort: never let bookkeeping break a run."""
        self._write_json(name, stamps)

    def rotate_exit_country(self, lane: Lane) -> Optional[str]:
        """Move the lane to the best country that still has an exit to give."""
        leaving = lane.exit_country
        ladders = (self._preferred, self._quiet, self._fallback,
                   self._expand)
        others = {l.exit_country for l in self.lanes if l is not lane}
        for exhausted in (False, True):
            if exhausted:
                lane.recent_countries.clear()  # walk again
            for spread in (True, False):
                for pool in ladders:
                    # tie break
                    ranked = sorted(
                        [c for c in dict.fromkeys(pool)
                         if c and c != leaving
                         and c not in lane.recent_countries],
                        key=lambda c: -self._score.get(c, 0))
                    for cc in ranked:
                        if spread and cc in others:
                            continue
                        lane.exit_country = cc
                        if self._pin(lane):
                            # rotation cursor
                            lane.recent_countries.append(cc)
                            del lane.recent_countries[:-_ROTATION_MEMORY]
                            return cc
        # unpin
        lane.exit_country = "*"
        lane.exit_fingerprint = ""
        return "*"

    def healthy_lanes(self) -> List[Lane]:
        return [l for l in self.lanes if l.healthy and not l.healing]
