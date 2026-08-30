"""Tor egress lanes -- genuine exit-IP diversity from one machine.

Each lane is a separate ``tor.exe`` pinned to a *different* exit country via
``StrictNodes`` + disjoint ``ExitNodes {cc}``, so exit IPs are distinct by
construction. A manager owns N lanes, each a local SOCKS5 proxy on
127.0.0.1:52001+ with a control port on 52301+.

``stem`` is imported lazily; a missing stem or tor binary degrades to
"Tor unavailable" rather than crashing the launcher. tor.exe children join a
kill-on-close Windows Job Object so closing the terminal never orphans them.

Heals available to the health daemon, cheapest first:
  * NEWNYM (``renew``) -- "switch to clean circuits", ~10s rate-limited, does
    NOT guarantee a new IP.
  * ``rebuild_circuits`` -- close general circuits + build a fresh one,
    blocking until BUILT. The reliable primary.
  * ``restart_lane`` -- bounce tor.exe.
  * ``regenerate_lane`` -- wipe DataDirectory (fresh guards + consensus),
    optionally onto a new exit country. The heavy artillery, 30-90s.
"""

from __future__ import annotations

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

from . import netutil, winjob

Log = Callable[..., None]

TOR_DIST_URL = "https://archive.torproject.org/tor-package-archive/torbrowser/"


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
    #: Health daemon's latest verdict. None = not probed yet.
    healthy: Optional[bool] = None
    #: True while the daemon is restarting/regenerating this lane.
    healing: bool = False
    #: Consecutive failed health cycles -- fast-fail input.
    unhealthy_cycles: int = 0
    #: Consecutive probes that came back 429 (destination burn).
    burned_cycles: int = 0
    last_regenerate_at: float = 0.0
    last_circuit_built_ts: float = 0.0
    sidelined: bool = False
    last_sideline_at: float = 0.0
    #: Live in-flight relay tunnels through this lane (relay maintains it).
    active: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def cookie_path(self) -> Path:
        return self.data_dir / "control_auth_cookie"

    def torrc_path(self) -> Path:
        return self.data_dir / "torrc"

    def running(self) -> bool:
        if self.process is not None and self.process.poll() is None:
            return True
        return netutil.port_is_open("127.0.0.1", self.socks_port, timeout=0.1)

    def label(self) -> str:
        return f"lane {self.index} {{{self.exit_country}}}"


class TorManager:
    """Owns the lifecycle of N local tor-backed SOCKS5 lanes."""

    def __init__(
        self,
        root_dir: Path,
        count: int = 5,
        exit_countries: Optional[List[str]] = None,
        socks_base: int = 52001,
        # Not 52101: Windows administratively excludes that block
        # (Hyper-V/WSL reserves 52093-52192 on many machines).
        control_base: int = 52301,
        tor_exe: str = "",
        boot_timeout: int = 120,
        log: Optional[Log] = None,
        reuse: bool = False,
    ) -> None:
        self.root = Path(root_dir)
        self.tools_dir = self.root / "tools"
        self.lanes_dir = self.root / "lanes"
        self.count = max(1, count)
        base = list(exit_countries) if exit_countries else ["us"]
        if not base:
            base = ["us"]
        self.countries = [base[i % len(base)] for i in range(self.count)]
        self._rotation_countries: List[str] = list(base)
        self.socks_base = socks_base
        self.control_base = control_base
        self.tor_exe_override = tor_exe
        self.boot_timeout = boot_timeout
        self.log: Log = log or (lambda *a, **k: None)
        self.lanes: List[Lane] = []
        self._tor_executable: Optional[Path] = None
        self._stopping = False
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        if reuse:
            self.lanes_dir.mkdir(parents=True, exist_ok=True)
        else:
            self._fresh_lanes_dir()
        self._load_existing()

    def _fresh_lanes_dir(self) -> None:
        """Every launch cooks lanes with a fresh identity -- new keys, new
        guards, new circuit state -- but keeps tor's cached network
        directory (``cached-*``). Those files are public consensus data,
        not identity; wiping them forced a full multi-minute bootstrap on
        every single launch. First put down any orphaned tor.exe a crashed
        run left holding our ports (the Job Object covers clean exits; this
        covers kill -9), and remove stale lock files so the new tor never
        refuses to start."""
        identity_files = ("lock", "state", "control_auth_cookie")
        if self.lanes_dir.exists():
            for torrc in self.lanes_dir.glob("tor-*/torrc"):
                for line in torrc.read_text().splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[0] in ("SocksPort", "ControlPort"):
                        try:
                            port = int(parts[1].rsplit(":", 1)[-1])
                        except ValueError:
                            continue
                        pid = netutil.pid_on_port(port)
                        if pid:
                            netutil.kill_pid(pid)
            for lane_dir in self.lanes_dir.glob("tor-*"):
                if not lane_dir.is_dir():
                    continue
                for name in identity_files:
                    try:
                        (lane_dir / name).unlink(missing_ok=True)
                    except OSError:
                        pass
                shutil.rmtree(lane_dir / "keys", ignore_errors=True)
        self.lanes_dir.mkdir(parents=True, exist_ok=True)

    # -- setup --------------------------------------------------------------
    def _load_existing(self) -> None:
        """Re-read ports + country from a previous run's torrcs so a restart
        keeps the same lane layout; unbindable ports are healed up front."""
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
            if socks_port in seen_socks or not netutil.bindable(socks_port):
                try:
                    socks_port = netutil.find_free_port(
                        self.socks_base + i, reserved=seen_socks)
                except RuntimeError:
                    pass
            seen_socks.add(socks_port)
            if control_port in seen_control or not netutil.bindable(control_port):
                try:
                    control_port = netutil.find_free_port(
                        self.control_base + i, reserved=seen_control)
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
            tf.extractall(self.tools_dir)
        tmp.unlink(missing_ok=True)

    def _lane_config(self, lane: Lane) -> Dict[str, str]:
        cfg = {
            "SocksPort": f"127.0.0.1:{lane.socks_port}",
            "ControlPort": f"127.0.0.1:{lane.control_port}",
            "DataDirectory": str(lane.data_dir),
            "CookieAuthentication": "1",
            "CookieAuthFile": str(lane.cookie_path()),
            "MaxCircuitDirtiness": "600",
            "CircuitBuildTimeout": "60",
            "RunAsDaemon": "0",
            "Log": f"notice file {lane.data_dir / 'tor.log'}",
        }
        # Country pin only when geoip is present -- without it a StrictNodes
        # pin would leave the lane unable to build any circuit.
        geoip = self._geoip_path()
        if geoip is not None:
            cfg["GeoIPFile"] = str(geoip)
            geo6 = self._geoip6_path()
            if geo6 is not None:
                cfg["GeoIPv6File"] = str(geo6)
            cfg["ExitNodes"] = "{" + lane.exit_country + "}"
        return cfg

    def _write_torrc(self, lane: Lane) -> None:
        lane.data_dir.mkdir(parents=True, exist_ok=True)
        lines = ["# Auto-generated by lingling. Do not edit by hand."]
        lines += [f"{k} {v}" for k, v in self._lane_config(lane).items()]
        lane.torrc_path().write_text("\n".join(lines) + "\n")

    def setup_lanes(self) -> Optional[str]:
        """Download tor if needed + write all torrcs. Returns error or None."""
        if not self.stem_available():
            return "stem is not installed (pip install stem)"
        err = self.ensure_tools()
        if err:
            return err
        for lane in self.lanes:
            self._write_torrc(lane)
        return None

    # -- lifecycle -----------------------------------------------------------
    def start_all(self, on_lane: Optional[Callable[[Lane, str], None]] = None) -> None:
        self.start_lanes(self.lanes, on_lane=on_lane)

    def start_lanes(self, lanes: List[Lane],
                    on_lane: Optional[Callable[[Lane, str], None]] = None) -> None:
        """Launch the given lanes in parallel (serial launch costs N x boot).

        ``on_lane(lane, "started"|"already_running"|"failed")`` fires per lane
        as it resolves so the CLI spinner / proof log can show real progress.
        """
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

    def _launch_lane(self, lane: Lane) -> str:
        """Launch one tor.exe via stem under our own watchdog. Returns a
        status string; never raises (the batch must survive one bad lane)."""
        if lane.running():
            return "already_running"
        if self._stopping:
            return "skipped"
        if not lane.torrc_path().exists():
            self._write_torrc(lane)
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
            # Self-heal the classic Windows excluded-range bind failure once.
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

        winjob.ensure_kill_job()
        if lane.process is not None:
            try:
                winjob.assign(lane.process.pid)
            except Exception:  # noqa: BLE001
                pass
        for _ in range(20):
            if netutil.port_is_open("127.0.0.1", lane.socks_port, timeout=0.5):
                break
            time.sleep(0.5)
        if not netutil.port_is_open("127.0.0.1", lane.socks_port, timeout=0.5):
            return "failed"
        lane.boot_ok = True
        return "started"

    def _launch_tor_process(self, stem: Any, lane: Lane,
                            config: Dict[str, str], msg_handler) -> Any:
        """stem's own timeout uses SIGALRM (POSIX main-thread only), so we
        race its launcher thread against ``boot_timeout`` ourselves and kill
        the booting tor.exe by port if the watchdog wins."""
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
            except BaseException as exc:  # noqa: BLE001
                result_q.put(("err", exc))

        threading.Thread(target=_do_launch, daemon=True,
                         name=f"tor-launch-{lane.index}").start()
        try:
            kind, payload = result_q.get(timeout=self.boot_timeout)
        except queue.Empty:
            kind = None
            # SOCKS up but stem lagging on the notice: give it a moment,
            # otherwise kill the wedged tor so stem's stdout read returns.
            if netutil.port_is_open("127.0.0.1", lane.socks_port, timeout=0.2):
                try:
                    kind, payload = result_q.get(timeout=10)
                except queue.Empty:
                    pass
            if kind is None:
                for port in (lane.control_port, lane.socks_port):
                    pid = netutil.pid_on_port(port)
                    if pid:
                        netutil.kill_pid(pid, grace_s=1)
                raise RuntimeError(
                    f"tor lane #{lane.index} did not bootstrap within "
                    f"{self.boot_timeout}s")
        if kind == "err":
            raise RuntimeError(f"tor lane #{lane.index} launch failed: {payload}")
        return payload

    def stop_all(self) -> None:
        self._stopping = True
        for lane in self.lanes:
            self._stop_lane_process(lane)
        for lane in self.lanes:
            for port in (lane.socks_port, lane.control_port):
                if netutil.port_is_open("127.0.0.1", port):
                    pid = netutil.pid_on_port(port)
                    if pid:
                        netutil.kill_pid(pid, grace_s=2)
        if self._is_windows():
            try:
                subprocess.run(["taskkill", "/F", "/IM", "tor.exe"],
                               capture_output=True, text=True, timeout=10)
            except Exception:  # noqa: BLE001
                pass

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

    def restart_lane(self, lane: Lane) -> bool:
        if self._stopping:
            return False
        self._stop_lane_process(lane)
        for port in (lane.socks_port, lane.control_port):
            if netutil.port_is_open("127.0.0.1", port):
                pid = netutil.pid_on_port(port)
                if pid:
                    netutil.kill_pid(pid, grace_s=2)
                time.sleep(0.2)
        if not lane.torrc_path().exists():
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

    # -- circuit control ------------------------------------------------------
    def renew(self, lane: Lane) -> bool:
        """NEWNYM quick-heal. Rate-limited ~10s per tor process."""
        if _stem() is None:
            return False
        if not netutil.port_is_open("127.0.0.1", lane.control_port, timeout=0.3):
            return False
        try:
            from stem import Signal
            from stem.control import Controller
            import stem.connection

            with Controller.from_port(port=lane.control_port) as controller:
                stem.connection.authenticate_cookie(
                    controller, str(lane.cookie_path()))
                wait = controller.get_newnym_wait()
                if wait and wait > 0:
                    return False
                controller.signal(Signal.NEWNYM)
                lane.last_circuit_built_ts = time.time()
                return True
        except Exception:  # noqa: BLE001
            return False

    def rebuild_circuits(self, lane: Lane) -> bool:
        """Close general circuits + build a fresh one, blocking until BUILT."""
        if _stem() is None:
            return False
        if not netutil.port_is_open("127.0.0.1", lane.control_port, timeout=0.3):
            return False
        try:
            from stem.control import Controller
            import stem.connection

            with Controller.from_port(port=lane.control_port) as controller:
                stem.connection.authenticate_cookie(
                    controller, str(lane.cookie_path()))
                for circ in controller.get_circuits():
                    flags = set(circ.build_flags or ())
                    if flags & {"INTERNAL", "ONEHOP_TUNNEL"}:
                        continue
                    try:
                        controller.close_circuit(circ.id)
                    except Exception:  # noqa: BLE001
                        pass
                controller.new_circuit(await_build=True)
                lane.last_circuit_built_ts = time.time()
                lane.exit_ip = ""  # refilled by the next health probe
                return True
        except Exception:  # noqa: BLE001
            return False

    # -- burn rotation --------------------------------------------------------
    def rotate_exit_country(self, lane: Lane) -> Optional[str]:
        """Move a lane to a different exit country (destination-side burns
        throttle the whole country persona, not one IP). Returns the new
        country or None when there is nowhere to rotate to."""
        in_use = {l.exit_country for l in self.lanes
                  if l is not lane and not l.sidelined}
        candidates = [c for c in self._rotation_countries
                      if c not in in_use and c != lane.exit_country]
        if not candidates:
            candidates = [c for c in self._rotation_countries
                          if c != lane.exit_country]
        if not candidates:
            return None
        lane.exit_country = candidates[0]
        return candidates[0]

    def healthy_lanes(self) -> List[Lane]:
        return [l for l in self.lanes
                if l.healthy and not l.sidelined and not l.healing]
