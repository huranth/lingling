import os
import subprocess
from pathlib import Path

from . import settings


def _alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    try:
        out = subprocess.check_output(
            [settings.TASKLIST, "/FI", f"PID eq {pid}", "/NH"],
            text=True,
            timeout=settings.LOCK_TIMEOUT,
            creationflags=settings.NO_WINDOW,
        )
        return str(pid) in out and "No tasks" not in out
    except Exception:
        return True


def claim(folder):
    path = Path(folder) / settings.SESSION_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = 0

    if pid and _alive(pid):
        return pid

    try:
        path.unlink()
    except OSError:
        pass

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, settings.PRIVATE_MODE)
    except FileExistsError:
        return -1

    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(fd)

    return 0


def release(folder):
    path = Path(folder) / settings.SESSION_LOCK

    try:
        if path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass
