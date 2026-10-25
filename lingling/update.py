"""The update check against the published manifest, with no new dependency."""

from __future__ import annotations

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

MANIFEST_URL = "https://lingling-update.vercel.app/api/latest"

PYPI_URL = "https://pypi.org/pypi/lingling/json"

SCHEMA = 1

CACHE_TTL = 6 * 3600

#: quarter hour
UP_TTL = 15 * 60

FAIL_TTL = 3600

TIMEOUT = 3.0

BUDGET = 8.0

HELPER_WAIT = 24 * 3600

CACHE_NAME = "update-check.json"
LOG_NAME = "update.log"

UPDATE_HELP = """
lingling update -- move to the newest lingling

The copy on your disk has the code it was installed with, and nothing
can change that from the outside: a build that cannot check can never
learn it is stale. So this is the one command worth knowing by name.

    lingling update            check, then install it safely
    lingling update --check    say what the newest version is, change nothing
    lingling update --now      install in this process, not after it

The default does not install while lingling is running. On Windows the
console launcher IS the running file, and `pip install --upgrade` wants
to replace it -- so the install is handed to a small helper that waits
for this process to end and only then runs pip. The upgrade lands
between two launches, which is the only moment it is safe.

When an update is waiting and you are at a terminal, the launch asks
before scheduling it: Enter or `y` installs it once this session ends,
`n` leaves it for `lingling update`. Without a terminal -- a shortcut,
a service, a redirected input -- nothing is asked and the install is
still scheduled, because a question nobody can answer is not a
question.

The check on launch is unobtrusive: a background thread, a three second
timeout, and a six hour cache, so an offline machine is silent rather
than slow. It only ever prints when there is something to say.

    LINGLING_UPDATE=auto     check, notice, ask, install after the session (default)
    LINGLING_UPDATE=notify   check and notice only -- never install for me
    LINGLING_UPDATE=off      do not check at all
    LINGLING_UPDATE_URL=...  check a different manifest endpoint
    LINGLING_UPDATE_CMD=...  install with your own command, not pip

Nothing here is required: with the check off, lingling launches exactly
as it did before this command existed.
"""

_HELPER = r'''
import os, subprocess, sys, time

pid = int(sys.argv[1])
cmd = sys.argv[2:]


def wait_seconds(raw):
    """A week at most: a bad value waits the default rather than for ever."""
    try:
        value = float(raw or 86400)
    except ValueError:
        return 86400.0
    if value != value or value < 0:
        return 86400.0
    return min(value, 7 * 86400)


deadline = time.time() + wait_seconds(os.environ.get("LINGLING_HELPER_WAIT"))

#: CREATE_NO_WINDOW, and it is not cosmetic. This helper has no console of
#: its own (it is spawned DETACHED_PROCESS), and Windows hands every
#: console-mode child of a console-less process a BRAND NEW console -- a
#: visible one. Polling tasklist without this flag measured as a console
#: window flashing twice a second for the whole session: the owner's
#: report of "terminals spamming when the timer finished" was this loop,
#: every time. Measured on Windows: 20 unflagged tasklist spawns from a
#: console-less parent peaked at 2 visible console windows, the same 20
#: with this flag stayed at 1.
NO_WINDOW = 0x08000000

#: The poll was twice a second, which is a lot of process churn to notice
#: a process that has been dead for a while -- the install happens minutes
#: after the session, not instantly after it. Two seconds halves the churn
#: and is still immediate by any human measure.
POLL_S = 2.0


def alive(p):
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", "PID eq %d" % p, "/NH"],
                text=True, timeout=10, creationflags=NO_WINDOW)
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
# The installer is a console app too, and this helper is console-less: it
# would otherwise flash a window for the length of the install. The
# explicit handles below are not decoration either: a console child of a
# console-less parent gets a NEW console, not this log, and with a bare
# spawn the installer's own lines and its exit code were both discarded.
# Measured on the shipped helper: update.log showed "running:" and then
# nothing, so a failed install looked exactly like a quiet one. With the
# handles, pip's output and the exit code land in the log.
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
    """Accept only the release scheme the updater can compare safely."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if re.fullmatch(
        r"[vV]?\d+\.\d+\.\d+(?:\.post\d+)?(?:\+[\w.-]+)?", value) else None


def _safe_info(info: dict) -> dict:
    """Normalize manifest and cache fields at the trust boundary."""
    result = dict(info)
    for name in ("latest", "min_supported"):
        result[name] = _version_text(info.get(name))
    for name in ("notice", "url", "source"):
        value = info.get(name)
        result[name] = value.strip() if isinstance(value, str) else None
    return result


def key(version: str) -> tuple:
    """PEP 440 puts a post release after the release it amends."""
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
    """True when ``candidate`` sorts after ``than`` under :func:`key`."""
    if _version_text(candidate) is None or _version_text(than) is None:
        return False
    return key(candidate) > key(than)


def current_version() -> str:
    """The version in this process, read per call so a test can swap it"""
    from . import __version__
    return __version__



def mode() -> str:
    """``auto`` (default), ``notify``, or ``off`` -- read per call"""
    raw = (os.environ.get("LINGLING_UPDATE") or "").strip().lower()
    if raw in ("off", "0", "no", "false", "none", "disable", "disabled"):
        return "off"
    if raw in ("notify", "check", "notice", "warn", "only"):
        return "notify"
    if raw in ("auto", "1", "yes", "true", "on", ""):
        return "auto"
    return "auto"


def manifest_url() -> str:
    return (os.environ.get("LINGLING_UPDATE_URL") or "").strip() or MANIFEST_URL


def _override_cmd() -> list:
    """``LINGLING_UPDATE_CMD``, for installs that are not pip, pipx or uv"""
    raw = (os.environ.get("LINGLING_UPDATE_CMD") or "").strip()
    if not raw:
        return []
    parts = shlex.split(raw, posix=(os.name != "nt"))
    return [p.strip('"') for p in parts if p.strip('"')]



def _get_json(url: str, timeout: float):
    """A JSON GET, or None. Never raises -- a check is not a feature."""
    request = urllib.request.Request(
        url, headers={"User-Agent": f"lingling/{current_version()}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except Exception:  # noqa: BLE001
        return None


def _lookup(timeout: float) -> dict:
    """Ask the manifest, then the registry. Either may fail alone."""
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
        latest = _version_text(registry.get("version")) \
            if isinstance(registry, dict) else None
        if latest:
            info["latest"] = latest
            info["source"] = "pypi"
    return info


def _cache_path(data_dir) -> Path:
    return Path(data_dir) / CACHE_NAME


def read_cache(data_dir) -> Optional[dict]:
    """The last answer, or None. No network, no side effects."""
    try:
        doc = json.loads(_cache_path(data_dir).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return _safe_info(doc) if isinstance(doc, dict) else None


def _write_cache(data_dir, info: dict) -> None:
    """Best effort: an unwritable cache costs a request, not a boot."""
    try:
        path = _cache_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(info, checked_at=int(time.time()))
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def fresh(info: Optional[dict], now: Optional[float] = None) -> bool:
    """Is this cached answer still worth acting on?

    How long an answer keeps depends on what it said.  "Something newer is out"
    is worth repeating for hours -- a launch that has already been told needs no
    second request to be told again.  "You are current" is not: it is the answer
    that a release invalidates the moment it is uploaded, and every launch reads
    it, so a stale one is a release nobody hears about.
    """
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
    """The newest version we know of, cached. Never raises."""
    if not force:
        cached = read_cache(data_dir)
        if fresh(cached):
            return dict(cached or {})
    info = _lookup(timeout)
    _write_cache(data_dir, info)
    return dict(info, checked_at=int(time.time()))


def behind(info: Optional[dict], current: Optional[str] = None) -> bool:
    """Should this build be asking the user to move? """
    if not isinstance(info, dict):
        return False
    info = _safe_info(info)
    current = current or current_version()
    if is_newer(info.get("latest"), current):
        return True
    floor = info.get("min_supported")
    return bool(floor) and not is_newer(current, str(floor)) \
        and str(floor) != current


def notice_lines(info: Optional[dict], current: Optional[str] = None,
                 mode_name: Optional[str] = None, ask: bool = False) -> list:
    """The lines worth printing, or an empty list to stay silent"""
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
        if mode_name == "auto" and ask:
            lines.append(f"  lingling {latest} is out (you are on {current}).")
        elif mode_name == "auto":
            lines.append(
                f"  lingling {latest} is out (you are on {current}) -- it "
                f"installs when this session ends")
        else:
            lines.append(
                f"  lingling {latest} is out (you are on {current}) -- run: "
                f"lingling update")
    if not lines:
        return []
    return lines



class Check:
    """A background check, and the answer when it arrives"""

    def __init__(self) -> None:
        self.info: Optional[dict] = None
        self.done = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, data_dir) -> "Check":
        def run() -> None:
            deadline = time.time() + BUDGET
            try:
                sweep_launcher_leftovers()
                cached = read_cache(data_dir)
                if fresh(cached):
                    self.info = dict(cached or {})
                    return
                remaining = max(0.5, min(TIMEOUT, deadline - time.time()))
                self.info = check(data_dir, force=True, timeout=remaining)
            except Exception:  # noqa: BLE001
                self.info = None
            finally:
                self.done.set()

        self._thread = threading.Thread(target=run, name="update-check",
                                        daemon=True)
        self._thread.start()
        return self

    def wait(self, timeout: Optional[float] = None) -> Optional[dict]:
        self.done.wait(timeout)
        return self.info


def start(data_dir) -> Optional[Check]:
    """Start the launch check, unless it is switched off."""
    if mode() == "off" or os.environ.get("LINGLING_NO_UPDATE_CHECK"):
        return None
    try:
        return Check().start(data_dir)
    except Exception:  # noqa: BLE001
        return None


def _say(stream, line: str) -> None:
    """Best effort: a closed or broken stream must not kill a launch."""
    try:
        stream.write(line + "\n")
        stream.flush()
    except Exception:  # noqa: BLE001
        pass


def emit(check_obj: Optional[Check], stream=None, ask: bool = False) -> None:
    """Say the one thing there is to say, or nothing at all"""
    if check_obj is None:
        return
    stream = stream or sys.stderr
    for line in notice_lines(check_obj.info, mode_name=mode(), ask=ask):
        _say(stream, line)


def cached_hint(data_dir, stream=None) -> None:
    """The ``--version`` line, from cache only. Never touches the net.

    Nothing is scheduled here, so the line names the command rather than saying
    an install is on its way: `--version` is a question, not a launch.
    """
    stream = stream or sys.stderr
    mode_name = "off" if mode() == "off" else "notify"
    for line in notice_lines(read_cache(data_dir), mode_name=mode_name):
        _say(stream, line)



def install_method() -> str:
    """How this copy was installed: ``pipx``, ``uv``, or ``pip``"""
    seen = []
    for attr in ("prefix", "base_prefix"):
        seen.append(str(getattr(sys, attr, "") or ""))
    seen.append(str(Path(sys.executable or "").parent))
    text = " ".join(seen).replace("\\", "/").lower()
    if "/pipx/" in text:
        return "pipx"
    if "/uv/tools/" in text or "/uv/python/" in text:
        return "uv"
    return "pip"


def upgrade_command() -> list:
    """The installer invocation for this copy, as an argv list."""
    override = _override_cmd()
    if override:
        return override
    method = install_method()
    if method == "pipx":
        exe = shutil.which("pipx")
        if exe:
            return [exe, "upgrade", "lingling"]
    if method == "uv":
        exe = shutil.which("uv")
        if exe:
            return [exe, "tool", "upgrade", "lingling"]
    # --user keeps the upgrade in the same place the current install lives
    # (~/.local). Without it pip would drop a second copy into the system
    # tree and leave two diverging installs behind.
    return [sys.executable or "python", "-m", "pip", "install", "--upgrade",
            "--user", "lingling"]


def scripts_dir() -> Path:
    """sysconfig, not the interpreter's path: a global launcher lives in Scripts/."""
    try:
        import sysconfig
        path = sysconfig.get_path("scripts")
        if path:
            return Path(path)
    except Exception:  # noqa: BLE001
        pass
    return Path(sys.executable or "").parent


def _running_launcher() -> Optional[Path]:
    """The test is the *name* of ``argv[0]``, not its path"""
    if os.name != "nt":
        return None
    exe = scripts_dir() / "lingling.exe"
    if not exe.is_file():
        return None
    name = Path(sys.argv[0] or "").name.lower()
    return exe if name in ("lingling", "lingling.exe") else None


def sweep_launcher_leftovers() -> None:
    """Drop launchers parked by an earlier upgrade"""
    if os.name != "nt":
        return
    try:
        for stale in scripts_dir().glob("lingling.exe.old-*"):
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError:
        pass


def run_install(capture: bool = False, timeout: float = 600):
    """Run the installer in this process"""
    argv = upgrade_command()
    parked = None
    launcher = _running_launcher()
    if launcher is not None:
        target = launcher.with_name(f"{launcher.name}.old-{os.getpid()}")
        try:
            os.replace(launcher, target)
            parked = target
        except OSError:
            parked = None
            return False, (
                "could not park the running launcher -- Windows will not "
                "let the install replace it while it runs. Close lingling "
                "and run this from a fresh shell, or use the deferred "
                "install (lingling update).")
    try:
        result = subprocess.run(argv, capture_output=capture, text=True,
                                stdin=subprocess.DEVNULL, timeout=timeout)
        ok = result.returncode == 0
        output = (result.stdout or "") + (result.stderr or "") \
            if capture else ""
    except FileNotFoundError:
        ok, output = False, f"could not run {argv[0]}"
    except subprocess.TimeoutExpired:
        ok, output = False, f"{argv[0]} took longer than {timeout:.0f}s"
    except Exception as exc:  # noqa: BLE001
        ok, output = False, str(exc)

    if parked is not None:
        if launcher.exists():
            try:
                parked.unlink()
            except OSError:
                pass
        else:
            try:
                os.replace(parked, launcher)
            except OSError:
                return False, (output + "\n\nthe installer wrote no launcher "
                               "and the previous one could not be moved "
                               f"back: {parked.name} is still in "
                               f"{parked.parent}")
            if ok:
                ok = False
                output += ("\nthe install reported success but wrote no "
                           "launcher -- the previous one has been restored")
    return ok, output


def installed_version() -> Optional[str]:
    """What the *installed* package says right now, after an install."""
    try:
        from importlib.metadata import version as _v
        return _v("lingling")
    except Exception:  # noqa: BLE001
        return None


def helper_python() -> str:
    """The interpreter the detached helper itself should run"""
    base = getattr(sys, "_base_executable", "") or ""
    if base and base != sys.executable and os.path.isfile(base):
        return base
    return sys.executable or "python"


_DETACHED = 0x00000008
_NEW_GROUP = 0x00000200
_BREAKAWAY = 0x01000000


def _detach_kwargs(breakaway: bool = True) -> dict:
    if os.name == "nt":
        flags = _DETACHED | _NEW_GROUP
        if breakaway:
            flags |= _BREAKAWAY
        return {"creationflags": flags}
    return {"start_new_session": True}


def spawn_deferred(data_dir, delay: Optional[float] = None):
    """Hand the install to a helper that waits for us to exit"""
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
    cmd = [helper_python(), "-c", _HELPER, str(os.getpid()), *argv]
    proc = None
    for breakaway in (True, False):
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, close_fds=True, env=env,
                **_detach_kwargs(breakaway))
            break
        except Exception:  # noqa: BLE001
            proc = None
    if proc is None:
        try:
            log.close()
        except Exception:  # noqa: BLE001
            pass
        return None
    try:
        log.write(f"\n--- scheduled by lingling {current_version()} at "
                  f"{time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        log.flush()
        log.close()
    except Exception:  # noqa: BLE001
        pass
    return proc.pid



PROMPT = "  install it when this session ends? [Y/n] "

SKIP_LINE = ("  ok -- skipped: `lingling update` installs it when you want, "
             "and LINGLING_UPDATE=notify stops the question")


_NO = ("n", "no")


def _console_stdin() -> bool:
    """Will the OS really deliver a keystroke to this process's stdin?"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        return bool(kernel32.GetConsoleMode(
            kernel32.GetStdHandle(-10), ctypes.byref(ctypes.c_uint())))
    except Exception:  # noqa: BLE001
        return False


def _at_terminal(stdout=None) -> bool:
    """Are both ends of the question a terminal, one that can answer?"""
    out = sys.stdout if stdout is None else stdout
    try:
        if not out.isatty():
            return False
        if os.name == "nt":
            return _console_stdin()
        return bool(sys.stdin.isatty())
    except Exception:  # noqa: BLE001
        return False


def _pending_input() -> bool:
    """Is a keystroke already waiting?"""
    try:
        if os.name == "nt":
            import msvcrt
            return bool(msvcrt.kbhit())
        import select
        return bool(select.select([sys.stdin], [], [], 0)[0])
    except Exception:  # noqa: BLE001
        return True


def askable(info: Optional[dict] = None) -> bool:
    """Is there someone at a terminal who can answer, right now?"""
    if mode() != "auto" or not behind(info):
        return False
    return _at_terminal() and not _pending_input()


def _ask(prompt: str, stream) -> str:
    """Write the prompt, then read one line from the terminal"""
    try:
        stream.write(prompt)
        stream.flush()
    except Exception:  # noqa: BLE001
        return "n"
    try:
        return input()
    except (EOFError, KeyboardInterrupt):
        _say(stream, "")
        return "n"


def auto_install(data_dir, info: Optional[dict], ask: bool = False,
                 read=None, stream=None) -> Optional[int]:
    """Schedule the deferred install when the mode says to"""
    if mode() != "auto" or not behind(info):
        return None
    if ask:
        stream = stream or sys.stderr
        answer = (read or _ask)(PROMPT, stream)
        if answer.strip().lower() in _NO:
            _say(stream, SKIP_LINE)
            return None
    return spawn_deferred(data_dir)



def _report(current: str, info: dict, stream=None) -> None:
    stream = stream or sys.stdout
    latest = info.get("latest")
    source = info.get("source") or "unknown"
    if latest:
        stream.write(f"  installed  {current}\n")
        stream.write(f"  available  {latest}  ({source})\n")
    else:
        stream.write(f"  installed  {current}\n")
        stream.write(f"  available  unknown -- {manifest_url()} and PyPI both "
                     f"did not answer\n")
    stream.flush()


def update_command(rest: Sequence[str]) -> int:
    """``lingling update`` -- check, report, and install. Returns a code."""
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
        print("  you are on the newest lingling -- nothing to do.")
        return 0
    if check_only:
        print("  run `lingling update` to install it.")
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
        print("  Run it from a fresh shell:\n    lingling update --now")
        return 1

    pid = spawn_deferred(data)
    if pid is None:
        print("  could not schedule the install.")
        print("  Run it yourself:\n    " + " ".join(upgrade_command()))
        return 1
    print(f"  scheduled -- {latest} installs when this lingling exits "
          f"(helper pid {pid}).")
    if not quiet:
        print(f"  progress: {Path(data) / LOG_NAME}")
    return 0
