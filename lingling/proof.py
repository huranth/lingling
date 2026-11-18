import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import settings

TITLE = settings.PROOF_TITLE


def _c(text, code=""):
    if not code or os.environ.get("NO_COLOR"):
        return str(text)
    return f"\x1b[{code}m{text}\x1b[0m"


def _show(event):
    kind = event.get("kind", "")
    clock = event.get("clock", "")
    lane = event.get("lane", "")
    note = event.get("note", "")
    host = event.get("host", "")
    port = event.get("port", 0)

    if kind == "start":
        print(_c(clock + "  session", settings.PROOF_SESSION_COLOR)
              + "  lane " + str(lane), flush=True)
    elif kind == "switch":
        line = _c(clock + "  switch", settings.PROOF_SWITCH_COLOR)
        print(line + "  " + str(note) + " -> " + str(lane), flush=True)
    elif kind == "limit":
        print(_c(clock + "  limit", settings.PROOF_LIMIT_COLOR)
              + "   lane " + str(lane), flush=True)
    elif kind == "cool":
        print(_c(clock + "  wait", settings.PROOF_WAIT_COLOR)
              + "    " + str(note), flush=True)
    elif kind == "wall":
        print(_c(clock + "  wall", settings.PROOF_LIMIT_COLOR)
              + "    lane " + str(lane)
              + "  " + str(note), flush=True)
    elif kind == "request":
        model = event.get("model") or ""
        provider = event.get("provider") or ""
        name = (provider + "/" + model).strip("/")
        print(clock + "  request  lane " + str(lane) + "  " + name, flush=True)
    elif kind == "done":
        model = str(event.get("model") or "")
        elapsed = str(event.get("ms") or 0)
        print(
            clock + "  done     lane " + str(lane) + "  "
            + model + "  " + elapsed + " ms",
            flush=True,
        )
    elif kind == "open":
        print(
            clock + "  tcp      lane " + str(lane) + "  "
            + host + ":" + str(port) + "  " + str(note),
            flush=True,
        )
    elif kind == "close":
        elapsed = str(event.get("ms") or 0)
        print(
            clock + "  tcp      lane " + str(lane) + "  "
            + host + ":" + str(port) + "  " + elapsed + " ms",
            flush=True,
        )
    elif kind == "fail":
        line = _c(clock + "  tcp fail", settings.PROOF_FAIL_COLOR)
        print(line + "  lane " + str(lane) + "  " + str(note), flush=True)


def tail(path, offset=0):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    position = int(offset or 0)

    try:
        while True:
            size = path.stat().st_size

            if size < position:
                position = 0

            if size > position:
                with path.open("rb") as file:
                    file.seek(position)
                    while True:
                        line = file.readline()
                        if not line.endswith(b"\n"):
                            break
                        position = file.tell()
                        try:
                            event = json.loads(line.decode("utf-8"))
                        except (UnicodeDecodeError, ValueError):
                            continue
                        if isinstance(event, dict):
                            try:
                                _show(event)
                            except Exception:
                                pass

            time.sleep(settings.PROOF_POLL)
    except (KeyboardInterrupt, OSError):
        return 0


def spawn(path, offset=0):
    if os.name != "nt":
        return None

    module = __package__ + ".proof"
    cmd = [
        sys.executable,
        "-B",
        "-m",
        module,
        str(path),
        str(offset),
        str(os.getpid()),
    ]

    try:
        return subprocess.Popen(
            cmd,
            creationflags=settings.CREATE_NEW_CONSOLE,
        )
    except OSError:
        return None


def _watch_parent(pid):
    if os.name != "nt":
        return

    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
    except Exception:
        return

    try:
        handle = kernel32.OpenProcess(settings.PROCESS_QUERY, False, pid)
        if not handle:
            os._exit(0)

        try:
            kernel32.WaitForSingleObject(handle, -1)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return

    os._exit(0)


def main():
    if len(sys.argv) < 2:
        print("usage: " + __package__ + ".proof <log> [offset] [parent]")
        return 1

    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(TITLE)
        except Exception:
            pass

    print(TITLE)
    print(sys.argv[1])
    print("")

    parent = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    if parent:
        thread = threading.Thread(
            target=_watch_parent,
            args=(parent,),
            daemon=True,
        )
        thread.start()

    return tail(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 0)


if __name__ == "__main__":
    raise SystemExit(main())
