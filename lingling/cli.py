"""lingling -- official OpenCode, riding rotating Tor lanes"""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from . import __version__, bounds, data_dir, proof
from .health import ExitSweep, HealthDaemon
from .lanes import TorManager
from .relay import Relay
from . import netutil
from . import update


def _other_lingling() -> int:
    """The pid of a live lingling holding the one-session lock, else 0."""
    return netutil.session_lock(DATA_DIR) or 0

DATA_DIR = data_dir()
PROOF_LOG = DATA_DIR / "proof.log"

DEFAULT_COUNTRIES = ["us", "de", "nl", "fr", "ro", "gb", "ca", "se", "pl", "ch"]

DEFAULT_LANES = 10

MAX_LANES = 64


def load_countries(path: Optional[Path] = None) -> tuple:
    """Country override: <data dir>/countries.txt, one country per line."""
    path = path or (DATA_DIR / "countries.txt")
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
            raw = []
            for line in text.splitlines():
                if "#" in line:
                    line = line.split("#", 1)[0]
                    if not line.strip():
                        continue
                raw.append(line)
            raw += [""] * (3 - len(raw))
            pools = [[c.strip().lower() for c in raw[i].split(",")
                      if len(c.strip()) == 2 and c.strip().isalpha()]
                     for i in range(3)]
            primary, fallback, preferred = pools
            if primary:
                return primary, fallback, preferred
        except OSError:
            pass
    return DEFAULT_COUNTRIES, [], []

_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _c(text: str, code: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


class _Loader:
    """One pinned line for the whole boot: cold or warm, plus a ticking clock."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._detail: str = ""
        self._first: str = ""
        self._t0 = time.time()
        self._lock = threading.Lock()

    def set(self, detail: str = "") -> None:
        with self._lock:
            self._detail = detail

    def steady(self, msg: str) -> None:
        """Pin one line from second zero: a cold first run never gets the kitchen phrases."""
        with self._lock:
            self._first = msg
            self._detail = msg

    def start(self) -> None:
        if not sys.stdout.isatty():
            return
        self._t0 = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """Percent only repaints when tor advances, so a five second gap reads as a hang."""
        tick = itertools.count()
        while not self._stop.is_set():
            t = next(tick)
            spin = _c(_SPIN[t % len(_SPIN)], "1;38;5;220")
            with self._lock:
                detail = self._detail
                first = self._first
            msg = _c(detail or first or "starting", "38;5;114")
            secs = _c(f"{int(time.time() - self._t0)}s", "90")
            sys.stdout.write(f"\r\x1b[K {spin} {msg} {secs}")
            sys.stdout.flush()
            time.sleep(0.09)

    def stop(self, final: str = "") -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        if sys.stdout.isatty():
            sys.stdout.write("\r\x1b[K")
            if final:
                sys.stdout.write(final + "\n")
            sys.stdout.flush()


def _lane_count(raw: str) -> int:
    """``--lanes``, held inside what one machine can actually run."""
    try:
        asked = int(raw)
    except ValueError:
        print(f"lingling: --lanes {raw} is not a number -- using "
              f"{DEFAULT_LANES}")
        return DEFAULT_LANES
    if asked < 1:
        print(f"lingling: --lanes {asked} is below 1 -- using 1")
        return 1
    if asked > MAX_LANES:
        print(f"lingling: --lanes {asked} is above {MAX_LANES} -- using "
              f"{MAX_LANES}")
        return MAX_LANES
    return asked


def _report_bounds() -> None:
    """Say what a hand-set knob got clamped to, before anything boots."""
    for note in bounds.notes:
        print(f"lingling: {note}")


def _parse_args(argv: list[str]) -> dict:
    """Split lingling's own flags from args passed through to opencode."""
    opts = {
        "lanes": bounds.count("LINGLING_TOR_COUNT", DEFAULT_LANES,
                              1, MAX_LANES),
        "no_tor": False,
        "no_proof": False,
        "proof_tail": None,
        "demo": False,
        "passthrough": [],
    }
    skip = False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a == "--proof":
            opts["proof_tail"] = argv[i + 1] if i + 1 < len(argv) else ""
            skip = True
        elif a == "--demo":
            opts["demo"] = True
        elif a == "--lanes":
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                opts["lanes"] = _lane_count(argv[i + 1])
                opts["lanes_explicit"] = True
                skip = True
        elif a == "--no-tor":
            opts["no_tor"] = True
        elif a == "--no-proof":
            opts["no_proof"] = True
        else:
            opts["passthrough"].append(a)
    return opts


def _boot_gate(manager: TorManager, first, daemon: HealthDaemon,
               download_limit: float = 120, quick_limit: float = 30,
               deep_limit: float = 45, deadline_s: float = 600) -> bool:
    """Wait for the first lane: progress first, probe only when tor is done"""
    deadline = time.time() + deadline_s
    last_pct = -1
    last_pct_at = time.time()
    last_cache = manager.cache_mtime(first)
    cache_moved = False
    pokes = 0
    while time.time() < deadline:
        pct = manager.lane_bootstrap_pct(first)
        cache_at = manager.cache_mtime(first)
        if pct > last_pct:
            last_pct = pct
            last_pct_at = time.time()
        elif cache_at > last_cache:
            last_cache = cache_at
            cache_moved = True
            last_pct_at = time.time()
        if pct >= 100:
            code = daemon.reachable(first, probe_timeout=8)
            if code == 429:
                daemon.refuse(first, code)
            elif code:
                first.healthy = True
                return True
        stall = time.time() - last_pct_at
        limit = download_limit if (cache_moved and pct <= 10) else \
            (quick_limit if pct <= 10 else deep_limit)
        if stall > limit and pokes < 3:
            pokes += 1
            if pct >= 100:
                manager.rotate_exit_country(first)
                manager.restart_lane(first)
            elif pokes == 1:
                manager.restart_lane(first, repin=True)
            elif pokes == 2:
                manager.rotate_exit_country(first)
                manager.restart_lane(first)
            else:
                manager.regenerate_lane(first)
            last_pct = -1
            last_pct_at = time.time()
        time.sleep(2)
    return first.healthy is True


def _cache_is_cold(data_dir: Path) -> bool:
    """True when no lane holds a descriptor cache yet."""
    try:
        return not any((data_dir / "lanes").glob("tor-*/cached-microdesc*"))
    except OSError:
        return True


def _resolve_geoip() -> Optional[Path]:
    """The geoip file the tor bundle ships, wherever this bundle put it."""
    for c in (DATA_DIR / "tools" / "tor" / "data" / "geoip",):
        if c.is_file():
            return c
    try:
        for p in (DATA_DIR / "tools").rglob("geoip"):
            if p.is_file():
                return p
    except OSError:
        pass
    return None


def _migrate_countries() -> None:
    """One-time: pins from the old LOCALAPPDATA dir follow to the new one."""
    if os.name != "nt":
        return
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return
    old = Path(base) / "lingling"
    if old.resolve() == DATA_DIR.resolve():
        return
    src = old / "countries.txt"
    dst = DATA_DIR / "countries.txt"
    if src.exists() and not dst.exists():
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
        except OSError:
            pass


def _dir_size(path: Path) -> int:
    """Total bytes under a path."""
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    except OSError:
        pass
    return total


def _human(n: float) -> str:
    """Bytes in human form."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


UNINSTALL_HELP = """
lingling uninstall -- wipe everything lingling put on disk

pip removes only the package, which is 18 files in site-packages. The
lanes, the Tor bundle, the geoip database and the proof logs live in the
data dir, and pip does not know they exist -- so a plain `pip uninstall`
leaves hundreds of megabytes and a stale relay cache behind, which is
what makes the next install look like a warm relaunch.

This deletes lanes, tor.exe, the relay cache, MITM certs and proof logs.
Keeps countries.txt (and its backups) so your lane pins survive.
Refuses to run while tor or opencode are still alive -- half a boot is
what leaves half-deleted files behind.
The pip package itself is removed the usual way, afterwards:

    lingling uninstall
    pip uninstall lingling

Add --yes to skip the confirmation prompt.
"""


def _process_table() -> tuple:
    """Every process on this machine: its name, and who started it."""
    if os.name != "nt":
        names: dict = {}
        parents: dict = {}
        try:
            entries = os.listdir("/proc")
        except OSError:
            return {}, {}
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open("/proc/" + entry + "/comm", "r") as fh:
                    name = fh.read().strip()
                with open("/proc/" + entry + "/stat", "r") as fh:
                    # comm may contain spaces and parens: split after the last ')'
                    fields = fh.read().rsplit(")", 1)[1].split()
                ppid = int(fields[1])
            except (OSError, IndexError, ValueError):
                continue
            names[int(entry)] = name
            parents[int(entry)] = ppid
        return names, parents
    import ctypes
    import ctypes.wintypes as wt
    TH32CS_SNAPPROCESS = 0x2

    class _PENTRY(ctypes.Structure):
        """The real PROCESSENTRY32, byte for byte."""
        _fields_ = [("dwSize", wt.DWORD),
                    ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    k.Process32First.restype = wt.BOOL
    k.Process32First.argtypes = [wt.HANDLE, ctypes.POINTER(_PENTRY)]
    k.Process32Next.restype = wt.BOOL
    k.Process32Next.argtypes = [wt.HANDLE, ctypes.POINTER(_PENTRY)]
    k.CloseHandle.argtypes = [wt.HANDLE]
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    parents = {}
    names = {}
    if snap:
        entry = _PENTRY()
        entry.dwSize = ctypes.sizeof(_PENTRY)
        if k.Process32First(snap, ctypes.byref(entry)):
            while True:
                parents[entry.th32ProcessID] = entry.th32ParentProcessID
                names[entry.th32ProcessID] = (
                    entry.szExeFile.decode("utf-8", "replace").lower())
                if not k.Process32Next(snap, ctypes.byref(entry)):
                    break
        k.CloseHandle(snap)
    return names, parents


def _lineage(names: dict, parents: dict) -> set:
    """This process and the lingling exe that launched it."""
    mine = {os.getpid()}
    pid = parents.get(os.getpid())
    while pid and pid in parents and len(mine) < 8:
        if names.get(pid, "").startswith("lingling"):
            mine.add(pid)
        pid = parents.get(pid)
    return mine


def _cmdline_uses_data_dir(pid: int) -> bool:
    """True when a tor process is one of our lanes (it points at our data dir)."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            cmd = fh.read().decode("utf-8", "replace").replace("\x00", " ")
    except OSError:
        return False
    return str(DATA_DIR) in cmd


def _running_children() -> list:
    """Lingling's helpers that lock the data dir, as (name, count)."""
    names, parents = _process_table()
    if not names:
        return []
    mine = _lineage(names, parents)
    counts = {}
    for pid, name in names.items():
        if pid in mine:
            continue
        if name in ("tor.exe", "opencode.exe", "lingling.exe",
                    "tor", "lingling"):
            if name == "tor" and not _cmdline_uses_data_dir(pid):
                # a tor that is not one of our lanes (the system daemon, for
                # one) does not lock our data dir
                continue
            counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items())


def _opencode_already_open() -> list:
    """OpenCode windows that were running before the handoff, by pid.

    Their background service is the one opencode keeps between windows, and
    the window that started it hands it that window's environment -- so a
    service already up cannot be given the lanes' proxy by restarting it.
    Nothing then reaches the relay: every call rides the machine's own address
    while the lanes sit idle, and the pane has nothing to show.  Only a fresh
    window, started by lingling, spawns a service that inherits the proxy.
    """
    names, parents = _process_table()
    if not names:
        return []
    mine = _lineage(names, parents)
    return sorted(pid for pid, name in names.items()
                  if name in ("opencode.exe", "opencode") and pid not in mine)


def _uninstall(rest: list[str]) -> int:
    """Wipe the data dir cleanly: children stop first, then it deletes."""
    if "--help" in rest or "-h" in rest:
        print(UNINSTALL_HELP)
        return 0
    running = _running_children()
    if running:
        listing = ", ".join(f"{n} x{c}" for n, c in running)
        print(f"lingling is still running ({listing}).")
        print("Close it first -- uninstalling mid-boot is what leaves "
              "half-deleted tor files behind.")
        return 1
    if not DATA_DIR.exists():
        print("nothing to remove -- no lingling data dir on this machine.")
        return 0
    keep = {p.name for p in DATA_DIR.iterdir()
            if p.name.startswith("countries")}
    entries = []
    for entry in sorted(DATA_DIR.iterdir()):
        if entry.name in keep:
            continue
        size = entry.stat().st_size if entry.is_file() else _dir_size(entry)
        entries.append((entry, size))
    if not entries:
        print("nothing to remove -- only the countries override is left.")
        return 0
    print("lingling uninstall -- this deletes:")
    for entry, size in entries:
        print(f"  {entry.name:<24} {_human(size):>10}")
    total = sum(size for _, size in entries)
    if "--yes" not in rest:
        if not sys.stdin.isatty():
            print("refusing to wipe without --yes in a non-interactive shell.")
            return 1
        try:
            answer = input(f"delete all of it ({_human(total)})? [y/N] ")
        except EOFError:
            print("refusing to wipe without --yes in a non-interactive shell.")
            return 1
        if answer.strip().lower() not in ("y", "yes"):
            print("cancelled -- nothing was touched.")
            return 1
    freed = 0
    stuck = []
    for entry, size in entries:
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            freed += size
        except OSError:
            stuck.append(entry.name)
    print(f"wiped {_human(freed)}.")
    left = [p.name for p in sorted(DATA_DIR.iterdir())]
    if left:
        print(f"kept: {', '.join(left)}")
    if stuck:
        print(f"could not delete (in use? close lingling and retry): "
              f"{', '.join(stuck)}")
    if not left:
        try:
            DATA_DIR.rmdir()
        except OSError:
            pass
    print("now remove the package itself:  pip uninstall lingling")
    return 0


def _say_update(check_obj) -> None:
    """The update notice, the one question, and the one install"""
    info = check_obj.info if check_obj is not None else None
    ask = update.askable(info)
    update.emit(check_obj, ask=ask)
    if check_obj is not None:
        update.auto_install(DATA_DIR, info, ask=ask)


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("uninstall", "--uninstall"):
        return _uninstall(argv[1:])
    if argv and argv[0] in ("update", "--update"):
        return update.update_command(argv[1:])
    opts = _parse_args(argv)

    if opts["proof_tail"] is not None:
        return proof.tail(Path(opts["proof_tail"] or str(PROOF_LOG)))

    if opts["demo"]:
        from .demo import run_demo
        question = " ".join(opts["passthrough"]).strip() or (
            "In one sentence: who are you, and what exit node do you think "
            "this request came from?")
        lanes = opts["lanes"] if opts.get("lanes_explicit") else 2
        try:
            other = _other_lingling()
        except OSError as exc:
            print(f"lingling: cannot claim session lock: {exc}")
            return 1
        if other:
            print("lingling is already running -- one kitchen, one session.")
            return 1
        try:
            return run_demo(question, lanes=lanes)
        finally:
            netutil.release_session_lock(DATA_DIR)

    if "--version" in opts["passthrough"] or "-v" in opts["passthrough"]:
        print(f"lingling {__version__} (wraps opencode)")
        update.cached_hint(DATA_DIR)
        return 0

    _report_bounds()
    opencode = shutil.which("opencode")
    if opencode is None:
        print("lingling: couldn't find `opencode` on your PATH.")
        print("Install it first (https://opencode.ai) and re-run.")
        return 1

    from . import mitm
    if not mitm.crypto_available():
        print("lingling: the `cryptography` package is missing or broken.")
        print("Reinstall it:  pip install --force-reinstall cryptography")
        return 1

    try:
        other = _other_lingling()
    except OSError as exc:
        print(f"lingling: cannot claim session lock: {exc}")
        return 1
    if other:
        print(_c(f"lingling is already running (pid {other}) -- one kitchen, "
                 f"one session. Close that window first.", "33"))
        return 1

    try:
        return _session(opts, opencode)
    finally:
        netutil.release_session_lock(DATA_DIR)


def _session(opts: dict, opencode: str) -> int:
    _migrate_countries()
    _spawn_geoip_fallback()
    update_check = update.start(DATA_DIR)
    loader = _Loader()
    if _cache_is_cold(DATA_DIR):
        loader.steady("first run -- pulling the relay directory, "
                      "a few minutes, once")
    else:
        loader.steady("warming the lanes -- under a minute")
    loader.start()
    manager: TorManager | None = None
    daemon: HealthDaemon | None = None
    sweep: ExitSweep | None = None
    relay: Relay | None = None
    direct = opts["no_tor"]
    end_code: list = []
    emit = None

    try:
        if not direct:
            countries, fallback, preferred = load_countries()
            manager = TorManager(
                DATA_DIR, count=opts["lanes"],
                exit_countries=countries,
                fallback_countries=fallback,
                preferred_countries=preferred,
                tor_exe=os.environ.get("LINGLING_TOR_EXE", ""),
                log=lambda *a: None,
                prune_orphans=True,
            )
            err = manager.setup_lanes()
            if err:
                loader.stop(_c(f" !! tor unavailable ({err}) -- going direct",
                               "33"))
                direct = True
            else:
                emit = proof.make_emitter(PROOF_LOG)
                manager.sticky.attach(emit)
                manager.attach(emit)
                daemon = HealthDaemon(manager, event=emit, log=lambda *a: None)

                def _report_boot(lane, status) -> None:
                    """Say so when a lane did not come up."""
                    if status in ("started", "already_running"):
                        return
                    emit({"type": "lane", "kind": "fail", "t": time.time(),
                          "lane": lane.index, "cc": lane.exit_country,
                          "ip": "",
                          "msg": f"lane {lane.index} {{{lane.exit_country}}} "
                                 f"did not come up ({status}) -- the health "
                                 f"daemon will keep trying"})

                first = manager.lanes[0]
                manager.start_lanes([first], on_lane=_report_boot)

                if not _boot_gate(manager, first, daemon):
                    loader.stop(_c(" !! the kitchen stayed cold -- "
                                   "going direct", "31"))
                    direct = True
                    manager.stop_all()
                else:
                    daemon.start()
                    sweep = ExitSweep(manager, event=emit,
                                      log=lambda *a: None)
                    sweep.start()

        if direct:
            loader.stop()
            print(_c("lingling: no lanes -- opencode rides your own IP.\n",
                     "33"))
            _say_update(update_check)
            code = _run_opencode(opencode, opts["passthrough"], None)
            end_code.append(code)
            return code

        if emit is None:
            emit = proof.make_emitter(PROOF_LOG)
        emit({"type": "start", "t": time.time(),
              "session": os.urandom(6).hex(), "lanes": len(manager.lanes),
              "countries": list(manager.countries), "version": __version__})
        relay = Relay(manager, event=emit)
        port = relay.start()

        ca_pem = None
        if os.environ.get("LINGLING_NO_MITM", "").lower() not in ("1", "true"):
            try:
                from . import mitm
                relay.cert_shop = mitm.CertShop(DATA_DIR / "mitm")
                relay.tunnels = mitm.TunnelPool()
                ca_pem = relay.cert_shop.ca_pem_path
            except Exception:  # noqa: BLE001
                pass

        loader.stop(_c(" served! lanes are hot -- proof is in the other "
                       "window.", "1;32"))
        _say_update(update_check)
        if not opts["no_proof"]:
            proof.spawn_proof_window(PROOF_LOG)

        rest = manager.lanes[1:]
        if rest:
            def _cook_rest() -> None:
                emit({"type": "lane", "kind": "heal", "t": time.time(),
                      "lane": 0, "cc": "", "ip": "",
                      "msg": f"cooking {len(rest)} more lanes in the "
                             f"background ..."})
                manager.start_lanes(rest, on_lane=_report_boot)

            threading.Thread(target=_cook_rest, name="lane-cook",
                             daemon=True).start()

        stale = _opencode_already_open()
        if stale:
            listed = ", ".join(str(pid) for pid in stale)
            print(_c(f" !! OpenCode was already running ({listed}) -- its "
                     f"background service predates these lanes and will not "
                     f"use them.", "31"), flush=True)
            print(_c("    This session's server was restarted under the "
                     "lanes, so it rides one; those other windows still "
                     "ride your own address.", "33"), flush=True)

        env = dict(os.environ)
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            env[var] = f"http://127.0.0.1:{port}"
        env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1"
        if ca_pem:
            env["NODE_EXTRA_CA_CERTS"] = str(ca_pem)
        code = _run_opencode(opencode, opts["passthrough"], env)
        end_code.append(code)
        return code
    finally:
        try:
            if sweep:
                sweep.stop()
            if daemon:
                daemon.stop()
        finally:
            try:
                if relay:
                    relay.stop()
            finally:
                if manager and not direct:
                    manager.stop_all()
        if emit:
            try:
                if end_code:
                    c = end_code[0]
                    if c == 130:
                        msg = "session ended by Ctrl+C"
                    elif c:
                        msg = (f"opencode exited on its own (code {c}) "
                               f"while the lanes were still hot")
                    else:
                        msg = "opencode closed the session -- bye"
                    emit({"type": "lane", "kind": "fail" if c not in (0, 130)
                          else "", "t": time.time(), "lane": 0, "cc": "",
                          "ip": "", "msg": msg})
                emit(proof.DONE)
                emit.close()
            except Exception:  # noqa: BLE001
                pass


def _spawn_geoip_fallback() -> None:
    """One background copy of the bundle geoip into the data dir's root"""
    dst = DATA_DIR / "geoip"
    if dst.exists():
        return
    src = _resolve_geoip()
    if src is None:
        return

    def _copy() -> None:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        except OSError:
            pass

    threading.Thread(target=_copy, name="geoip-seed", daemon=True).start()


_SERVER_ERA: Optional[bool] = None


def _uses_background_server(binary: str) -> bool:
    """From 2.0.26 the TUI is only a client"""
    global _SERVER_ERA
    if _SERVER_ERA is None:
        try:
            out = subprocess.run([binary, "--help"], capture_output=True,
                                 text=True, encoding="utf-8",
                                 errors="replace", timeout=15,
                                 **netutil._quiet()).stdout or ""
            _SERVER_ERA = "--standalone" in out
        except Exception:  # noqa: BLE001
            _SERVER_ERA = False
    return _SERVER_ERA


def _server(binary: str, env: dict, action: str) -> None:
    """Windows does not hand a detached server the proxy it was given."""
    names = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
             "NO_PROXY", "no_proxy", "NODE_EXTRA_CA_CERTS")
    saved = {name: os.environ.get(name) for name in names}
    for name in names:
        if env.get(name) is not None:
            os.environ[name] = env[name]
    try:
        subprocess.run([binary, "service", action], capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=60, **netutil._quiet())
    except Exception:  # noqa: BLE001
        pass
    finally:
        for name, old in saved.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


_SUBCOMMANDS = ("run", "serve", "acp", "api", "mcp", "models", "auth",
                "debug", "plugin", "stats", "service", "reload", "upgrade",
                "update", "uninstall", "pair")


def _has_standalone(args: list[str]) -> bool:
    """True when the caller already asked for a private server."""
    return any(a == "--standalone" for a in args)


def _with_standalone(args: list[str]) -> list[str]:
    """Put --standalone where the opencode parser actually reads it.

    opencode only honours the flag AFTER its subcommand: `opencode run
    --standalone` works, `opencode --standalone run` is parsed as a global
    flag and the subcommand keeps talking to the shared service.
    """
    if _has_standalone(args):
        return args
    for i, a in enumerate(args):
        if a in _SUBCOMMANDS:
            return args[:i + 1] + ["--standalone"] + args[i + 1:]
    return ["--standalone", *args]


def _run_opencode(binary: str, args: list[str], env: dict | None) -> int:
    """Exec opencode with stdio inherited so it owns the terminal."""
    print(_c(" handoff: starting opencode -- its TUI loads quietly for a "
             "few seconds.", "90"), flush=True)
    server = env is not None and _uses_background_server(binary)
    if server:
        _server(binary, env, "restart")
    if (server and os.name != "nt" and not _has_standalone(args)
            and os.environ.get("LINGLING_NO_STANDALONE", "") != "1"):
        # The background server keeps the environment it was born with, so a
        # client that attaches to it leaves on the bare address while the
        # kitchen looks healthy. On Windows the way out is to restart that
        # server under our environment. On Linux the service is SHARED: every
        # client on the machine talks to it, so ending it to restart it
        # would break every other session and any agent already running.
        # A private server does the same job without touching the shared
        # one, and it inherits the proxy and the MITM CA.
        args = _with_standalone(args)
    try:
        try:
            proc = subprocess.Popen([binary, *args], env=env)
        except OSError as exc:
            print(f"lingling: couldn't launch opencode: {exc}")
            return 1
        try:
            code = proc.wait()
        except KeyboardInterrupt:
            print(_c(" Ctrl+C -- closing the kitchen.", "90"), flush=True)
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            return 130
    finally:
        if server:
            _server(binary, env, "stop")
    if code != 0:
        print(_c(f" opencode exited (code {code}) -- the lanes stayed hot "
                 f"the whole time; run lingling again to ride.", "33"),
              flush=True)
    return code


def entry() -> None:
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print(_c(" Ctrl+C -- closing the kitchen.", "90"))
        sys.exit(130)
