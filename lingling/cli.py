"""lingling -- official OpenCode, riding rotating Tor lanes.

    lingling [anything opencode accepts]

Boot order: download tor (first run only) -> cook the lanes -> probe each
lane against the real upstream until at least one is verifiably carrying
traffic -> start the local relay -> open the proof window -> exec opencode
with HTTPS_PROXY pointed at the relay. Everything you type after `lingling`
is handed to opencode untouched, so every flag, subcommand and future
opencode feature just works.
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

from . import __version__, netutil, proof
from .health import HealthDaemon
from .lanes import TorManager
from .relay import Relay

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PROOF_LOG = DATA_DIR / "proof.log"

DEFAULT_COUNTRIES = ["us", "de", "nl", "fr", "ro", "gb", "ca", "se", "pl", "ch"]

_KITCHEN_LINES = [
    "cooking the lanes", "baking it", "warming the exits",
    "glazing the tunnel", "seasoning the circuits", "proofing the dough",
    "preheating the relays", "tasting the traffic",
]

_BAR_WIDTH = 22
_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class _Loader:
    """Single self-rewriting status line: spinner + rectangular bar + a
    message that rotates through the kitchen lines."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._msg_idx = itertools.count()
        self._detail = ""
        self._done = 0
        self._total = 1
        self._lock = threading.Lock()

    def set(self, detail: str = "", done: int | None = None,
            total: int | None = None) -> None:
        with self._lock:
            if detail:
                self._detail = detail
            if done is not None:
                self._done = done
            if total is not None:
                self._total = max(1, total)

    def start(self) -> None:
        if not sys.stdout.isatty():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        tick = itertools.count()
        while not self._stop.is_set():
            with self._lock:
                done, total, detail = self._done, self._total, self._detail
            t = next(tick)
            spin = _SPIN[t % len(_SPIN)]
            msg = _KITCHEN_LINES[(t // 24) % len(_KITCHEN_LINES)]
            filled = round(_BAR_WIDTH * done / total)
            bar = "▓" * filled + "░" * (_BAR_WIDTH - filled)
            suffix = f"  {detail}" if detail else ""
            sys.stdout.write(f"\r\x1b[K {spin} {msg} [{bar}] {done}/{total}{suffix}")
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
    """Pull lingling's own flags out; everything else passes to opencode."""
    opts = {
        "lanes": int(os.environ.get("LINGLING_TOR_COUNT", "5") or 5),
        "no_tor": False,
        "no_proof": False,
        "proof_tail": None,
        "passthrough": [],
    }
    it = iter(range(len(argv)))
    skip = False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a == "--proof":
            opts["proof_tail"] = argv[i + 1] if i + 1 < len(argv) else ""
            skip = True
        elif a == "--lanes":
            try:
                opts["lanes"] = max(1, int(argv[i + 1]))
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


def main(argv: list[str]) -> int:
    opts = _parse_args(argv)

    if opts["proof_tail"] is not None:
        return proof.tail(Path(opts["proof_tail"] or str(PROOF_LOG)))

    if "--version" in opts["passthrough"] or "-v" in opts["passthrough"]:
        print(f"lingling {__version__} (wraps opencode)")
        return 0

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
            manager = TorManager(
                DATA_DIR, count=opts["lanes"],
                exit_countries=DEFAULT_COUNTRIES,
                tor_exe=os.environ.get("LINGLING_TOR_EXE", ""),
                log=lambda *a: None,
            )
            loader.set(detail="checking the tor kit", done=0,
                       total=len(manager.lanes))
            err = manager.setup_lanes()
            if err:
                loader.stop(f" !! tor unavailable ({err}) -- going direct")
                direct = True
            else:
                done_n = 0

                def _on_lane(lane, status):
                    nonlocal done_n
                    done_n += 1
                    loader.set(detail=f"lane {lane.index} {{{lane.exit_country}}} "
                                      f"{status}", done=done_n)

                manager.start_all(on_lane=_on_lane)

                emit = proof.make_emitter(PROOF_LOG)
                daemon = HealthDaemon(manager, event=emit,
                                      log=lambda *a: None)
                # Block until at least one lane provably carries traffic --
                # better to cook a few extra seconds here than to hand the
                # user a session whose first prompt dies.
                deadline = time.time() + 150
                while time.time() < deadline:
                    for lane in manager.lanes:
                        if lane.healthy is not True and not lane.sidelined:
                            verdict = daemon.probe_lane(lane)
                            if verdict == "healthy":
                                lane.healthy = True
                                lane.unhealthy_cycles = 0
                            elif verdict == "burned":
                                lane.burned_cycles += 1
                                daemon._heal_burn(lane)
                    ready = manager.healthy_lanes()
                    loader.set(detail=f"{len(ready)} lane(s) verified",
                               done=len(manager.lanes))
                    if ready:
                        break
                    time.sleep(2)
                if not manager.healthy_lanes():
                    loader.stop(" !! no lane came up -- going direct")
                    direct = True
                    manager.stop_all()
                daemon.start()

        if direct:
            loader.stop()
            print("lingling: no lanes -- opencode rides your own IP.\n")
            return _run_opencode(opencode, opts["passthrough"], None)

        emit = proof.make_emitter(PROOF_LOG)
        relay = Relay(manager, event=emit)
        port = relay.start()

        loader.stop(f" ok -- {len(manager.healthy_lanes())} lane(s) cooking "
                    f"on 127.0.0.1:{port}")
        if not opts["no_proof"]:
            proof.spawn_proof_window(PROOF_LOG)

        env = dict(os.environ)
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            env[var] = f"http://127.0.0.1:{port}"
        env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1"
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
    """Exec opencode with stdio fully inherited -- it owns the terminal."""
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
