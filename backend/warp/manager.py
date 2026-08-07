"""Cloudflare WARP egress rotation -- free, unlimited, OpenCode-specific.

OpenCode rate-limits its free tier by IP. Cloudflare WARP gives every account a
fresh exit IP, and accounts are free and unlimited. So we register N separate
WARP identities (via `wgcf`), turn each into a local SOCKS5 proxy (via
`wireproxy`), and feed them all into Lingling's ProxyPool. When OpenCode 429s
one WARP IP, the pool cools it and rotates to the next -- automatically.

Mirrors github.com/MrAlony/oc-quota (WARP + wireproxy + 429 interceptor), but
native to Lingling: no Rust, no .bat, no 9Router. Just Python + the two WARP
tools. Architecture (all on 127.0.0.1):

    Lingling ProxyPool picks one of:
        socks5://127.0.0.1:51001  --> wireproxy #1 --> WARP identity #1 --> opencode.ai
        socks5://127.0.0.1:51002  --> wireproxy #2 --> WARP identity #2 --> opencode.ai
        ...

Each WARP identity = a different Cloudflare exit IP. All free, all unlimited.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from warp import job

# Cloudflare WARP WireGuard constants (public, fixed for all WARP peers).
WARP_PEER_PUBLIC_KEY = "bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo="
WARP_ENDPOINT = "engage.cloudflareclient.com:2408"
WARP_DNS = "1.1.1.1, 1.0.0.1"

WGCF_REPO = "ViRb3/wgcf"
WIREPROXY_REPO = "windtf/wireproxy"

BASE_PORT = 51001  # first wireproxy SOCKS5 listener; +1 per identity


def _port_is_open(host: str, port: int, timeout: float = 0.2) -> bool:
    """Return True if a TCP connection to host:port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _identity_config_ok(inst: "WarpInstance") -> bool:
    """Check that an identity has a valid key and both config files."""
    if not inst.private_key or not inst.address_v4:
        return False
    return (
        (inst.identity_dir / "wgcf-profile.conf").exists()
        and (inst.identity_dir / "wireproxy.conf").exists()
    )


def _find_free_port(
    start_port: int,
    host: str = "127.0.0.1",
    max_offset: int = 50,
    reserved: Optional[Any] = None,
) -> int:
    """Find the next free TCP port at or after start_port.

    ``reserved`` is a set of ports that are spoken for even though nothing is
    listening on them right now. Without it a regenerating identity could take a
    *sibling's* assigned port: siblings are frequently down at exactly the moment
    the health daemon is regenerating, so their ports scan as free. Both configs
    then wrote the same BindAddress and whichever wireproxy started second failed
    to bind, losing an exit until someone noticed.
    """
    taken = reserved or frozenset()
    for offset in range(max_offset):
        port = start_port + offset
        if port in taken:
            continue
        if not _port_is_open(host, port):
            return port
    raise RuntimeError(f"no free TCP port found near {start_port} on {host}")


@dataclass
class WarpInstance:
    """One WARP identity + its local wireproxy SOCKS5 listener."""
    index: int
    port: int
    identity_dir: Path
    proxy_url: str
    process: Optional[subprocess.Popen] = None
    private_key: str = ""
    address_v4: str = ""
    address_v6: str = ""

    def _is_running(self) -> bool:
        """Check if wireproxy is running - either we're tracking it or the port is open."""
        # If we're tracking the process, use that
        if self.process is not None and self.process.poll() is None:
            return True

        # For externally started processes, check if the port is listening
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.1)  # Very short timeout to avoid hanging
            result = sock.connect_ex(("127.0.0.1", self.port))
            sock.close()
            return result == 0
        except OSError:
            # Only socket errors mean "not running". A bare `except:` here also
            # swallowed KeyboardInterrupt, so Ctrl-C during a health sweep could
            # be eaten silently.
            return False

    def status(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "port": self.port,
            "proxy_url": self.proxy_url,
            "running": self._is_running(),
            "has_identity": bool(self.private_key),
            "pid": self.process.pid if self.process else None,
        }


class WarpManager:
    """Owns the lifecycle of N local WARP-backed SOCKS5 proxies."""

    # Go runtime tuning for wireproxy.exe children.
    # GOMEMLIMIT: hard cap on Go heap; GC collects aggressively near this
    # limit.  25 MiB is the sweet spot: Go's runtime overhead is ~15 MB
    # and a SOCKS5 proxy needs ~10 MB heap.  Measured: 32% RSS reduction
    # vs. untuned (308 MB → 210 MB for 10 identities).
    # GOMAXPROCS=1: one OS thread per tunnel (it only forwards, no
    # parallelism needed).  Saves ~2-3 MB of thread stack per process.
    _WIREPROXY_ENV = {**os.environ, "GOMEMLIMIT": "25MiB", "GOMAXPROCS": "1"}

    def __init__(self, root_dir: Path, count: int = 5) -> None:
        self.root = Path(root_dir)
        self.tools_dir = self.root / "tools"
        self.identities_dir = self.root / "identities"
        self.count = max(1, count)
        self.instances: List[WarpInstance] = []
        self._lock = threading.Lock()
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self.identities_dir.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    def _load_existing(self) -> None:
        """Re-load any identities created in a previous run.

        Reads the actual SOCKS5 port from each identity's wireproxy.conf
        (if it exists) instead of hardcoding BASE_PORT + i. This preserves
        port migrations from previous runs — e.g. if identity #1 was migrated
        from 51001 to 51003 because 51001/51002 were occupied, the correct
        port is restored on restart.

        Duplicates are repaired rather than trusted. A pre-fix ``regenerate``
        could hand one identity a sibling's port, and both configs then held the
        same BindAddress; reloading that state left two instances believing they
        owned one port, so ``_pid_on_port``/``_kill_pid`` acted on each other's
        process and the pool wrote one URL under two ids. The second claimant is
        moved to a free port instead.
        """
        seen_ports: set = set()
        for i in range(self.count):
            ident_dir = self.identities_dir / f"warp-{i + 1}"
            # Read the actual port from the existing wireproxy.conf, if any.
            port = BASE_PORT + i
            wireproxy_conf = ident_dir / "wireproxy.conf"
            if wireproxy_conf.exists():
                for line in wireproxy_conf.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("BindAddress"):
                        # Format: BindAddress = 127.0.0.1:51003
                        port_str = line.split("=", 1)[1].strip().rsplit(":", 1)[-1]
                        try:
                            port = int(port_str)
                        except ValueError:
                            pass
                        break
            if port in seen_ports:
                try:
                    port = _find_free_port(BASE_PORT + i, reserved=seen_ports)
                except RuntimeError:
                    # No free port near the default range. The old fallback
                    # reset to BASE_PORT + i -- which is exactly the port that
                    # just collided -- so the duplicate survived and two
                    # identities kept one BindAddress. Move above the highest
                    # port already claimed instead.
                    port = (max(seen_ports) + 1) if seen_ports else (BASE_PORT + i)
                    while port in seen_ports or _port_is_open("127.0.0.1", port):
                        port += 1
            seen_ports.add(port)
            inst = WarpInstance(
                index=i + 1,
                port=port,
                identity_dir=ident_dir,
                proxy_url=f"socks5://127.0.0.1:{port}",
            )
            prof = ident_dir / "wgcf-profile.conf"
            if prof.exists():
                inst.private_key, inst.address_v4, inst.address_v6 = _parse_profile(prof.read_text())
            self.instances.append(inst)


    # -- platform helpers --------------------------------------------------
    def _is_windows(self) -> bool:
        return platform.system() == "Windows"

    def _ext(self) -> str:
        return ".exe" if self._is_windows() else ""

    def _wgcf_path(self) -> Path:
        return self.tools_dir / f"wgcf{self._ext()}"

    def _wireproxy_path(self) -> Path:
        return self.tools_dir / f"wireproxy{self._ext()}"

    def tools_ready(self) -> bool:
        return self._wgcf_path().exists() and self._wireproxy_path().exists()

    # -- tools -------------------------------------------------------------
    def ensure_tools(self, log=print) -> Dict[str, Any]:
        """Download wgcf and wireproxy if missing. Returns a status dict."""
        results: Dict[str, Any] = {"wgcf": None, "wireproxy": None, "downloaded": []}
        if self.tools_ready():
            results["wgcf"] = str(self._wgcf_path())
            results["wireproxy"] = str(self._wireproxy_path())
            return results
        if not self._wgcf_path().exists():
            log("[warp] downloading wgcf ...")
            self._download_latest(self._wgcf_path(), WGCF_REPO, _pick_wgcf_asset)
            results["downloaded"].append("wgcf")
        if not self._wireproxy_path().exists():
            log("[warp] downloading wireproxy ...")
            self._download_latest(self._wireproxy_path(), WIREPROXY_REPO, _pick_wireproxy_asset)
            results["downloaded"].append("wireproxy")
        results["wgcf"] = str(self._wgcf_path()) if self._wgcf_path().exists() else None
        results["wireproxy"] = str(self._wireproxy_path()) if self._wireproxy_path().exists() else None
        return results

    def _download_latest(self, dest: Path, repo: str, picker) -> None:
        """Fetch the latest release asset matching picker(asset_name) -> bool."""
        import ssl
        import urllib.request
        
        # Create SSL context that verifies certificates
        # This handles Windows SSL certificate verification issues
        ctx = ssl.create_default_context()
        
        api = f"https://api.github.com/repos/{repo}/releases/latest"
        try:
            with urllib.request.urlopen(api, timeout=30, context=ctx) as r:
                release = json.loads(r.read().decode())
        except Exception:
            # Fallback: use unverified context if default SSL fails
            # This happens when Python can't verify GitHub's certificates
            ctx_unverified = ssl.create_default_context()
            ctx_unverified.check_hostname = False
            ctx_unverified.verify_mode = ssl.CERT_NONE
            try:
                with urllib.request.urlopen(api, timeout=30, context=ctx_unverified) as r:
                    release = json.loads(r.read().decode())
            except Exception as e:
                raise RuntimeError(f"failed to fetch release info from GitHub API: {e}")
        
        asset = None
        for a in release.get("assets", []):
            if picker(a["name"]):
                asset = a
                break
        if asset is None:
            raise RuntimeError(f"no suitable asset in {repo} latest release")
        
        # Download under the asset's real name so _is_archive() can see the
        # extension (downloading as 'wireproxy.exe.tmp' hides the .tar.gz).
        tmp = self.tools_dir / asset["name"]
        
        try:
            urllib.request.urlretrieve(asset["browser_download_url"], tmp)
        except Exception:
            # If standard download fails, retry with unverified context
            try:
                req = urllib.request.Request(asset["browser_download_url"])
                req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
                
                ctx_unverified = ssl.create_default_context()
                ctx_unverified.check_hostname = False
                ctx_unverified.verify_mode = ssl.CERT_NONE
                
                opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx_unverified))
                urllib.request.install_opener(opener)
                urllib.request.urlretrieve(asset["browser_download_url"], tmp)
            except Exception as e:
                raise RuntimeError(f"failed to download asset: {e}")
        
        if _is_archive(tmp.name):
            _extract_binary(tmp, dest, binary_name=dest.stem)
            tmp.unlink(missing_ok=True)
        else:
            tmp.replace(dest)
        if not self._is_windows():
            os.chmod(dest, 0o755)

    # -- WARP identity registration (wgcf) ---------------------------------
    def setup_identities(self, log=print) -> Dict[str, Any]:
        """Register WARP accounts + wireproxy configs for all N slots.

        Idempotent: slots that already have a complete, valid identity are skipped.
        Broken or missing identities (no private key, no IPv4, or missing config
        files) are wiped and re-registered. Registrations run in parallel.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        self.ensure_tools(log=log)

        def _prepare_and_register(inst: WarpInstance) -> None:
            if _identity_config_ok(inst):
                return
            # Broken/incomplete: wipe and start fresh
            if inst.identity_dir.exists():
                shutil.rmtree(inst.identity_dir)
            inst.private_key = ""
            inst.address_v4 = ""
            inst.address_v6 = ""
            self._register_one(inst, log=log)

        results: List[Dict[str, Any]] = []
        work: List[WarpInstance] = []
        for inst in self.instances:
            if _identity_config_ok(inst):
                results.append({"index": inst.index, "status": "exists", "port": inst.port})
            else:
                work.append(inst)

        # Register one at a time. Cloudflare WARP rate-limits account creation,
        # and running wgcf register in parallel often only succeeds for the first
        # identity before the rest get throttled.
        with ThreadPoolExecutor(max_workers=1) as pool:
            futures = {pool.submit(_prepare_and_register, inst): inst for inst in work}
            for future in as_completed(futures):
                inst = futures[future]
                try:
                    future.result()
                    results.append({"index": inst.index, "status": "registered", "port": inst.port})
                except Exception as exc:  # noqa: BLE001
                    results.append({"index": inst.index, "status": "error", "error": str(exc)})
        return {"results": results, "tools": self.tools_ready()}

    def _register_one(self, inst: WarpInstance, log=print) -> None:
        d = inst.identity_dir
        d.mkdir(parents=True, exist_ok=True)
        wgcf = str(self._wgcf_path())
        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                log(f"[warp] registering identity #{inst.index} (attempt {attempt}/3) ...")
                _run([wgcf, "register", "--accept-tos"], cwd=d)
                _run([wgcf, "generate"], cwd=d)
                prof = (d / "wgcf-profile.conf").read_text()
                inst.private_key, inst.address_v4, inst.address_v6 = _parse_profile(prof)
                (d / "wireproxy.conf").write_text(_wireproxy_conf(
                    inst.private_key, inst.address_v4, inst.address_v6, inst.port,
                ))
                log(f"[warp] identity #{inst.index} ready on 127.0.0.1:{inst.port}")
                return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                log(f"[warp] identity #{inst.index} attempt {attempt} failed: {exc}")
                if attempt < 3:
                    time.sleep(2 ** attempt)  # 2s, 4s backoff
        raise last_err if last_err else RuntimeError("registration failed")

    # -- process lifecycle -------------------------------------------------
    def start_all(self, log=print) -> Dict[str, Any]:
        """Launch a wireproxy process for every configured identity.

        Broken identities are re-registered on the fly. If a configured port is
        occupied by a foreign process, the instance is migrated to the next free
        port and its wireproxy.conf is rewritten.
        """
        self.ensure_tools(log=log)
        started, skipped, failed, reregistered = 0, 0, 0, 0
        details: List[Dict[str, Any]] = []

        for inst in self.instances:
            detail: Dict[str, Any] = {"index": inst.index, "port": inst.port, "status": "pending"}

            # Drop dead process handles so we don't confuse a stale pid with life
            if inst.process is not None and inst.process.poll() is not None:
                inst.process = None

            # Re-register identities with broken/missing configs
            if not _identity_config_ok(inst):
                if inst.identity_dir.exists():
                    shutil.rmtree(inst.identity_dir)
                inst.private_key = ""
                inst.address_v4 = ""
                inst.address_v6 = ""
                try:
                    log(f"[warp] re-registering broken identity #{inst.index} ...")
                    self._register_one(inst, log=log)
                    reregistered += 1
                    detail["reregistered"] = True
                except Exception as exc:  # noqa: BLE001
                    log(f"[warp] failed to re-register #{inst.index}: {exc}")
                    detail.update({"status": "failed", "error": f"re-register failed: {exc}"})
                    details.append(detail)
                    failed += 1
                    continue

            proc_alive = inst.process is not None and inst.process.poll() is None
            already_running = inst._is_running() and _identity_config_ok(inst)
            port_open = _port_is_open("127.0.0.1", inst.port)

            if proc_alive or already_running:
                # Already running (either tracked or detected via port check).
                # Previously, an externally-started wireproxy (no tracked process)
                # would trigger a false "port occupied -> migrate" here. Now we
                # detect it via _is_running() + valid config and skip instead.
                # Edge case: a foreign process on the exact WARP port with valid
                # config on disk skips too — acceptably rare.
                skipped += 1
                detail.update({"status": "skipped", "running": True})
                details.append(detail)
                continue

            if port_open:
                # Port held by a foreign/orphan process that is NOT our wireproxy:
                # migrate to the next free port.
                try:
                    new_port = _find_free_port(inst.port + 1)
                    log(f"[warp] port {inst.port} occupied for #{inst.index}, moving to {new_port}")
                    inst.port = new_port
                    inst.proxy_url = f"socks5://127.0.0.1:{new_port}"
                    (inst.identity_dir / "wireproxy.conf").write_text(
                        _wireproxy_conf(inst.private_key, inst.address_v4, inst.address_v6, new_port)
                    )
                    detail["port"] = new_port
                except Exception as exc:  # noqa: BLE001
                    log(f"[warp] failed to relocate port for #{inst.index}: {exc}")
                    detail.update({"status": "failed", "error": f"port relocation failed: {exc}"})
                    details.append(detail)
                    failed += 1
                    continue

            conf = inst.identity_dir / "wireproxy.conf"
            if not conf.exists():
                detail.update({"status": "failed", "error": "missing wireproxy.conf"})
                details.append(detail)
                failed += 1
                continue

            try:
                inst.process = subprocess.Popen(
                    [str(self._wireproxy_path()), "-c", str(conf)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=self._WIREPROXY_ENV,
                    creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
                    if self._is_windows() else 0,
                    start_new_session=not self._is_windows(),
                )
                # Kill-on-close job: when this gateway dies (window closed, crash,
                # taskkill), Windows kills the wireproxy children with it instead
                # of leaving them to hold the ports forever.
                job.ensure_kill_job()
                if not job.assign(inst.process.pid):
                    log(f"[warp] WARNING: wireproxy #{inst.index} could not join the kill-job; it will orphan on exit")

                started += 1
                detail.update({"status": "started", "pid": inst.process.pid})
            except Exception as exc:  # noqa: BLE001
                log(f"[warp] failed to start #{inst.index}: {exc}")
                detail.update({"status": "failed", "error": f"start failed: {exc}"})
                details.append(detail)
                failed += 1
                continue

            details.append(detail)

        # Wireproxy needs time to bring up the WireGuard tunnel and bind its
        # SOCKS5 port. Poll all configured ports for up to ~25 seconds before
        # deciding an identity failed to start.
        all_open = False
        for _ in range(50):
            all_open = True
            for inst in self.instances:
                if not _identity_config_ok(inst):
                    continue
                if not _port_is_open("127.0.0.1", inst.port, timeout=0.5):
                    all_open = False
                    break
            if all_open:
                break
            time.sleep(0.5)

        for inst in self.instances:
            if not _identity_config_ok(inst):
                continue
            opened = _port_is_open("127.0.0.1", inst.port, timeout=0.5)
            if not opened:
                for d in details:
                    if d.get("index") == inst.index and d.get("status") == "started":
                        d.update({"status": "failed", "error": "SOCKS5 port did not open"})
                        failed += 1
                        started -= 1
                        break

        return {
            **self.status(),
            "started": started,
            "skipped": skipped,
            "failed": failed,
            "reregistered": reregistered,
            "details": details,
        }

    def stop_all(self) -> Dict[str, Any]:
        """Stop all tracked wireproxy processes and evict orphans from our ports."""
        stopped = 0
        for inst in self.instances:
            if inst.process and inst._is_running():
                try:
                    if self._is_windows():
                        inst.process.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        inst.process.terminate()
                    inst.process.wait(timeout=5)
                    stopped += 1
                except Exception:  # noqa: BLE001
                    try:
                        inst.process.kill()
                        stopped += 1
                    except Exception:  # noqa: BLE001
                        pass
            inst.process = None

        # Evict any orphan process (e.g. from a previous Lingling run) that is
        # still listening on one of our WARP ports. Without this, refresh leaves
        # stale wireproxy instances alive and new ones cannot bind.
        for inst in self.instances:
            if _port_is_open("127.0.0.1", inst.port):
                pid = _pid_on_port(inst.port)
                if pid:
                    _kill_pid(pid, grace_s=2)
                    stopped += 1

        # Allow the OS to reclaim the ports before a subsequent start_all().
        time.sleep(0.3)
        return {"stopped": stopped, **self.status()}

    # -- single-instance lifecycle ------------------------------------------
    def restart_instance(self, inst: WarpInstance, log=print) -> bool:
        """Restart wireproxy for a single instance.

        Stops the existing process (if any) and starts a fresh one.
        Returns True if the SOCKS5 port becomes open within ~6 seconds.
        """
        # Stop existing process
        if inst.process is not None:
            try:
                if self._is_windows():
                    inst.process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    inst.process.terminate()
                inst.process.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    inst.process.kill()
                except Exception:  # noqa: BLE001
                    pass
            inst.process = None

        # Kill any orphan on our port
        if _port_is_open("127.0.0.1", inst.port):
            pid = _pid_on_port(inst.port)
            if pid:
                _kill_pid(pid, grace_s=2)
            time.sleep(0.2)

        # Start fresh
        conf = inst.identity_dir / "wireproxy.conf"
        if not conf.exists():
            log(f"[warp] restart_instance #{inst.index}: missing wireproxy.conf")
            return False

        if not _identity_config_ok(inst):
            log(f"[warp] restart_instance #{inst.index}: broken identity config")
            return False

        try:
            inst.process = subprocess.Popen(
                [str(self._wireproxy_path()), "-c", str(conf)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._WIREPROXY_ENV,
                creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
                if self._is_windows() else 0,
                start_new_session=not self._is_windows(),
            )
            job.ensure_kill_job()
            if not job.assign(inst.process.pid):
                log(f"[warp] WARNING: wireproxy #{inst.index} could not join the kill-job; it will orphan on exit")

        except Exception as exc:  # noqa: BLE001
            log(f"[warp] restart_instance #{inst.index}: start failed: {exc}")
            return False

        # Wait for port to open
        for _ in range(12):  # ~6 seconds
            if _port_is_open("127.0.0.1", inst.port, timeout=0.5):
                log(f"[warp] restart_instance #{inst.index} ok on port {inst.port}")
                return True
            time.sleep(0.5)

        log(f"[warp] restart_instance #{inst.index}: port did not open within 6s")
        return False

    def regenerate_instance(self, inst: WarpInstance, log=print) -> bool:
        """Fully regenerate a WARP identity: wipe, re-register, restart wireproxy.

        Destroys the existing identity directory and config files,
        registers a fresh WARP account via wgcf, generates new configs,
        and starts wireproxy. Returns True on success.
        """
        log(f"[warp] regenerate_instance #{inst.index} ...")

        # Stop existing process
        if inst.process is not None:
            try:
                if self._is_windows():
                    inst.process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    inst.process.terminate()
                inst.process.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    inst.process.kill()
                except Exception:  # noqa: BLE001
                    pass
            inst.process = None

        # Kill any orphan on our port
        if _port_is_open("127.0.0.1", inst.port):
            pid = _pid_on_port(inst.port)
            if pid:
                _kill_pid(pid, grace_s=2)

        # Wipe identity directory
        if inst.identity_dir.exists():
            shutil.rmtree(inst.identity_dir)
        inst.identity_dir.mkdir(parents=True, exist_ok=True)
        inst.private_key = ""
        inst.address_v4 = ""
        inst.address_v6 = ""

        # Find a free port, avoiding every sibling's assigned port. A sibling
        # that happens to be down still owns its port; taking it would give two
        # identities the same BindAddress.
        try:
            new_port = _find_free_port(
                BASE_PORT + inst.index - 1,
                reserved={i.port for i in self.instances if i is not inst},
            )
            inst.port = new_port
            inst.proxy_url = f"socks5://127.0.0.1:{new_port}"
        except Exception as exc:  # noqa: BLE001
            log(f"[warp] regenerate_instance #{inst.index}: port find failed: {exc}")
            return False

        # Register via wgcf
        try:
            self._register_one(inst, log=log)
        except Exception as exc:  # noqa: BLE001
            log(f"[warp] regenerate_instance #{inst.index}: registration failed: {exc}")
            return False

        # Start wireproxy
        try:
            return self.restart_instance(inst, log=log)
        except Exception as exc:  # noqa: BLE001
            log(f"[warp] regenerate_instance #{inst.index}: restart after regen failed: {exc}")
            return False

    # -- introspection -----------------------------------------------------
    def status(self) -> Dict[str, Any]:
        with self._lock:
            running = sum(1 for i in self.instances if i._is_running())
            ready = sum(1 for i in self.instances if i.private_key)
            return {
                "count": self.count,
                "tools_ready": self.tools_ready(),
                "identities_registered": ready,
                "proxies_running": running,
                "base_port": BASE_PORT,
                "proxy_urls": [i.proxy_url for i in self.instances if i.private_key],
                "instances": [i.status() for i in self.instances],
            }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _run(cmd: List[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=True,
    )


def _parse_profile(text: str):
    """Extract PrivateKey + v4/v6 Address from a wgcf WireGuard profile."""
    key = v4 = v6 = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("PrivateKey"):
            key = line.split("=", 1)[1].strip()
        elif line.startswith("Address"):
            for a in (x.strip() for x in line.split("=", 1)[1].split(",")):
                if ":" in a and not v6:
                    v6 = a
                elif "." in a and not v4:
                    v4 = a
    return key, v4, v6


def _pid_on_port(port: int) -> Optional[int]:
    """Return the PID listening on 127.0.0.1:port (Windows-only fast path)."""
    if platform.system().lower() != "windows":
        return None
    try:
        output = subprocess.check_output(
            ["netstat", "-ano"], text=True, timeout=5,
        )
        for line in output.splitlines():
            if f"127.0.0.1:{port}" in line:
                parts = line.strip().split()
                if parts:
                    try:
                        return int(parts[-1])
                    except ValueError:
                        pass
    except Exception:  # noqa: BLE001
        pass
    return None


def _kill_pid(pid: int, grace_s: float = 2.0) -> bool:
    """Kill a process by PID. Windows-only for now."""
    if platform.system().lower() != "windows":
        return False
    try:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass
    # Give the process a moment to exit, then force kill if still alive.
    deadline = time.time() + grace_s
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, text=True, timeout=10)
        return True
    except Exception:  # noqa: BLE001
        return False


def _pid_alive(pid: int) -> bool:
    """Return True if a Windows process with this PID is still running."""
    if platform.system().lower() != "windows":
        return False
    try:
        output = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], text=True, timeout=5,
        )
        return str(pid) in output and "No tasks" not in output
    except Exception:  # noqa: BLE001
        return False


def _wireproxy_conf(priv: str, addr_v4: str, addr_v6: str, port: int) -> str:
    addr_lines = f"Address = {addr_v4}\n"
    if addr_v6:
        addr_lines += f"Address = {addr_v6}\n"
    return (
        f"[Interface]\n"
        f"PrivateKey = {priv}\n"
        f"{addr_lines}"
        f"DNS = {WARP_DNS}\n\n"
        f"[Peer]\n"
        f"PublicKey = {WARP_PEER_PUBLIC_KEY}\n"
        f"AllowedIPs = 0.0.0.0/0\n"
        f"Endpoint = {WARP_ENDPOINT}\n\n"
        f"[Socks5]\n"
        f"BindAddress = 127.0.0.1:{port}\n"
    )


def _is_archive(name: str) -> bool:
    return name.endswith((".zip", ".tar.gz", ".tgz", ".tar.bz2", ".gz"))


def _extract_binary(archive: Path, dest: Path, binary_name: str) -> None:
    name = archive.name
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            target = _find_in_zip(z, binary_name)
            with z.open(target) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    elif name.endswith((".tar.gz", ".tgz")):
        import tarfile
        with tarfile.open(archive) as t:
            target = _find_in_tar(t, binary_name)
            with t.extractfile(target) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    else:
        import gzip
        with gzip.open(archive, "rb") as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)


def _find_in_zip(zf: zipfile.ZipFile, binary_name: str) -> str:
    for n in zf.namelist():
        base = os.path.basename(n)
        if base == binary_name or base.startswith(binary_name):
            return n
    raise RuntimeError(f"{binary_name} not found in archive")


def _find_in_tar(tf, binary_name: str) -> str:
    for m in tf.getmembers():
        base = os.path.basename(m.name)
        if m.isfile() and (base == binary_name or base.startswith(binary_name)):
            return m.name
    raise RuntimeError(f"{binary_name} not found in archive")


def _pick_wgcf_asset(name: str) -> bool:
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    n = name.lower()
    if "checksums" in n or ".sig" in n or ".txt" in n:
        return False
    if sysname == "windows":
        return "windows" in n and _match_arch(n, machine)
    if sysname == "darwin":
        return ("darwin" in n or "macos" in n) and _match_arch(n, machine)
    return "linux" in n and _match_arch(n, machine)


def _pick_wireproxy_asset(name: str) -> bool:
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    n = name.lower()
    if "checksums" in n or ".sig" in n or ".txt" in n:
        return False
    if sysname == "windows":
        return "windows" in n and _match_arch(n, machine)
    if sysname == "darwin":
        return ("darwin" in n or "macos" in n) and _match_arch(n, machine)
    return "linux" in n and _match_arch(n, machine)


def _match_arch(n: str, machine: str) -> bool:
    m = machine.lower()
    if m in ("amd64", "x86_64"):
        return "amd64" in n or "x86_64" in n or "x64" in n
    if m in ("arm64", "aarch64"):
        return "arm64" in n or "aarch64" in n
    if m in ("armv7", "arm"):
        return "armv7" in n or "arm" in n
    return True



