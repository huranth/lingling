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
from typing import Optional

from . import __version__, data_dir, proof
from .health import HealthDaemon
from .lanes import TorManager
from .relay import Relay

DATA_DIR = data_dir()
PROOF_LOG = DATA_DIR / "proof.log"

DEFAULT_COUNTRIES = ["us", "de", "nl", "fr", "ro", "gb", "ca", "se", "pl", "ch"]


def load_countries(path: Optional[Path] = None) -> tuple:
    """Country override: <data dir>/countries.txt, line 1 primary, line 2
    fallback, line 3 preferred. Falls back to DEFAULT_COUNTRIES.

    ``#`` starts a comment, and a comment-only line disappears entirely -- it
    must not count as one of the three pools. A blank line still does: that is
    how you skip a pool."""
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
        self._first: str = ""
        self._lock = threading.Lock()

    def set(self, detail: str = "") -> None:
        with self._lock:
            self._detail = detail

    def steady(self, msg: str) -> None:
        """Pin one line from second zero: a cold first run never gets the
        kitchen phrases. They are warm-boot flavour, and on a machine that
        is still fetching tor they read as "this tool is slow" -- the one
        false impression a new user can form before the real message
        arrives. The pinned line holds until real progress replaces it."""
        with self._lock:
            self._first = msg
            self._detail = msg

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
                first = self._first
            if detail:
                msg = _c(detail, "38;5;114")
            elif first:
                msg = _c(first, "38;5;114")
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


def _boot_gate(manager: TorManager, first, daemon: HealthDaemon, loader,
               download_limit: float = 120, quick_limit: float = 30,
               deep_limit: float = 90, deadline_s: float = 600) -> bool:
    """Wait for the first lane, escalating only at genuine dead air.

    A cold cache makes this gate different from a warm one: fetching the
    relay descriptors takes minutes on a fresh install, and tor writes them
    incrementally -- so a MOVING cache is progress, not a stall, and a
    restart over it resets the download each cycle. That restart loop was
    every fresh user's first boot before this gate could see what tor was
    doing: the [circ,edge] sink never carried a percent, so a healthy
    mid-download tor read as stuck at 0%.

    Two evidence sources now feed it, both cheap:

      * ``boot.log`` -- a plain-notice sink written by the lane config, and
        the only place bootstrap percentages are visible at all;
      * ``cache_mtime`` -- descriptor file mtimes, which move exactly while
        tor is fetching. Log lines can be suppressed; a growing 36 MB cache
        cannot.

    Escalation holds off while either moves, and a tor that is silent in
    BOTH is the only thing that gets poked. Returns True once the lane
    answers, False when the deadline passed without one."""
    deadline = time.time() + deadline_s
    last_pct = -1
    last_pct_at = time.time()
    last_cache = manager.cache_mtime(first)
    cache_moved = False
    pokes = 0
    while time.time() < deadline:
        code = daemon.reachable(first)
        if code == 429:
            daemon.on_refused(first, code)
        elif code:
            first.healthy = True
            return True
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
        # stuck checks
        stall = time.time() - last_pct_at
        limit = download_limit if (cache_moved and pct <= 10) else \
            (quick_limit if pct <= 10 else deep_limit)
        if pct == last_pct and stall > limit and pokes < 3:
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
    """True when no lane holds a descriptor cache yet.

    Read from the filesystem alone, BEFORE the spinner starts: which line
    a first run shows cannot wait on a manager, or the flavour lines get
    their frame in first and the user reads the wait as slowness."""
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
    loader.start()
    manager: TorManager | None = None
    daemon: HealthDaemon | None = None
    relay: Relay | None = None
    direct = opts["no_tor"]

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
                    """Say so when a lane did not come up.

                    `start_lanes` has always computed this status per lane and
                    then called `on_lane` only `if on_lane` -- and no caller
                    ever passed one, so the whole outcome was discarded. A lane
                    whose launch failed therefore produced no event at all: the
                    pane stayed silent and `start.lanes` still advertised the
                    full pool. Found as a soak that ran **all 46 calls on lane
                    6** with no lane events anywhere."""
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
            return _run_opencode(opencode, opts["passthrough"], None)

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
