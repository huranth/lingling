import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import settings
from .pool import Pool

PREFIX = settings.SERVICE_PREFIX
NO_WINDOW = settings.NO_WINDOW


def _quote(text):
    return str(text).replace("'", "''")


def _wireguard():
    if settings.WIREGUARD_PATH:
        return settings.WIREGUARD_PATH

    for name in settings.WIREGUARD_NAMES:
        found = shutil.which(name)
        if found:
            return found

    standard = settings.PROGRAM_FILES / settings.WIREGUARD_DIR / settings.WIREGUARD_EXE
    if standard.is_file():
        return str(standard)

    return ""


def _wait_wireguard(seconds=settings.WIREGUARD_WAIT):
    deadline = time.time() + seconds

    while time.time() < deadline:
        found = _wireguard()
        if found:
            return found
        time.sleep(settings.TUNNEL_POLL)

    return ""


def _run_elevated(script, folder, result):
    path = Path(folder) / settings.SETUP_SCRIPT
    path.write_text(script, encoding="utf-8")
    result = Path(result)
    try:
        result.unlink()
    except OSError:
        pass

    line = (
        f"$p = Start-Process -FilePath '{_quote(settings.POWERSHELL)}' "
        f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',"
        f"'{_quote(path)}' -Verb RunAs -Wait -PassThru; exit $p.ExitCode"
    )
    proc = subprocess.run(
        [settings.POWERSHELL, "-NoProfile", "-Command", line],
        capture_output=True,
        text=True,
        timeout=settings.ELEVATE_TIMEOUT,
        creationflags=NO_WINDOW if os.name == "nt" else 0,
    )
    text = (proc.stdout + "\n" + proc.stderr).strip()

    if "cancel" in text.lower():
        raise RuntimeError("admin approval was canceled")

    try:
        answer = result.read_text(encoding="utf-8").strip()
    except OSError:
        answer = ""

    finally:
        for item in (path, result):
            try:
                item.unlink()
            except OSError:
                pass

    if answer != settings.OK_TEXT:
        raise RuntimeError(answer or text or "WireGuard setup failed")


def ensure_wireguard(folder):
    found = _wireguard()
    if found:
        return found

    if os.name != "nt":
        raise RuntimeError("WireGuard setup is Windows-only")

    print("WireGuard is not installed. Installing it now...")
    winget = settings.WINGET

    if winget:
        try:
            subprocess.run(
                [
                    winget,
                    "install",
                    "--id",
                    settings.WIREGUARD_ID,
                    "--exact",
                    "--silent",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                    "--disable-interactivity",
                ],
                capture_output=True,
                text=True,
                timeout=settings.WINGET_TIMEOUT,
                creationflags=NO_WINDOW,
            )
        except Exception:
            pass

        found = _wait_wireguard(settings.WINGET_WAIT)
        if found:
            return found

    installer = Path(folder) / settings.WIREGUARD_INSTALLER
    try:
        urllib.request.urlretrieve(settings.WIREGUARD_URL, installer)
    except Exception as exc:
        raise RuntimeError(
            "WireGuard is missing and could not be downloaded. "
            "Install it from " + settings.WIREGUARD_DOCS
            + " then run " + settings.APP + " again."
        ) from exc

    result = Path(folder) / settings.INSTALL_RESULT
    script = f"""
$ErrorActionPreference = 'Stop'
try {{
    Start-Process -FilePath '{_quote(installer)}' -ArgumentList '/S' -Wait
    '{_quote(settings.OK_TEXT)}' | Set-Content -LiteralPath '{_quote(result)}' -Encoding ASCII
}} catch {{
    $_.Exception.Message | Set-Content -LiteralPath '{_quote(result)}' -Encoding ASCII
    exit 1
}}
"""
    try:
        _run_elevated(script, folder, result)
    finally:
        try:
            installer.unlink()
        except OSError:
            pass

    found = _wait_wireguard(settings.WIREGUARD_WAIT)
    if found:
        return found

    raise RuntimeError(
        "WireGuard was installed but " + settings.WIREGUARD_EXE + " was not found. "
        "Open WireGuard once, then run " + settings.APP + " again."
    )


def install(folder, config):
    if os.name != "nt":
        raise RuntimeError("WireGuard setup is Windows-only")

    folder = Path(folder)
    wg = ensure_wireguard(folder)
    name = settings.SERVICE_NAME
    service = PREFIX + name
    result = folder / settings.WIREGUARD_RESULT
    log = folder / settings.INSTALL_LOG
    script = f"""
$ErrorActionPreference = 'Continue'
$log = '{_quote(log)}'
$result = '{_quote(result)}'
$svc = $null
try {{
    $sc = '{_quote(settings.SC)}'
    "install {_quote(service)}" | Set-Content -LiteralPath $log -Encoding ASCII
    $old = & '{_quote(wg)}' /uninstalltunnelservice '{_quote(name)}' 2>&1 | Out-String
    $oldCode = $LASTEXITCODE
    "old exit $oldCode`n$old" | Add-Content -LiteralPath $log
    $new = & '{_quote(wg)}' /installtunnelservice '{_quote(config)}' 2>&1 | Out-String
    $code = $LASTEXITCODE
    "install exit $code`n$new" | Add-Content -LiteralPath $log

    for ($i = 0; $i -lt {settings.SERVICE_LOOKUP}; $i++) {{
        $svc = Get-Service -Name '{_quote(service)}' -ErrorAction SilentlyContinue
        if ($svc) {{ break }}
        Start-Sleep -Milliseconds {settings.SERVICE_LOOKUP_MS}
    }}

    if (-not $svc) {{
        Get-Service -Name '{_quote(PREFIX)}*' -ErrorAction SilentlyContinue |
            ForEach-Object {{ $_.Name + ' ' + $_.Status }} |
            Add-Content -LiteralPath $log
        "WireGuard did not create the tunnel service. install exit $code`n$new" |
            Set-Content -LiteralPath $result -Encoding ASCII
        exit 1
    }}

    & $sc config '{_quote(service)}' start= demand | Out-Null

    if ($svc.Status -ne 'Running') {{
        Start-Service -Name '{_quote(service)}' -ErrorAction Stop
    }}

    'ok' | Set-Content -LiteralPath $result -Encoding ASCII
}} catch {{
    $_.Exception.Message | Set-Content -LiteralPath $result -Encoding ASCII
    exit 1
}} finally {{
    if ($svc) {{
        & $sc stop '{_quote(service)}' | Out-Null
    }}
}}
"""
    _run_elevated(script, folder, result)
    return service


def uninstall(service, folder):
    if os.name != "nt":
        return

    wg = _wireguard()
    if not wg:
        return

    name = service[len(PREFIX):] if service.startswith(PREFIX) else service
    result = Path(folder) / settings.WIREGUARD_RESULT
    script = f"""
$ErrorActionPreference = 'Continue'
$result = '{_quote(result)}'
try {{
    $out = & '{_quote(wg)}' /uninstalltunnelservice '{_quote(name)}' 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0 -and $out -notmatch 'does not exist') {{
        $out | Set-Content -LiteralPath $result -Encoding ASCII
        exit 1
    }}
    'ok' | Set-Content -LiteralPath $result -Encoding ASCII
}} catch {{
    $_.Exception.Message | Set-Content -LiteralPath $result -Encoding ASCII
    exit 1
}}
"""
    try:
        _run_elevated(script, folder, result)
    except Exception:
        pass


def read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

    return data if isinstance(data, dict) else {}


def write_status(path, state, note=""):
    item = {"state": state, "note": note, "at": time.time()}
    path = Path(path)
    tmp = path.with_suffix(settings.TMP_SUFFIX)
    tmp.write_text(json.dumps(item), encoding="utf-8")
    os.replace(tmp, path)


def log_line(path, text):
    try:
        with open(path, "a", encoding="utf-8") as file:
            file.write(str(text) + "\n")
    except OSError:
        pass


class Tunnel:
    def __init__(self, folder, service, prefix):
        self.folder = Path(folder)
        self.service = service
        self.prefix = prefix
        self.status = str(self.folder / settings.TUNNEL_STATUS)
        self.stop_file = str(self.folder / settings.TUNNEL_STOP)
        self.log = str(self.folder / settings.TUNNEL_LOG)

    def start(self):
        data = read_json(self.status)

        if data.get("state") == "ready":
            self.stop()

        self._clear()
        self._spawn()
        deadline = time.time() + settings.TUNNEL_READY

        while time.time() < deadline:
            data = read_json(self.status)

            if data.get("state") == "ready":
                return
            if data.get("state") == "error":
                raise RuntimeError(data.get("note") or "tunnel failed")

            time.sleep(settings.TUNNEL_POLL)

        raise RuntimeError("tunnel start timed out")

    def stop(self):
        data = read_json(self.status)

        if data.get("state") in ("stopped", "error"):
            return

        try:
            Path(self.stop_file).write_text(settings.STOP_TEXT, encoding="utf-8")
        except OSError:
            return

        deadline = time.time() + settings.TUNNEL_STOP_WAIT

        while time.time() < deadline:
            data = read_json(self.status)

            if data.get("state") in ("stopped", "error"):
                return

            time.sleep(settings.TUNNEL_POLL)

    def _clear(self):
        for path in (self.status, self.stop_file):
            try:
                Path(path).unlink()
            except OSError:
                pass

    def _spawn(self):
        args = [
            "-B",
            "-m",
            __package__ + ".tunnel",
            "--guard",
            str(os.getpid()),
            self.service,
            self.status,
            self.stop_file,
            self.log,
            self.prefix,
        ]
        line = " ".join(f'"{arg}"' if " " in arg else arg for arg in args)
        root = Path(__file__).resolve().parents[1]
        command = (
            f"Start-Process -FilePath '{_quote(sys.executable)}' "
            f"-ArgumentList '{_quote(line)}' "
            f"-WorkingDirectory '{_quote(root)}' "
            "-Verb RunAs -WindowStyle Hidden"
        )
        proc = subprocess.run(
            [settings.POWERSHELL, "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=settings.SPAWN_TIMEOUT,
        )
        text = (proc.stderr + proc.stdout).strip()

        if "cancel" in text.lower():
            raise RuntimeError("admin approval was canceled")
        if proc.returncode != 0:
            raise RuntimeError(text or "tunnel needs admin")


def service_state(service):
    result = subprocess.run(
        [settings.SC, "query", service],
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW if os.name == "nt" else 0,
    )
    text = result.stdout

    if "RUNNING" in text:
        return "RUNNING"
    if "STOPPED" in text:
        return "STOPPED"

    return ""


def wait_state(service, wanted, seconds):
    deadline = time.time() + seconds

    while time.time() < deadline:
        if service_state(service) == wanted:
            return

        time.sleep(settings.TUNNEL_POLL)

    raise RuntimeError("tunnel not " + wanted.lower())


def set_demand(service):
    result = subprocess.run(
        [settings.SC, "config", service, "start=", "demand"],
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW,
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "service config failed")


def start_service(service):
    subprocess.run(
        [settings.SC, "start", service],
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW,
    )
    wait_state(service, "RUNNING", settings.SERVICE_START)
    time.sleep(settings.SERVICE_SETTLE)


def stop_service(service):
    subprocess.run(
        [settings.SC, "stop", service],
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW,
    )
    wait_state(service, "STOPPED", settings.SERVICE_STOP)


def add_lanes(prefix, service, log):
    name = service[len(PREFIX):] if service.startswith(PREFIX) else service
    lanes = Pool(prefix).lanes
    script = Path(log).with_name(settings.LANES_SCRIPT)
    rows = []

    for lane in lanes:
        rows.append(
            'interface ipv6 add address "' + name + '" '
            + lane + "/" + str(settings.LANE_BITS)
        )

    script.write_text("\n".join(rows) + "\n", encoding="ascii")

    try:
        result = subprocess.run(
            [settings.NETSH, "-f", str(script)],
            capture_output=True,
            text=True,
            creationflags=NO_WINDOW,
        )
    finally:
        try:
            script.unlink()
        except OSError:
            pass

    log_line(log, f"lanes {len(lanes)}; exit {result.returncode}")

    if result.returncode != 0:
        text = (result.stdout + result.stderr).strip()
        raise RuntimeError(text or "could not assign lane addresses")


def wait_parent(pid, stop_file):
    handle = ctypes.windll.kernel32.OpenProcess(settings.PROCESS_QUERY, False, pid)

    if not handle:
        return

    try:
        while True:
            if Path(stop_file).exists():
                return
            wait_ms = int(settings.TUNNEL_POLL * settings.MS_SECOND)
            if ctypes.windll.kernel32.WaitForSingleObject(handle, wait_ms) == 0:
                return
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def guard():
    parent = int(sys.argv[2])
    service = sys.argv[3]
    status = sys.argv[4]
    stop_file = sys.argv[5]
    log = sys.argv[6]
    prefix = sys.argv[7]
    ready = False

    try:
        set_demand(service)
        start_service(service)
        add_lanes(prefix, service, log)
        write_status(status, "ready")
        ready = True
        wait_parent(parent, stop_file)
    except Exception as exc:
        write_status(status, "error", str(exc))
        log_line(log, str(exc))
    finally:
        try:
            stop_service(service)
        except Exception as exc:
            log_line(log, str(exc))
            if ready:
                write_status(status, "error", str(exc))
                ready = False

        if ready:
            write_status(status, "stopped")


def main():
    if "--guard" in sys.argv:
        return guard()

    print("usage: " + settings.APP + ".tunnel --guard ...")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
