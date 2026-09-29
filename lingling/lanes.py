"""Tor egress lanes: N tor.exe processes pinned to distinct exit countries
(StrictNodes + ExitNodes {cc}), each a local SOCKS5 on 127.0.0.1:52001+.
Missing stem/tor degrades to "Tor unavailable"; tor.exe children join a
kill-on-close Windows Job Object so they never outlive this process.
Heal ladder: restart_lane -> regenerate_lane.
"""

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

#: strike threshold
_TIMEOUT_RUN = 3

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
    #: timeout run
    timeout_run: int = 0
    #: repin stage
    repins: int = 0
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
        """Re-read ports from previous torrcs so a restart keeps the same
        lane layout; heal unbindable ports up front. A concrete country pin
        in a torrc is a stale copy of a previous list -- the current
        countries.txt wins. Only an unpin ({*}) survives a restart.

        The port search is wider than `find_free_port`'s default because
        Windows excludes port ranges (Hyper-V/WSL) and the default block can
        land entirely inside one: on this machine 52301-52500 is unbindable,
        so the stock 200-port scan found nothing and left the lane with a
        control port tor could not use."""
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
        # dead lanes
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
        """Kill our own leftover tor processes before launching any lane.

        A tor that outlived its lingling holds its lane's DataDirectory, and
        the next launch dies with "another Tor process is running with the same
        data directory". The lane then never comes up, silently, for the whole
        session. Measured: FIVE orphans alive with no lingling running, and
        five of six lanes failed to boot -- the pool quietly ran on one exit
        while `start.lanes` still advertised six.

        Scoped to the LANE PORT RANGE, not to a lane's two ports, because the
        port a leftover holds is not reliably the one this build would assign
        it: the indexing has shifted between versions, so a lane-by-lane sweep
        misses exactly the orphans it is meant to catch. A Tor Browser never
        listens in this range, so nothing else can be hit -- which is why this
        is not the `taskkill /IM tor.exe` that was rightly reverted."""
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

        A lane dir is ~47 MB, almost all of it `cached-microdescs` (36 MB) plus
        the consensus. Nothing ever removed the dir of a lane that stopped
        being configured, so they accumulate. Measured: **30 dirs, 1.4 GB, for a
        pool of 5-6** -- against a 1.5 GB state directory.

        Only the DEAD ones. The live lanes' dirs are Tor's descriptor cache,
        and re-downloading ~36 MB per lane on every boot is a real delay for
        43 MB of disk. Pruning the dead ones costs nothing at all, which is
        why this runs at startup and nothing is wiped at exit.

        Scoped hard: only `tor-<digits>` directly under `lanes_dir`, and only
        indices the current pool does not use. A live lane's dir is never
        touched, and nothing outside `lanes_dir` is looked at."""
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
        """Launch one tor.exe, marked in-flight for the whole attempt.

        `healing` is what stops the health daemon restarting a lane that is
        still coming up. A launch takes tens of seconds, and `_launch_lane`
        sets `healthy = False` at the start -- so without this flag the daemon
        sees "down" and kills the very launch it is waiting on. The flag was
        declared and read by `healthy_lanes()` but NEVER written anywhere, so
        it was inert and there was no way to tell "booting" from "broken".

        `wanted` is set here too: being asked to run is what tells the daemon
        it may bring the lane back if the launch fails. Without it the daemon
        cannot tell a lane whose launch failed from one the CLI has simply not
        reached yet, and it skipped both forever."""
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
        """stem's timeout uses SIGALRM (POSIX only), so we race its launcher
        thread against ``boot_timeout`` ourselves. stem returns as soon as
        tor prints its first bootstrap line (completion_percent=0): a tor
        that bootstraps slowly must NOT be killed here -- the boot gate
        tracks progress and escalates. This race only guards against a tor
        that never starts at all."""
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
        """Stop every lane we started.

        Deliberately scoped to our own lanes. This used to finish with
        ``taskkill /F /IM tor.exe``, which force-kills **every** tor.exe on
        the machine -- including an unrelated Tor Browser.

        It also claimed `winjob` "already guarantees the children die with this
        process". Measured, that is not enough: `winjob` works -- the job is
        created, our process is in it, and an assigned child reports as in it
        -- and ONE tor still survived a run. Five had accumulated, holding five
        lane DataDirectories, and the next run could only boot one lane. So the
        sweep is not redundant and it is now the whole lane range.

        Idempotent: the second call returns at once. Callers now reach here
        both by hand and through a `finally`, and the sweep costs seconds.

        Lane data directories are NOT deleted here. They are Tor's descriptor
        cache -- ~36 MB per lane -- and wiping them means re-downloading it on
        the next boot. The owner asked for the disk back, then said what he
        actually wanted was no delay at startup; the startup prune gives him
        both, because it only ever removes dirs for lanes the pool does not
        have. Nothing accumulates either way."""
        if self._stopping:
            return
        self._stopping = True
        # drain rebuilds
        for t in list(self._rebuilds):
            t.join(timeout=20)
        for lane in self.lanes:
            self._stop_lane_process(lane)
        # whole range
        self._reap_orphans()

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
        """Best-known bootstrap percent from the lane's boot.log; -1 = no
        evidence of progress recorded. -1 is treated as 0% progress by the
        boot gate: a lane that never even logged 0% hasn't started.

        boot.log is a separate plain-notice sink, and the reason it exists is
        a cold-start trap found on a wiped data dir: the [circ,edge] sink
        records circuit noise, but bootstrap progress is a NOTICE in the
        general domain -- it went to stdout only, so tor.log never showed a
        percent and the gate read a healthy, mid-download tor as STUCK. On a
        warm cache the download is instant and the gate never had to look;
        cold, every fresh user paid a restart loop that reset the download
        each cycle. The notice sink makes the gate's input exist."""
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
        """When the descriptor cache was last touched, or 0.0 with none.

        The boot gate's evidence that a cold tor is DOWNLOADING rather than
        dead: on a wiped data dir the download takes minutes, and a gate that
        restarts on silence resets it every cycle -- the fresh user's boot
        loop. File mtimes are the honest signal: tor rewrites these files as
        it fetches, so a moving mtime is progress, silence after a move is
        a stall, and the files exist whether or not any log line does."""
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
        """Drop the ExitNodes country pin and relaunch. A pinned lane needs
        the exit descriptors for exactly one country; when the directory
        network is missing them, unpinning lets tor build any path. The pin
        is not sticky: a country that cannot bootstrap re-rolls to the next
        quiet pool entry instead of staying stuck."""
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
        """Let in-flight requests finish before the lane is torn down.

        A request riding a lane that is torn down loses its upstream mid-body
        and the client is left waiting with nothing in the log to show for it.
        Measured over the log: six teardowns happened with traffic in the
        preceding 25 seconds, and the ones that hurt most -- a lane pulled with
        21 calls in flight, another with 25 -- were followed by 24 and 8 minutes
        of silence. This is a small, cheap guard against a costly event."""
        deadline = time.time() + timeout
        while lane.active > 0 and time.time() < deadline:
            time.sleep(0.2)

    def restart_lane(self, lane: Lane, repin: bool = False) -> bool:
        """Re-cook a lane, re-pinning it first if its relay was limited.

        With a pin in the torrc a plain restart would come back on the very
        same limited exit, so a limited lane has to be re-pinned to a
        relay -- that is the whole point of the pin.

        `repin` asks for a fresh exit on a lane that is NOT limited. The
        timeout tally needs that: a circuit that keeps timing out is bad
        without being rate-limited, and re-pinning it must not be expressed
        by writing a fake expiry into `limited_until`, which means "quota
        spent until T" and feeds country rotation."""
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
        """A lane's own timeout tally: 3 without a 200 and it is demolished.

        The tally is per lane and only a 200 clears it. Other lanes succeeding
        in between does NOT clear it -- that is the whole point. Measured over
        the log: lane 1 times out, lanes 2-5 return 200, lane 1 times out
        again, and the run keeps climbing while every neighbour stays healthy.

        Replayed over the owner's 615 callends: the longest run any of his
        lanes reached was **2**, so this escalation has never fired on his
        traffic -- it is a backstop, not a routine path. (The 6-lane soak
        harness, which runs far hotter, reached 3 three times.) A run only
        starts being charged at all now that the transport does the charging.

        Two strikes, escalating:

            3rd timeout, no 200 since  ->re-pin a fresh exit, same country
            3rd timeout after a re-pin ->a fresh country as well

        Same country first because a fresh country means a cold tor boot and a
        young circuit, which is the exact condition that produces the SSLEOFs.
        Returns a description of what moved, or None if nothing did."""
        lane.timeout_run += 1
        if lane.timeout_run < _TIMEOUT_RUN:
            return None
        lane.timeout_run = 0
        if lane.repins == 0:
            # fresh exit
            lane.repins = 1
            self._rebuild_async(lane, repin=True)
            return f"lane {lane.index} timed out {_TIMEOUT_RUN}x -- fresh exit"
        # fresh country
        lane.repins = 0
        moved = self.rotate_exit_country(lane)
        self._rebuild_async(lane)
        return (f"lane {lane.index} timed out {_TIMEOUT_RUN}x again -- "
                f"moved to {{{moved or lane.exit_country}}}")

    def _rebuild_async(self, lane: Lane, repin: bool = False) -> None:
        """Re-cook a lane on its own thread, tracked so shutdown can join it.

        `restart_lane` drains live requests and re-launches tor, so it can sit
        for seconds. The request that tripped the counter must not wait on
        that -- it is already retrying elsewhere.

        Tracked, because a rebuild already past its `_stopping` check can
        launch tor AFTER `stop_all` has swept: that is how a straggler survives
        a run and accumulates until the next run cannot boot a lane."""
        t = threading.Thread(
            target=self.restart_lane, args=(lane,), kwargs={"repin": repin},
            name=f"rebuild-{lane.index}", daemon=True)
        self._rebuilds = [x for x in self._rebuilds if x.is_alive()]
        self._rebuilds.append(t)
        t.start()

    def note_ok(self, lane: Lane) -> None:
        """A 200 clears the lane's tallies, and nothing else does."""
        lane.timeout_run = 0
        lane.repins = 0

    def note_result(self, country: str, status: int) -> None:
        """Score a country by what the EXIT did: a 200, or a 429. Nothing else.

        The docstring always claimed "200s are the only thing that counts",
        but the code subtracted for every non-200. That charged a country for
        things it does not control: a 403 is the free tier gating on the
        CLIENT, which this codebase treats as not-a-lane-signal everywhere
        else, and a 500 is upstream capacity -- the same footing as the 503s
        that never reach here at all. Measured over the log, 70 callends
        decremented a country and 63 of them were 403s, every one a GET
        `/api.json` from the soak harness. Only 2 were the owner's.

        Both a real request and the health probe route their verdict through
        here. The probe used to call the re-pinner directly, so a lane that
        arrived on a spent exit charged the relay but never the country -- and
        since rotation reads the country's record, the same country kept being
        walked back into the same spent range."""
        if not country or country == "*":
            return
        if status not in (200, 429):
            # exit signals
            return
        self._score[country] = self._score.get(country, 0) + (
            1 if status == 200 else -1)
        self._save_score()

    def _save_score(self) -> None:
        """Best effort: a restart should not forget which countries worked.

        Written as integer counts, not the float expiries `_save_stamps` is
        built for -- reading one back as the other silently zeroes the map.
        Routed through `_write_json` so the same seam covers both."""
        self._write_json(SCORE_PATH, self._score)

    def _load_score(self) -> Dict[str, int]:
        """Integer running totals, so a restart keeps the ranking.

        Separate from `_load_stamps` on purpose: those are expiries and drop
        themselves when stale, while a score is a plain tally that only ever
        moves by one. Reading one as the other would silently zero the map."""
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
        """Give a lane its own relay so no two lanes share an exit.

        True when the country is usable at all, which includes the
        country-only fallback used before any relay list exists -- returning
        False there would make the first ever boot skip every country and land
        on "*". False means the relay list is available and this country has
        nothing to give, i.e. every relay there is currently limited."""
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
        """Record that this lane's exit has hit the free tier, and until when.

        **The single place that decides an exit is out.** A 429 is the
        evidence. Every path funnels through here so the lane's own deadline,
        the pool's map and the picker can never disagree. They used to: three
        different callers set the deadline with different rules, and an unnamed
        429 left the exit looking healthy.

        `retry-after` is NOT taken as the deadline. Measured over the log: 72
        of the 87 429s carry a `retry-after`, and they collapse onto just three
        instants -- 05:30:03 and 05:30:04 on two consecutive days -- a spread of
        four seconds within each. All 8 of the owner's own 429s land on
        05:30:03. So it is the far end's global window reset, not a cooldown
        for this exit. And the exit is plainly not out: 17 of 23 exits that
        429'd served a 200 afterwards, four of them within 12s, and one lane
        returned 429 then 200 one second apart on the same IP. Writing that
        window onto the exit retires healthy relays for the rest of the day, so
        the bounded local cooldown is what we use."""
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
        """``{fingerprint: valid_until}`` from disk, dropping expired entries.

        Both pieces of relay bookkeeping are the same shape -- a fingerprint
        with a moment after which it stops mattering -- so they share this.
        For a limited relay that moment is the far end's own reset; for a used
        one it is how long the exit is considered "not fresh". Keeping the
        limited map on disk matters because a reset runs to hours while a
        session lasts minutes: without it every restart re-pins relays we
        already know are limited and pays a 429 to learn it again."""
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
        """Best effort: never let bookkeeping break a run.

        One write path, so a caller with no disk -- the verifier builds the
        manager with `__new__` -- has a single method to stub."""
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
        """Move the lane to the best country that still has an exit to give.

        Ladder: preferred -> quiet -> fallback -> expansion pool. A country is
        skipped when it is in the rotation cursor, when another lane holds it,
        or when every relay there is currently limited -- and that last test
        comes from `_limited`, which carries the far end's own reset, so it
        expires by itself.

        The cursor is a *rotation* aid, not a blacklist: it exists so a lane
        walks its ladder instead of bouncing between two countries, it is kept
        in memory only, and it is fed by nothing but a successful rotation.
        When it covers everything it is cleared and the walk starts again, so
        a country always comes back. `*` is reserved for an empty ladder.

        The previous design persisted a country blacklist in the torrc and fed
        it from 429s, so hitting the free tier limit removed a country for
        good. It had already banned `fr,us,no,hu` on lane 1, and a lane that
        had walked its whole ladder fell to `*` permanently."""
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
