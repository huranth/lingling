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
from .relay import Relay

DATA_DIR = data_dir()
PROOF_LOG = DATA_DIR / "proof.log"

DEFAULT_COUNTRIES = ["us", "de", "nl", "fr", "ro", "gb", "ca", "se", "pl", "ch"]


def _load_countries() -> tuple:
    """Private override: <data dir>/countries.txt, line 1 primary, line 2
    fallback, line 3 preferred. Never shipped."""
    path = DATA_DIR / "countries.txt"
    if path.exists():
        try:
            # Physical line positions matter: blank lines stay blank so a
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
    """Self-rewriting status line: kitchen phrase, or real boot news when set."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._detail: str = ""
        self._lock = threading.Lock()

    def set(self, detail: str = "") -> None:
        with self._lock:
            self._detail = detail

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
            with self._lock:
                detail = self._detail
            if detail:
                msg = _c(detail, "38;5;114")
            else:
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

                # Gate: wait until lane 1 provably carries traffic. A slow
                # boot is NOT a dead boot -- track progress, poke on stall.
                deadline = time.time() + 600
                last_pct = -1
                last_pct_at = time.time()
                pokes = 0
                while time.time() < deadline:
                    verdict = daemon.probe_lane(first)
                    if verdict == "healthy":
                        first.healthy = True
                        first.unhealthy_cycles = 0
                        break
                    pct = manager.lane_bootstrap_pct(first)
                    if pct > last_pct:
                        last_pct = pct
                        last_pct_at = time.time()
                        loader.set(f"tor {pct}%")
                    # Stuck signatures: 0-10% (never reached the network)
                    # pokes at 30s; >10% (no path through the pinned country)
                    # pokes at 90s. Ladder: restart, unpin, re-cook; a second
                    # unpin is a no-op so >10% re-cooks on poke 2.
                    stall = time.time() - last_pct_at
                    limit = 30 if pct <= 10 else 90
                    if pct == last_pct and stall > limit and pokes < 3:
                        pokes += 1
                        if pokes == 1 and pct <= 10:
                            loader.set("tor not responding -- restarting")
                            manager.restart_lane(first)
                        elif pokes <= 2 and (pct <= 10 or pokes == 1):
                            loader.set(f"tor stuck at {pct}% -- "
                                       "unpinning country")
                            manager.unpin_lane(first)
                        else:
                            loader.set("tor stuck -- re-cooking lane")
                            manager.regenerate_lane(first)
                        last_pct = -1
                        last_pct_at = time.time()
                    if verdict == "burned":
                        first.burned_cycles += 1
                        daemon._heal_burn(first)
                    time.sleep(2)
                if first.healthy is not True:
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
