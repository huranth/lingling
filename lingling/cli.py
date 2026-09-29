"""lingling -- official OpenCode, riding rotating Tor lanes."""

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

from . import __version__, data_dir, proof
from .health import HealthDaemon
from .lanes import TorManager
from .relay import Relay

DATA_DIR = data_dir()
PROOF_LOG = DATA_DIR / "proof.log"

DEFAULT_COUNTRIES = ["us", "de", "nl", "fr", "ro", "gb", "ca", "se", "pl", "ch"]


def load_countries(path: Optional[Path] = None) -> tuple:
    """Country override: <data dir>/countries.txt, line 1 primary, line 2 fallback, line 3 preferred."""
    path = path or (DATA_DIR / "countries.txt")
    if path.exists():
        try:
            # line order
            text = path.read_text(encoding="utf-8")
            raw = []
            for line in text.splitlines():
                if "#" in line:
                    line = line.split("#", 1)[0]
                    if not line.strip():
                        continue  # comment only
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
    """Self-rewriting status line: kitchen phrase, or real boot news when set."""

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
        """
        The percent only repaints when tor itself advances, so a five
        second gap reads as a hang. The elapsed counter ticks every
        second whether or not anything else changed -- motion that
        proves the loop is alive, and the honest cost of the wait.
        """
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


def _parse_args(argv: list[str]) -> dict:
    """Split lingling's own flags from args passed through to opencode."""
    opts = {
        "lanes": int(os.environ.get("LINGLING_TOR_COUNT", "5") or 5),
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
            """
            A bare --lanes must not eat the flag after it: swallowing
            --demo or --no-proof here silently flips cold-start messaging
            and proof spawning without the user ever passing a count.
            """
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                try:
                    opts["lanes"] = max(1, int(argv[i + 1]))
                    opts["lanes_explicit"] = True
                    skip = True
                except ValueError:
                    pass
        elif a == "--no-tor":
            opts["no_tor"] = True
        elif a == "--no-proof":
            opts["no_proof"] = True
        else:
            opts["passthrough"].append(a)
    return opts


def _boot_gate(manager: TorManager, first, daemon: HealthDaemon, loader,
               download_limit: float = 120, quick_limit: float = 30,
               deep_limit: float = 90, deadline_s: float = 600) -> bool:
    """Wait for the first lane: progress first, probe only when tor is done."""
    deadline = time.time() + deadline_s
    last_pct = -1
    last_pct_at = time.time()
    last_cache = manager.cache_mtime(first)
    cache_moved = False
    pokes = 0
    while time.time() < deadline:
        """
        Evidence before the probe. The exit probe blocks for its whole
        window against a half-booted tor, and the old order ran it first --
        so the screen froze on tor 0% while tor raced to 100% unobserved,
        and a boot that was 17s from serving read as a hang. Progress is
        read every sweep; the probe runs only at 100%, briefly.
        """
        pct = manager.lane_bootstrap_pct(first)
        cache_at = manager.cache_mtime(first)
        if pct > last_pct:
            last_pct = pct
            last_pct_at = time.time()
            loader.set(f"tor {pct}%")
        elif cache_at > last_cache:
            # downloading
            last_cache = cache_at
            cache_moved = True
            last_pct_at = time.time()
            loader.set("fetching the relay directory -- first start only")
        if pct >= 100:
            loader.set("checking the exit")
            code = daemon.reachable(first, probe_timeout=8)
            if code == 429:
                daemon.on_refused(first, code)
            elif code:
                first.healthy = True
                return True
        """
        A dead exit at 100% escalates on the same ladder as a stalled
        boot: the probe failing is silence too, just a slower one.
        """
        # stuck checks
        stall = time.time() - last_pct_at
        limit = download_limit if (cache_moved and pct <= 10) else \
            (quick_limit if pct <= 10 else deep_limit)
        if stall > limit and pokes < 3:
            pokes += 1
            if pokes == 1 and pct <= 10:
                loader.set("tor not responding -- restarting")
                manager.restart_lane(first)
            elif pokes <= 2 and (pct <= 10 or pokes == 1):
                loader.set(f"tor stuck at {pct}% -- unpinning country")
                manager.unpin_lane(first)
            else:
                loader.set("tor stuck -- re-cooking lane")
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


def _tor_present(data_dir: Path) -> bool:
    """True when the tor binary is already on disk."""
    name = "tor.exe" if os.name == "nt" else "tor"
    try:
        return any((data_dir / "tools").rglob(name))
    except OSError:
        return False


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

Deletes lanes, tor.exe, the relay cache, MITM certs and proof logs.
Keeps countries.txt (and its backups) so your lane pins survive.
Refuses to run while tor or opencode are still alive -- half a boot
is what leaves half-deleted files behind.
The pip package itself is removed the usual way, afterwards:

    pip uninstall lingling
"""


def _running_children() -> list:
    """Lingling's helpers that lock the data dir, as (name, count)."""
    if os.name != "nt":
        return []
    """
    This uninstaller is itself a lingling.exe, and so is the shim above
    it -- count only *other* boots, by walking up the parent chain through
    the process snapshot. One source of names, so nothing double counts.
    """
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
    mine = {os.getpid()}
    pid = parents.get(os.getpid())
    while pid and pid in parents and len(mine) < 8:
        if names.get(pid, "").startswith("lingling"):
            mine.add(pid)
        pid = parents.get(pid)
    counts = {}
    for pid, name in names.items():
        if pid in mine:
            continue
        if name in ("tor.exe", "opencode.exe", "lingling.exe"):
            counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items())


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
    keep = {"countries.txt"}
    keep |= {p.name for p in DATA_DIR.glob("countries.txt.bak-*")}
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
            # closed stdin
            answer = ""
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


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("uninstall", "--uninstall"):
        return _uninstall(argv[1:])
    opts = _parse_args(argv)

    if opts["proof_tail"] is not None:
        return proof.tail(Path(opts["proof_tail"] or str(PROOF_LOG)))

    if opts["demo"]:
        from .demo import run_demo
        question = " ".join(opts["passthrough"]).strip() or (
            "In one sentence: who are you, and what exit node do you think "
            "this request came from?")
        lanes = opts["lanes"] if opts.get("lanes_explicit") else 2
        return run_demo(question, lanes=lanes)

    if "--version" in opts["passthrough"] or "-v" in opts["passthrough"]:
        print(f"lingling {__version__} (wraps opencode)")
        return 0

    opencode = shutil.which("opencode")
    if opencode is None:
        print("lingling: couldn't find `opencode` on your PATH.")
        print("Install it first (https://opencode.ai) and re-run.")
        return 1

    # fail early
    from . import mitm
    if not mitm.crypto_available():
        print("lingling: the `cryptography` package is missing or broken.")
        print("Reinstall it:  pip install --force-reinstall cryptography")
        return 1

    loader = _Loader()
    if _cache_is_cold(DATA_DIR):
        """
        The first frame decides what a new user believes this tool is. A
        cold data dir means minutes of fetching, so the honest line is
        pinned before the spinner ever ticks and the flavour lines never
        rotate on this run; the boot gate replaces it with live progress.
        A warm machine gets the kitchen phrases as before.
        """
        if _tor_present(DATA_DIR):
            loader.steady("first start -- fetching tor's relay directory "
                          "(one-time, a few minutes)")
        else:
            loader.steady("first start -- downloading tor and the relay "
                          "directory (one-time, a few minutes)")
    else:
        """
        Say the cache is warm: a reinstall of the package never touches
        this data dir, and a fast boot with no explanation reads as the
        cold-start message misfiring.
        """
        loader.steady("warm cache found -- lanes boot fast")
    loader.start()
    manager: TorManager | None = None
    daemon: HealthDaemon | None = None
    relay: Relay | None = None
    direct = opts["no_tor"]
    end_code: list = []

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
            )
            loader.set("starting tor")
            err = manager.setup_lanes()
            if err:
                loader.stop(_c(f" !! tor unavailable ({err}) -- going direct",
                               "33"))
                direct = True
            else:
                emit = proof.make_emitter(PROOF_LOG)
                daemon = HealthDaemon(manager, event=emit,
                                      log=lambda *a: None)

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

                # first lane
                first = manager.lanes[0]
                manager.start_lanes([first], on_lane=_report_boot)
                if manager.cache_mtime(first) == 0.0:
                    loader.set("first cold start -- fetching the relay "
                               "directory")

                if not _boot_gate(manager, first, daemon, loader):
                    loader.stop(_c(" !! the kitchen stayed cold -- "
                                   "going direct", "31"))
                    direct = True
                    manager.stop_all()
                else:
                    daemon.start()

        if direct:
            loader.stop()
            print(_c("lingling: no lanes -- opencode rides your own IP.\n",
                     "33"))
            code = _run_opencode(opencode, opts["passthrough"], None)
            end_code.append(code)
            return code

        emit = proof.make_emitter(PROOF_LOG)
        # session marker
        emit({"type": "start", "t": time.time(),
              "session": os.urandom(6).hex(), "lanes": len(manager.lanes),
              "countries": list(manager.countries), "version": __version__})
        relay = Relay(manager, event=emit)
        port = relay.start()

        # mitm proof
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
        if not opts["no_proof"]:
            proof.spawn_proof_window(PROOF_LOG)

        # background boot
        rest = manager.lanes[1:]
        if rest:
            def _cook_rest() -> None:
                for lane in rest:
                    emit({"type": "lane", "kind": "heal", "t": time.time(),
                          "lane": lane.index, "cc": lane.exit_country,
                          "ip": "",
                          "msg": f"lane {lane.index} {{{lane.exit_country}}} "
                                 f"registering in the background ..."})
                manager.start_lanes(rest, on_lane=_report_boot)

            threading.Thread(target=_cook_rest, name="lane-cook",
                             daemon=True).start()

        env = dict(os.environ)
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            env[var] = f"http://127.0.0.1:{port}"
        env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1"
        if ca_pem:
            # trust CA
            env["NODE_EXTRA_CA_CERTS"] = str(ca_pem)
        code = _run_opencode(opencode, opts["passthrough"], env)
        end_code.append(code)
        return code
    finally:
        if daemon:
            daemon.stop()
        if relay:
            relay.stop()
        if manager and not direct:
            manager.stop_all()
        if not direct:
            try:
                emit = proof.make_emitter(PROOF_LOG)
                if end_code:
                    # session verdict
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
            except Exception:  # noqa: BLE001
                pass


def _run_opencode(binary: str, args: list[str],
                  env: dict | None) -> int:
    """Exec opencode with stdio inherited so it owns the terminal."""
    """
    The TUI takes seconds to paint its first frame and says nothing while
    it does -- a blank screen here read as a crash, and a Ctrl+C into it
    made that true. The handoff line lands before the blank pause starts,
    and the exit line says who ended the session when the child does.
    """
    print(_c(" handoff: starting opencode -- its TUI loads quietly for a "
             "few seconds.", "90"), flush=True)
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
    if code != 0:
        print(_c(f" opencode exited (code {code}) -- the lanes stayed hot "
                 f"the whole time; run lingling again to ride.", "33"),
              flush=True)
    return code


def entry() -> None:
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        """A boot cut short is a choice, not a crash -- no traceback."""
        print(_c(" Ctrl+C -- closing the kitchen.", "90"))
        sys.exit(130)
