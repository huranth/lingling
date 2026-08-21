"""Tor egress lanes: zero-account exit IPs beside the WARP pool.

Measured live: OpenCode answers Tor exits (HTTP 200, ~2s), simultaneous Tor
instances egress from distinct IPs, and restarting an instance picks a new
random route -- a fresh exit in seconds, no registration, no account. Each
instance is one SOCKS5 lane the proxy pool treats like any other proxy.

Why not pin countries per instance: an ``ExitNodes {cc}`` restriction left
second instances stuck building their first circuit for minutes (observed
live), while unpinned instances boot in ~35s and still land on distinct
exits. Random routes, restarted on burn, are both faster and simpler.

Bootstrap cost: the first instance downloads the Tor directory consensus
(~30-60s); later instances clone its cached descriptors and start in seconds.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

TOR_VERSION = "15.0.20"
TOR_DIST = "https://dist.torproject.org/torbrowser"
TOR_BASE_PORT = 52001  # WARP owns 51001+; Tor lanes live one block above

# Directory cache files that can be safely cloned between instances. ``lock``
# and ``state`` are per-process and must never be copied.
_CLONE_FILES = (
    "cached-certs",
    "cached-consensus",
    "cached-descriptors",
    "cached-descriptors.new",
    "cached-microdesc-consensus",
    "cached-microdescs",
    "cached-microdescs.new",
)


class TorInstance:
    """One tor.exe process: its own data dir, SOCKS port, and random exit."""

    def __init__(self, index: int, port: int, data_dir: Path) -> None:
        self.index = index
        self.port = port
        self.data_dir = data_dir
        self.proxy_url = f"socks5://127.0.0.1:{port}"
        self.process: Optional[subprocess.Popen] = None

    @property
    def pid(self) -> str:
        return f"tor-{self.index}"

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None


class TorEgressManager:
    """Owns the lifecycle of N local Tor SOCKS5 egress lanes."""

    def __init__(
        self,
        root_dir: Path,
        count: int,
        base_port: int = TOR_BASE_PORT,
    ) -> None:
        self.root = Path(root_dir)
        self.count = max(0, count)
        self.base_port = base_port
        self._lock = threading.Lock()
        self.instances: List[TorInstance] = [
            TorInstance(
                i + 1,
                base_port + i,
                self.root / "instances" / f"tor-{i + 1}",
            )
            for i in range(self.count)
        ]

    # -- tools ----------------------------------------------------------
    def _tor_path(self) -> Path:
        return self.root / "tools" / "tor" / "tor.exe"

    def tools_ready(self) -> bool:
        return self._tor_path().exists()

    def _latest_stable_version(self, log: Callable[..., Any] = print) -> Optional[str]:
        """Newest stable Tor version advertised under TOR_DIST/, or None.

        The listing also carries alpha tags (e.g. 16.0a9) whose Windows expert
        bundle may not ship for every platform, so match a strict
        major.minor.patch and take the max. Used so a stale ``TOR_VERSION`` pin
        can never 404 the download: the previous hard-coded version rotted the
        moment Tor shipped the next one. The pin is only the fallback when the
        listing itself is unreachable.
        """
        import re
        import urllib.request
        try:
            req = urllib.request.Request(
                f"{TOR_DIST}/", headers={"User-Agent": "lingling"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                html = resp.read().decode("utf-8", "replace")
            found = re.findall(r'href="(\d+)\.(\d+)\.(\d+)/"', html)
            if not found:
                return None
            mx = max((int(a), int(b), int(c)) for a, b, c in found)
            return f"{mx[0]}.{mx[1]}.{mx[2]}"
        except Exception as exc:  # noqa: BLE001
            log(f"[tor] could not read the version listing ({exc})")
            return None

    def _bundle_url(self, version: str) -> str:
        # Verified live: the Windows expert bundle is a .tar.gz at this path
        # (the .zip name 404s), extracting a top-level tor/ directory.
        return f"{TOR_DIST}/{version}/tor-expert-bundle-windows-x86_64-{version}.tar.gz"

    def ensure_tools(self, log: Callable[..., Any] = print) -> bool:
        """Download and extract the Tor expert bundle once.

        Resolves the newest stable version from the dist listing first, with
        ``TOR_VERSION`` as the fallback only when the listing is unreachable,
        so a superseded pin can never 404 the download (which is how Tor lanes
        silently went missing one release). Each candidate is tried in turn;
        an HTTP error moves on to the next rather than aborting the lane pool.
        """
        if self.tools_ready():
            return True
        import urllib.request
        import urllib.error

        tor = self._tor_path()
        tor.parent.mkdir(parents=True, exist_ok=True)
        archive = self.root / "tools" / "tor-expert-bundle.tar.gz"
        # Newest stable first; the pinned default covers a listing outage and
        # is tried even when discovery already returned it (idempotent).
        discovered = self._latest_stable_version(log)
        candidates: List[str] = []
        if discovered and discovered != TOR_VERSION:
            candidates.append(discovered)
        candidates.append(TOR_VERSION)

        log("[tor] grabbing the Tor expert bundle (first run only) ...")
        last = "no download attempted"
        for version in candidates:
            url = self._bundle_url(version)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "lingling"})
                with urllib.request.urlopen(req, timeout=120) as resp, \
                        open(archive, "wb") as out:
                    shutil.copyfileobj(resp, out)
            except urllib.error.HTTPError as exc:
                last = f"v{version}: HTTP {exc.code}"
                log(f"[tor] v{version} download HTTP {exc.code}; trying next candidate")
                continue
            except Exception as exc:  # noqa: BLE001
                last = f"v{version}: {exc}"
                log(f"[tor] v{version} download failed ({exc})")
                continue
            import tarfile
            with tarfile.open(archive, "r:gz") as tf:
                tf.extractall(self.root / "tools")
            if not tor.exists():
                log(f"[tor] extraction did not produce {tor}")
                return False
            archive.unlink(missing_ok=True)
            log(f"[tor] tools ready (v{version}) — dressed and out the door")
            return True
        log(f"[tor] download failed ({last}) — continuing without Tor lanes")
        return False

    # -- instance lifecycle ----------------------------------------------
    def _torrc(self, inst: TorInstance) -> str:
        return (
            f"SocksPort 127.0.0.1:{inst.port}\n"
            f"DataDirectory {inst.data_dir.as_posix()}\n"
            # Quiet: no control port, no client DNS, no socks-on-localhost
            # exceptions. No ExitNodes pin -- see module docstring.
            "SafeSocks 0\n"
            "TestSocks 0\n"
            "Log notice stderr\n"
        )

    def _spawn(self, inst: TorInstance, log: Callable[..., Any] = print) -> None:
        inst.data_dir.mkdir(parents=True, exist_ok=True)
        conf = inst.data_dir / "torrc"
        conf.write_text(self._torrc(inst))
        creation = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        inst.process = subprocess.Popen(
            [str(self._tor_path()), "-f", str(conf)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation,
        )
        # Same kill-on-close discipline as wireproxy: no orphaned tor.exe
        # holding SOCKS ports after the gateway dies.
        try:
            from warp import job
            job.ensure_kill_job()
            job.assign(inst.process.pid)
        except Exception:  # noqa: BLE001
            pass

    def _clone_cache(self, src_dir: Path, dst_dir: Path, log: Callable[..., Any]) -> bool:
        """Copy cached directory info between instance dirs, file by file.

        tor.exe memory-maps its cache files on Windows, so copying from a
        *running* instance fails with WinError 1224 ("user-mapped section").
        Callers must clone only from dirs whose process is stopped; a per-file
        failure is logged and skipped, which just costs that instance a slow
        network bootstrap instead of failing the lane.
        """
        dst_dir.mkdir(parents=True, exist_ok=True)
        copied = False
        for name in _CLONE_FILES:
            src = src_dir / name
            if not src.exists():
                continue
            try:
                shutil.copy2(src, dst_dir / name)
                copied = True
            except OSError as exc:
                log("[tor] cache clone skipped %s (%s)", name, exc)
        return copied

    def _cache_ready(self, inst: TorInstance) -> bool:
        # A consensus + some descriptors is enough to build circuits fast.
        return (inst.data_dir / "cached-microdesc-consensus").exists() or \
            (inst.data_dir / "cached-consensus").exists()

    def _stop(self, inst: TorInstance) -> None:
        if inst.process is not None:
            try:
                inst.process.kill()
                inst.process.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            inst.process = None

    def start_all(self, log: Callable[..., Any] = print) -> Dict[str, Any]:
        """Start every lane, with a cache-safe seeding order.

        Fresh machine: bootstrap the first instance alone, STOP it (its cache
        files are memory-mapped while it runs), clone its cache into the other
        dirs, then start everything. Warm machine: every dir already holds a
        cache, no copying happens at all.
        """
        if not self.tools_ready():
            return {"started": 0, "failed": self.count, "error": "tools missing"}
        with self._lock:
            started = failed = 0
            seed_dir = next(
                (i.data_dir for i in self.instances
                 if not i.is_running() and self._cache_ready(i)),
                None,
            )
            if seed_dir is None and self.instances:
                # Fresh machine: bring one instance up just long enough to
                # fetch the directory, then stop it for safe cloning.
                first = self.instances[0]
                try:
                    self._spawn(first, log)
                    deadline = time.time() + 90
                    while time.time() < deadline and not self._cache_ready(first):
                        if first.process is None or first.process.poll() is not None:
                            break
                        time.sleep(2)
                except Exception as exc:  # noqa: BLE001
                    log(f"[tor] bootstrap spawn failed: {exc}")
                    failed += 1
                if self._cache_ready(first):
                    seed_dir = first.data_dir
                self._stop(first)   # unmap its cache before anyone reads it
            if seed_dir is not None:
                for inst in self.instances:
                    if not self._cache_ready(inst):
                        self._clone_cache(seed_dir, inst.data_dir, log)
            for inst in self.instances:
                if inst.is_running():
                    started += 1
                    continue
                try:
                    self._spawn(inst, log)
                    started += 1
                except Exception as exc:  # noqa: BLE001
                    log(f"[tor] failed to start #{inst.index}: {exc}")
                    failed += 1
            return {"started": started, "failed": failed}

    def restart_instance(self, inst: TorInstance, log: Callable[..., Any] = print) -> bool:
        """Kill and relaunch one lane — its route is re-picked, so a burned
        exit IP is replaced by a fresh one in seconds."""
        with self._lock:
            if inst.process is not None:
                try:
                    inst.process.kill()
                    inst.process.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
                inst.process = None
            try:
                self._spawn(inst, log)
                return True
            except Exception as exc:  # noqa: BLE001
                log(f"[tor] restart failed for #{inst.index}: {exc}")
                return False

    def stop_all(self) -> Dict[str, Any]:
        stopped = 0
        with self._lock:
            for inst in self.instances:
                if inst.process is not None:
                    try:
                        inst.process.kill()
                        inst.process.wait(timeout=5)
                        stopped += 1
                    except Exception:  # noqa: BLE001
                        pass
                    inst.process = None
        return {"stopped": stopped}

    # -- pool integration --------------------------------------------------
    def sync_to_pool(self, proxy_pool: Any, log: Callable[..., Any] = print) -> int:
        """Add running lanes to the proxy pool (idempotent)."""
        added = 0
        for inst in self.instances:
            if not inst.is_running():
                continue
            if proxy_pool.get_by_id(inst.pid) is None:
                proxy_pool.add(
                    inst.proxy_url,
                    proxy_id=inst.pid,
                    label=f"Tor exit lane #{inst.index}",
                )
                added += 1
        if added:
            log("[tor] %d lanes folded into the proxy pool", added)
        return added

    def status(self) -> Dict[str, Any]:
        with self._lock:
            running = sum(1 for i in self.instances if i.is_running())
            return {
                "count": self.count,
                "tools_ready": self.tools_ready(),
                "running": running,
                "base_port": self.base_port,
                "instances": [
                    {"index": i.index, "port": i.port, "running": i.is_running()}
                    for i in self.instances
                ],
            }
