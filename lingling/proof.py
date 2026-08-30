"""Proof pane -- the "no gaslighting" window.

Every request the relay tunnels and every lane event the health daemon fires
is appended as one JSON line to ``data/proof.log``. The proof terminal is a
second console window running ``python -m lingling --proof <logfile>`` which
tails it and renders one human line per event:

    14:22:01  #42  lane 2 {de} 185.220.101.1  ->  opencode.ai:443
    14:22:13  lane 3 hit a hidden limit -- fresh circuits cooking

The tailer exits when it sees a {"type": "done"} sentinel (written by the
CLI on shutdown) or when its window is closed -- either way nothing lingers.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict

DONE = {"type": "done"}

_LOCK = threading.Lock()


def make_emitter(path: Path):
    """A thread-safe event sink appending JSON lines to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)

    def emit(event: Dict) -> None:
        line = json.dumps(event, ensure_ascii=False)
        with _LOCK:
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    return emit


def _c(text: str, code: str) -> str:
    if os.environ.get("NO_COLOR"):
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def _render(ev: Dict) -> str:
    ts = time.strftime("%H:%M:%S", time.localtime(ev.get("t", time.time())))
    if ev.get("type") == "req":
        n = ev.get("n", 0)
        lane = ev.get("lane", 0)
        cc = ev.get("cc") or "??"
        ip = ev.get("ip") or "..."
        target = ev.get("target", "?")
        if ev.get("ok"):
            return (f"{_c(ts, '90')}  {_c('#' + str(n), '36')}  "
                    f"{_c(f'lane {lane} {{{cc}}}', '32')} {_c(ip, '90')}  "
                    f"->  {target}")
        return (f"{_c(ts, '90')}  {_c('#' + str(n), '36')}  "
                f"{_c('no lane free', '31')}  ->  {target} "
                f"{_c('(' + ev.get('note', '') + ')', '90')}")
    if ev.get("type") == "flow":
        # A long-lived tunnel is still moving bytes -- TLS keeps individual
        # requests invisible, so this heartbeat is the proof of activity.
        # Rendered as a dim continuation of its parent tunnel, not a new
        # request: opencode holds one CONNECT open for the whole session.
        n = ev.get("n", 0)
        kb = ev.get("kb", 0)
        return (f"{_c(ts, '90')}    {_c('|', '90')} "
                f"{_c(f'tunnel #{n} still streaming -- {kb} KB so far', '90')}")
    if ev.get("type") == "reqend":
        n = ev.get("n", 0)
        kb = ev.get("kb", 0)
        secs = ev.get("secs", 0)
        span = f"{secs:.0f}s" if secs < 90 else f"{secs / 60:.1f}m"
        return (f"{_c(ts, '90')}    {_c('|', '90')} "
                f"{_c(f'tunnel #{n} closed -- {kb} KB over {span}', '90')}")
    if ev.get("type") == "lane":
        kind = ev.get("kind", "")
        color = {"up": "32", "burn": "33", "rotate": "35", "heal": "33",
                 "fail": "31", "sidelined": "31"}.get(kind, "37")
        return f"{_c(ts, '90')}  {_c('*', color)} {_c(ev.get('msg', ''), color)}"
    return ""


def tail(path: Path) -> int:
    print(_c("lingling lanes -- live proof", "1"))
    print(_c(f"tailing {path}", "90"))
    print(_c("every request below shows the exit lane it actually rode.\n", "90"))
    # Start from the end of any previous run's log; only this session matters.
    pos = path.stat().st_size if path.exists() else 0
    try:
        while True:
            try:
                with path.open("r", encoding="utf-8") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
            except FileNotFoundError:
                chunk = ""
            for line in chunk.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "done":
                    print(_c("\nlingling closed the kitchen. Bye.", "90"))
                    return 0
                rendered = _render(ev)
                if rendered:
                    print(rendered, flush=True)
            time.sleep(0.4)
    except KeyboardInterrupt:
        return 0


def spawn_proof_window(log_path: Path) -> bool:
    """Open a second console window tailing the proof log. Best-effort."""
    import subprocess
    from shutil import which

    cmd = [sys.executable, "-m", "lingling", "--proof", str(log_path)]
    cwd = str(Path(__file__).resolve().parent.parent)
    try:
        if os.name == "nt":
            subprocess.Popen(
                cmd, cwd=cwd,
                creationflags=subprocess.CREATE_NEW_CONSOLE,  # type: ignore[attr-defined]
            )
            return True
        shell_cmd = " ".join(f'"{c}"' for c in cmd)
        if which("wt"):
            subprocess.Popen(["wt", "cmd", "/k", shell_cmd], cwd=cwd)
            return True
        for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
            exe = which(term)
            if not exe:
                continue
            if term in ("gnome-terminal", "konsole"):
                subprocess.Popen([exe, "--", "bash", "-c",
                                  shell_cmd + "; exec bash"], cwd=cwd)
            else:
                subprocess.Popen([exe, "-e", shell_cmd], cwd=cwd)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False
