"""Update check."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional, Sequence

from . import settings

MANIFEST_URL = settings.MANIFEST_URL
PYPI_URL = settings.PYPI_URL
SCHEMA = settings.UPDATE_SCHEMA
CACHE_TTL = settings.UPDATE_CACHE_TTL
# short cache
UP_TTL = settings.UPDATE_UP_TTL
FAIL_TTL = settings.UPDATE_FAIL_TTL
TIMEOUT = settings.UPDATE_TIMEOUT
BUDGET = settings.UPDATE_BUDGET
MIN_TIMEOUT = settings.UPDATE_MIN_TIMEOUT
HELPER_WAIT = settings.HELPER_WAIT
CACHE_NAME = settings.CACHE_NAME
LOG_NAME = settings.UPDATE_LOG

UPDATE_HELP = f"""
{settings.APP} update -- move to the newest {settings.APP}

    {settings.APP} update            check, then install after this process ends
    {settings.APP} update --check    say what the newest version is, change nothing
    {settings.APP} update --now      install in this process

The default hands the install to a small helper. It waits for this
process to end, then runs pip, so the upgrade lands between two launches.
That is the only safe moment on Windows, where the running console
launcher cannot be replaced.

The launch check is quiet: a background thread, a {settings.UPDATE_TIMEOUT:g} second
timeout, and a {settings.UPDATE_CACHE_TTL / settings.SECONDS_HOUR:g} hour cache. Offline stays silent.
It only prints when there is something to say.

    LINGLING_UPDATE=auto     check, notice, install after the session (default)
    LINGLING_UPDATE=notify   check and notice only
    LINGLING_UPDATE=off      do not check
    LINGLING_UPDATE_URL=...  another manifest endpoint
    LINGLING_UPDATE_CMD=...  your own install command
"""

_HELPER = r'''
import os, subprocess, sys, time

pid = int(sys.argv[1])
cmd = sys.argv[2:]


def wait_seconds(raw):
    """Time cap."""
    default = float(os.environ["LINGLING_HELPER_WAIT"])
    limit = float(os.environ["LINGLING_HELPER_MAX"])
    try:
        value = float(raw or default)
    except ValueError:
        return default
    if value != value or value < 0:
        return default
    return min(value, limit)


deadline = time.time() + wait_seconds(os.environ.get("LINGLING_HELPER_WAIT"))

# no window
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# then poll
POLL_S = float(os.environ["LINGLING_HELPER_POLL"])
TASKLIST = os.environ["LINGLING_TASKLIST"]


def alive(p):
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                [TASKLIST, "/FI", "PID eq %d" % p, "/NH"],
                text=True,
                timeout=float(os.environ["LINGLING_LOCK_TIMEOUT"]),
                creationflags=NO_WINDOW)
            return str(p) in out and "No tasks" not in out
        except Exception:
            return True
    try:
        os.kill(p, 0)
        return True
    except OSError:
        return False


while time.time() < deadline:
    if not alive(pid):
        break
    time.sleep(POLL_S)
else:
    print("launcher still alive after the wait budget -- not installing")
    sys.exit(0)

print("running: %s" % " ".join(cmd), flush=True)
# then install
try:
    rc = subprocess.call(
        cmd, stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr,
        creationflags=(NO_WINDOW if os.name == "nt" else 0))
except OSError as exc:
    print("installer did not start: %s" % exc, flush=True)
    sys.exit(1)
print("install exit: %d" % rc, flush=True)
sys.exit(rc)
'''


def _version_text(value) -> Optional[str]:
    """Clean version."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if re.fullmatch(
        r"[vV]?\d+\.\d+\.\d+(?:\.post\d+)?(?:\+[\w.-]+)?", value) else None


def _safe_info(info: dict) -> dict:
    """Clean fields."""
    result = dict(info)
    for name in ("latest", "min_supported"):
        result[name] = _version_text(info.get(name))
    for name in ("notice", "url", "source"):
        value = info.get(name)
        result[name] = value.strip() if isinstance(value, str) else None
    return result


def key(version: str) -> tuple:
    """Sort key."""
    text = _version_text(version)
    if text is None:
        return (0, 0, 0, 0, 0)
    text = text.lstrip("vV").split("+", 1)[0]
    post = 0
    if ".post" in text:
        text, _, tail = text.partition(".post")
        digits = re.sub(r"\D", "", tail)
        post = int(digits) if digits else 1
    parts = []
    for chunk in text.split("."):
        digits = re.sub(r"\D", "", chunk)
        parts.append(int(digits) if digits else 0)
    parts += [0] * (3 - len(parts))
    return (parts[0], parts[1], parts[2], 1 if post else 0, post)


def is_newer(candidate: Optional[str], than: str) -> bool:
    """Newer?"""
    if _version_text(candidate) is None or _version_text(than) is None:
        return False
    return key(candidate) > key(than)


def current_version() -> str:
    """Right now."""
    from . import __version__
    return __version__


def mode() -> str:
    """Update way."""
    raw = (os.environ.get("LINGLING_UPDATE") or "").strip().lower()
    if raw in ("off", "0", "no", "false", "none", "disable", "disabled"):
        return "off"
    if raw in ("notify", "check", "notice", "warn", "only"):
        return "notify"
    return "auto"


def manifest_url() -> str:
    return (os.environ.get("LINGLING_UPDATE_URL") or "").strip() or MANIFEST_URL


def _override_cmd() -> list:
    """Own command."""
    raw = (os.environ.get("LINGLING_UPDATE_CMD") or "").strip()
    if not raw:
        return []
    parts = shlex.split(raw, posix=(os.name != "nt"))
    return [p.strip('"') for p in parts if p.strip('"')]


def _get_json(url: str, timeout: float):
    """Fetch json."""
    request = urllib.request.Request(
        url, headers={"User-Agent": f"{settings.PACKAGE}/{current_version()}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except Exception:
        return None


def _lookup(timeout: float) -> dict:
    """Fetch info."""
    info = {"latest": None, "min_supported": None, "notice": None,
            "url": None, "source": None}

    doc = _get_json(manifest_url(), timeout)
    if isinstance(doc, dict) and doc.get("schema") == SCHEMA:
        info["latest"] = doc.get("latest") or None
        info["min_supported"] = doc.get("min_supported") or None
        info["notice"] = doc.get("notice") or None
        info["url"] = doc.get("url") or None
        info = _safe_info(info)
        if info["latest"] or info["notice"]:
            info["source"] = "manifest"
            if info["latest"]:
                return info
    doc = _get_json(PYPI_URL, timeout)
    if isinstance(doc, dict):
        registry = doc.get("info")
        latest = None
        if isinstance(registry, dict):
            latest = _version_text(registry.get("version"))
        if latest:
            info["latest"] = latest
            info["source"] = settings.REGISTRY_SOURCE
    return info


def _cache_path(data_dir) -> Path:
    return Path(data_dir) / CACHE_NAME


def read_cache(data_dir) -> Optional[dict]:
    """Load cache."""
    try:
        doc = json.loads(_cache_path(data_dir).read_text(encoding="utf-8"))
    except Exception:
        return None
    return _safe_info(doc) if isinstance(doc, dict) else None


def _write_cache(data_dir, info: dict) -> None:
    """Save cache."""
    try:
        path = _cache_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(info, checked_at=int(time.time()))
        tmp = path.with_suffix(settings.TMP_SUFFIX)
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def fresh(info: Optional[dict], now: Optional[float] = None) -> bool:
    """Still fresh."""
    if not isinstance(info, dict):
        return False
    try:
        age = (now if now is not None else time.time()) - float(
            info.get("checked_at") or 0)
    except (TypeError, ValueError):
        return False
    if behind(info):
        ttl = CACHE_TTL
    elif info.get("latest"):
        ttl = UP_TTL
    else:
        ttl = FAIL_TTL
    return 0 <= age < ttl


def check(data_dir, force: bool = False, timeout: float = TIMEOUT) -> dict:
    """Check once."""
    if not force:
        cached = read_cache(data_dir)
        if fresh(cached):
            return dict(cached or {})
    info = _lookup(timeout)
    _write_cache(data_dir, info)
    return dict(info, checked_at=int(time.time()))


def behind(info: Optional[dict], current: Optional[str] = None) -> bool:
    """Behind check."""
    if not isinstance(info, dict):
        return False
    info = _safe_info(info)
    current = current or current_version()
    if is_newer(info.get("latest"), current):
        return True
    floor = info.get("min_supported")
    if not floor or str(floor) == current:
        return False
    return not is_newer(current, str(floor))


def notice_lines(info: Optional[dict], current: Optional[str] = None,
                 mode_name: Optional[str] = None) -> list:
    """Notice text."""
    if mode_name == "off" or not behind(info, current):
        return []
    info = _safe_info(info)
    current = current or current_version()
    latest = info.get("latest")
    lines = []
    notice = (info.get("notice") or "").strip()
    if notice:
        lines.append(f"  {notice}")
    floor = info.get("min_supported")
    if floor and not is_newer(current, str(floor)) and str(floor) != current:
        lines.append(
            f"  you are on {current}, and this opencode needs {floor} or "
            f"newer -- update before the next session")
    elif is_newer(latest, current):
        if mode_name == "auto":
            lines.append(
                f"  {settings.APP} {latest} is out (you are on {current}) -- it "
                f"installs when this session ends")
        else:
            lines.append(
                f"  {settings.APP} {latest} is out (you are on {current}) -- run: "
                f"{settings.APP} update")
    return lines


class Check:
    """Quiet check."""

    def __init__(self) -> None:
        self.info: Optional[dict] = None

    def start(self, data_dir) -> "Check":
        def run() -> None:
            deadline = time.time() + BUDGET
            try:
                sweep_launcher_leftovers()
                cached = read_cache(data_dir)
                if fresh(cached):
                    self.info = dict(cached or {})
                    return
                remaining = max(MIN_TIMEOUT, min(TIMEOUT, deadline - time.time()))
                self.info = check(data_dir, force=True, timeout=remaining)
            except Exception:
                self.info = None

        thread = threading.Thread(
            target=run,
            name="update-check",
            daemon=True,
        )
        thread.start()
        return self


def start(data_dir) -> Optional[Check]:
    """Start it."""
    if mode() == "off" or os.environ.get("LINGLING_NO_UPDATE_CHECK"):
        return None
    try:
        return Check().start(data_dir)
    except Exception:
        return None


def _say(stream, line: str) -> None:
    """Just write."""
    try:
        stream.write(line + "\n")
        stream.flush()
    except Exception:
        pass


def emit(check_obj: Optional[Check], stream=None) -> None:
    """Say it."""
    if check_obj is None:
        return
    stream = stream or sys.stderr
    for line in notice_lines(check_obj.info, mode_name=mode()):
        _say(stream, line)


def cached_hint(data_dir, stream=None) -> None:
    """Quick hint."""
    stream = stream or sys.stderr
    mode_name = "off" if mode() == "off" else "notify"
    for line in notice_lines(read_cache(data_dir), mode_name=mode_name):
        _say(stream, line)


def install_method() -> str:
    """Install way."""
    seen = []
    for attr in ("prefix", "base_prefix"):
        seen.append(str(getattr(sys, attr, "") or ""))
    seen.append(str(Path(sys.executable or "").parent))
    text = " ".join(seen).replace("\\", "/").lower()
    if "/" + settings.PIPX_LABEL + "/" in text:
        return settings.PIPX_LABEL
    if (
        "/" + settings.UV_LABEL + "/tools/" in text
        or "/" + settings.UV_LABEL + "/python/" in text
    ):
        return settings.UV_LABEL
    return settings.PIP


def upgrade_command() -> list:
    """Upgrade command."""
    override = _override_cmd()
    if override:
        return override
    method = install_method()
    if method == settings.PIPX_LABEL:
        exe = shutil.which(settings.PIPX_LABEL)
        if exe:
            return [exe, "upgrade", settings.PACKAGE]
    if method == settings.UV_LABEL:
        exe = shutil.which(settings.UV_LABEL)
        if exe:
            return [exe, "tool", "upgrade", settings.PACKAGE]
    return [sys.executable or "python", "-m", settings.PIP, "install",
            "--upgrade", settings.PACKAGE]


def scripts_dir() -> Path:
    """Script folder."""
    try:
        import sysconfig
        path = sysconfig.get_path("scripts")
        if path:
            return Path(path)
    except Exception:
        pass
    return Path(sys.executable or "").parent


def _running_launcher() -> Optional[Path]:
    """Open launcher."""
    if os.name != "nt":
        return None
    exe = scripts_dir() / (settings.APP + ".exe")
    if not exe.is_file():
        return None
    name = Path(sys.argv[0] or "").name.lower()
    names = (settings.APP, settings.APP + ".exe")
    return exe if name in names else None


def sweep_launcher_leftovers() -> None:
    """Old files."""
    if os.name != "nt":
        return
    try:
        for stale in scripts_dir().glob(settings.APP + ".exe" + settings.OLD_SUFFIX + "*"):
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError:
        pass


def run_install(timeout=settings.INSTALL_TIMEOUT):
    """Run it."""
    argv = upgrade_command()
    parked = None
    launcher = _running_launcher()

    if launcher is not None:
        target = launcher.with_name(
            f"{launcher.name}{settings.OLD_SUFFIX}{os.getpid()}"
        )

        try:
            os.replace(launcher, target)
            parked = target
        except OSError:
            print(
                "could not park the running launcher -- close "
                + settings.APP + " and use: " + settings.APP + " update"
            )
            return

    try:
        subprocess.run(
            argv,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except Exception:
        pass

    if parked is None:
        return

    if launcher.exists():
        try:
            parked.unlink()
        except OSError:
            pass
        return

    try:
        os.replace(parked, launcher)
    except OSError:
        print("could not restore the launcher: " + str(parked))


def installed_version() -> Optional[str]:
    """On disk."""
    try:
        from importlib.metadata import version as _v
        return _v(settings.PACKAGE)
    except Exception:
        return None


def helper_python() -> str:
    """Helper path."""
    base = getattr(sys, "_base_executable", "") or ""
    if base and base != sys.executable and os.path.isfile(base):
        return base
    return sys.executable or "python"


def _detach_kwargs(breakaway: bool = True) -> dict:
    if os.name == "nt":
        flags = settings.DETACHED | settings.NEW_GROUP
        if breakaway:
            flags |= settings.BREAKAWAY
        return {"creationflags": flags}
    return {"start_new_session": True}


def spawn_deferred(data_dir, delay: Optional[float] = None):
    """After exit."""
    argv = upgrade_command()
    if not argv:
        return None
    log_path = Path(data_dir) / LOG_NAME
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(log_path, "a", encoding="utf-8")
    except OSError:
        return None
    env = dict(os.environ)
    env["LINGLING_HELPER_WAIT"] = str(delay or HELPER_WAIT)
    env["LINGLING_HELPER_POLL"] = str(settings.HELPER_POLL)
    env["LINGLING_HELPER_MAX"] = str(settings.HELPER_MAX)
    env["LINGLING_LOCK_TIMEOUT"] = str(settings.LOCK_TIMEOUT)
    env["LINGLING_TASKLIST"] = settings.TASKLIST
    cmd = [helper_python(), "-c", _HELPER, str(os.getpid()), *argv]
    proc = None
    for breakaway in (True, False):
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, close_fds=True, env=env,
                **_detach_kwargs(breakaway))
            break
        except Exception:
            proc = None
    if proc is None:
        try:
            log.close()
        except Exception:
            pass
        return None
    try:
        log.write(f"\n--- scheduled by {settings.APP} {current_version()} at "
                  f"{time.strftime(settings.LOG_FORMAT)} ---\n")
        log.flush()
        log.close()
    except Exception:
        pass
    return proc.pid


def auto_install(data_dir, info: Optional[dict]) -> Optional[int]:
    """Then install."""
    if mode() != "auto" or not behind(info):
        return None
    return spawn_deferred(data_dir)


def _report(current: str, info: dict) -> None:
    latest = info.get("latest")
    source = info.get("source") or "unknown"

    if latest:
        print(f"  installed  {current}")
        print(f"  available  {latest}  ({source})")
        return

    print(f"  installed  {current}")
    print(
        "  available  unknown -- "
        + manifest_url() + " and " + settings.REGISTRY_NAME
        + " both did not answer"
    )


def update_command(rest: Sequence[str]) -> int:
    """Update flow."""
    from . import data_dir as _data_dir

    args = list(rest)
    if "--help" in args or "-h" in args:
        print(UPDATE_HELP)
        return 0
    sweep_launcher_leftovers()
    check_only = "--check" in args
    now = "--now" in args
    quiet = "-q" in args or "--quiet" in args

    data = _data_dir()
    current = current_version()
    info = check(data, force=True)
    _report(current, info)

    if not info.get("latest"):
        print("  could not reach the update servers -- nothing was changed.")
        print("  install by hand:  " + " ".join(upgrade_command()))
        return 1

    if not behind(info, current):
        print("  you are on the newest " + settings.APP + " -- nothing to do.")
        return 0
    if check_only:
        print("  run `" + settings.APP + " update` to install it.")
        return 0

    latest = info.get("latest") or "the newest release"

    if now:
        print(f"  installing {latest} in this process ...")
        run_install()
        after = installed_version()
        if after and key(after) > key(current):
            print(f"  done -- {after} is installed. Relaunch to ride it.")
            return 0
        print(f"  the install did not take (still {after or current}).")
        print("  Run it from a fresh shell:\n    " + settings.APP + " update --now")
        return 1

    pid = spawn_deferred(data)
    if pid is None:
        print("  could not schedule the install.")
        print("  Run it yourself:\n    " + " ".join(upgrade_command()))
        return 1
    print(f"  scheduled -- {latest} installs when this {settings.APP} exits "
          f"(helper pid {pid}).")
    if not quiet:
        print(f"  progress: {Path(data) / LOG_NAME}")
    return 0
