"""lingling -- official OpenCode, riding rotating Tor lanes.

CLI entrypoint: boots Tor lanes, starts the local relay, then execs opencode
with HTTPS_PROXY pointed at it; all other args pass through untouched.
"""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__, data_dir, proof
from .health import HealthDaemon
from .lanes import TorManager
from .netutil import pid_alive, port_is_open
from .relay import Relay

DATA_DIR = data_dir()
PROOF_LOG = DATA_DIR / "proof.log"
LOCK_FILE = DATA_DIR / "lingling.lock"

DEFAULT_COUNTRIES = ["us", "de", "nl", "fr", "ro", "gb", "ca", "se", "pl", "ch"]


def _load_countries() -> tuple:
    """Private override: <data dir>/countries.txt with a primary CSV list on
    line 1, an optional fallback pool on line 2, and an optional preferred
    pool on line 3 (lanes stick to preferred countries until those countries
    accumulate too many bad exits). Never shipped."""
    path = DATA_DIR / "countries.txt"
    if path.exists():
        try:
            # Physical line positions matter: line 1 primary, line 2
            # fallback, line 3 preferred. Blank lines stay blank so a
            # skipped pool can't shift the lines below it.
            raw = path.read_text(encoding="utf-8").splitlines()
            raw += [""] * (3 - len(raw))
            pools = [[c.strip() for c in raw[i].split(",") if c.strip()]
                     for i in range(3)]
            primary, fallback, preferred = pools
            if primary:
                return primary, fallback, preferred
        except OSError:
            pass
    return DEFAULT_COUNTRIES, [], []

_KITCHEN_LINES = [
    "cooking the lanes", "baking it", "warming the exits",
    "glazing the tunnel", "seasoning the circuits", "proofing the dough",
    "preheating the relays", "tasting the traffic",
]

_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_PHRASE_COLORS = ["38;5;215", "38;5;222", "38;5;180", "38;5;173",
                  "38;5;114", "38;5;109", "38;5;139", "38;5;175"]


def _c(text: str, code: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


class _Loader:
    """Self-rewriting status line; deliberately vibe-only, no lane counts or progress bar."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def set(self, detail: str = "") -> None:
        # Kept for API compatibility; details are intentionally not shown.
        pass

    def start(self) -> None:
        if not sys.stdout.isatty():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        tick = itertools.count()
        while not self._stop.is_set():
            t = next(tick)
            spin = _c(_SPIN[t % len(_SPIN)], "1;38;5;220")
            idx = (t // 24) % len(_KITCHEN_LINES)
            msg = _c(_KITCHEN_LINES[idx], _PHRASE_COLORS[idx])
            sys.stdout.write(f"\r\x1b[K {spin} {msg}")
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
            try:
                opts["lanes"] = max(1, int(argv[i + 1]))
                opts["lanes_explicit"] = True
            except (IndexError, ValueError):
                pass
            skip = True
        elif a == "--no-tor":
            opts["no_tor"] = True
        elif a == "--no-proof":
            opts["no_proof"] = True
        else:
            opts["passthrough"].append(a)
    return opts


def _singleton() -> bool:
    """Own a single-instance lock file in the data dir. Returns True on
    success; False if another lingling is managing the same lanes. Stale
    locks (dead PID) are reclaimed. `--proof` tailing is exempt (it never
    boots lanes) and handles only reads, so it doesn't take the lock."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if LOCK_FILE.exists():
            try:
                other = int(LOCK_FILE.read_text(encoding="utf-8").strip())
            except ValueError:
                other = 0
            if other and other != os.getpid() and pid_alive(other):
                print(_c(
                    f"lingling: another instance is already running (PID "
                    f"{other}) and owns the lanes.\n"
                    f"  To use lingling here, close the other one first "
                    f"(Ctrl+C it, or `taskkill /PID {other} /T`).",
                    "31"))
                return False
            # Stale lock from a dead instance (crash, force-kill): reclaim.
            LOCK_FILE.unlink(missing_ok=True)
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:
        print(_c(f"lingling: could not write the lane lock: {exc}", "33"))
        return False
    import atexit
    atexit.register(_drop_lock)
    return True


def _drop_lock() -> None:
    """Remove the lock file only if it still names our PID (a newer
    instance may have reclaimed it after a crash)."""
    try:
        if LOCK_FILE.exists():
            cur = LOCK_FILE.read_text(encoding="utf-8").strip()
            if cur == str(os.getpid()):
                LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def main(argv: list[str]) -> int:
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

    # The real run owns the lanes; a second instance must not boot another
    # health daemon over the same tor.exe set. The PID lock only catches
    # NEW (2.1.3+) instances -- an older, lock-less build leaves the base
    # lane ports bound instead. Detect that too, so we never spiral into
    # two parallel lane sets racing the same upstream.
    if not opts["no_tor"] and not _singleton():
        return 1
    if not opts["no_tor"] and any(
            port_is_open("127.0.0.1", port) for port in (52001, 52002)):
        print(_c(
            "lingling: lane ports 52001/52002 are already bound -- an "
            "older lingling is probably running and owns the lanes.\n"
            "  Close it first (Ctrl+C it, or `taskkill /IM lingling.exe "
            "/T`) and re-run.",
            "31"))
        return 1

    opencode = shutil.which("opencode")
    if opencode is None:
        print("lingling: couldn't find `opencode` on your PATH.")
        print("Install it first (https://opencode.ai) and re-run.")
        return 1

    loader = _Loader()
    loader.start()
    manager: TorManager | None = None
    daemon: HealthDaemon | None = None
    relay: Relay | None = None
    direct = opts["no_tor"]

    try:
        if not direct:
            countries, fallback, preferred = _load_countries()
            manager = TorManager(
                DATA_DIR, count=opts["lanes"],
                exit_countries=countries,
                fallback_countries=fallback,
                preferred_countries=preferred,
                tor_exe=os.environ.get("LINGLING_TOR_EXE", ""),
                log=lambda *a: None,
            )
            loader.set()
            err = manager.setup_lanes()
            if err:
                loader.stop(_c(f" !! tor unavailable ({err}) -- going direct",
                               "33"))
                direct = True
            else:
                emit = proof.make_emitter(PROOF_LOG)
                daemon = HealthDaemon(manager, event=emit,
                                      log=lambda *a: None)

                # Boot order: only lane 1 cooks in the foreground; the rest follow in background.
                first = manager.lanes[0]
                manager.start_lanes([first])

                # Block until lane 1 provably carries traffic, else the user's first prompt dies.
                deadline = time.time() + 150
                while time.time() < deadline:
                    verdict = daemon.probe_lane(first)
                    if verdict == "healthy":
                        first.healthy = True
                        first.unhealthy_cycles = 0
                        break
                    if verdict == "burned":
                        first.burned_cycles += 1
                        daemon._heal_burn(first)
                    time.sleep(2)
                if first.healthy is not True:
                    loader.stop(_c(" !! the kitchen stayed cold -- "
                                   "going direct", "31"))
                    direct = True
                    manager.stop_all()
                daemon.start()

        if direct:
            loader.stop()
            print(_c("lingling: no lanes -- opencode rides your own IP.\n",
                     "33"))
            return _run_opencode(opencode, opts["passthrough"], None)

        emit = proof.make_emitter(PROOF_LOG)
        relay = Relay(manager, event=emit)
        port = relay.start()

        # Per-request proof via local TLS termination; best-effort, falls back to blind tunnels.
        ca_pem = None
        if os.environ.get("LINGLING_NO_MITM", "").lower() not in ("1", "true"):
            try:
                from . import mitm
                relay.cert_shop = mitm.CertShop(DATA_DIR / "mitm")
                ca_pem = relay.cert_shop.ca_pem_path
            except Exception:  # noqa: BLE001
                pass

        loader.stop(_c(" served! lanes are hot -- proof is in the other "
                       "window.", "1;32"))
        if not opts["no_proof"]:
            proof.spawn_proof_window(PROOF_LOG)

        # Remaining lanes bootstrap in background; health probes join them to rotation as they come up.
        rest = manager.lanes[1:]
        if rest:
            def _cook_rest() -> None:
                for lane in rest:
                    emit({"type": "lane", "kind": "heal", "t": time.time(),
                          "lane": lane.index, "cc": lane.exit_country,
                          "ip": "",
                          "msg": f"lane {lane.index} {{{lane.exit_country}}} "
                                 f"registering in the background ..."})
                manager.start_lanes(rest)

            threading.Thread(target=_cook_rest, name="lane-cook",
                             daemon=True).start()

        env = dict(os.environ)
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            env[var] = f"http://127.0.0.1:{port}"
        env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1"
        if ca_pem:
            # Let opencode trust our local CA so we can log each model call.
            env["NODE_EXTRA_CA_CERTS"] = str(ca_pem)
        return _run_opencode(opencode, opts["passthrough"], env)
    finally:
        if daemon:
            daemon.stop()
        if relay:
            relay.stop()
        if manager and not direct:
            manager.stop_all()
        if not direct:
            try:
                proof.make_emitter(PROOF_LOG)(proof.DONE)
            except Exception:  # noqa: BLE001
                pass


def _run_opencode(binary: str, args: list[str],
                  env: dict | None) -> int:
    """Exec opencode with stdio inherited so it owns the terminal."""
    try:
        proc = subprocess.Popen([binary, *args], env=env)
    except OSError as exc:
        print(f"lingling: couldn't launch opencode: {exc}")
        return 1
    try:
        return proc.wait()
    except KeyboardInterrupt:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        return 130


def entry() -> None:
    sys.exit(main(sys.argv[1:]))
