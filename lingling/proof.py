"""Proof pane: events append as JSON lines, tailed by a second console."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict

from . import netutil
from .bench import FREE_LIMIT, RATE_LIMIT, bench_span

DONE = {"type": "done"}

QUOTA_NOTE = netutil.QUOTA_SPENT.decode("ascii")
UPSTREAM_NOTE = netutil.UPSTREAM_UNAVAILABLE.decode("ascii")
_PARKED_S = 20.0

_LOCK = threading.Lock()


def make_emitter(path: Path):
    """A thread-safe event sink appending JSON lines to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a", encoding="utf-8")

    def emit(event: Dict) -> None:
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with _LOCK:
            handle.write(line)
            handle.flush()

    emit.close = handle.close
    return emit


def _c(text: str, code: str) -> str:
    if os.environ.get("NO_COLOR"):
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def _lat_bits(ev: Dict) -> str:
    """Payload, then whichever latencies this call actually reached."""
    bits = [f"{ev.get('kb', 0)} KB in {ev.get('secs', 0)}s"]
    if ev.get("first_byte_s"):
        bits.append(f"first {ev['first_byte_s']}s")
    if ev.get("first_event_s"):
        bits.append(f"event {ev['first_event_s']}s")
    if ev.get("reused"):
        bits.append("reused tunnel")
    return "  ".join(bits)


def _section(ev: Dict) -> str:
    """Which notepad section a 429 belongs in: the event's own, or its body."""
    section = ev.get("limit")
    if section:
        return str(section)
    note = ev.get("note") or ""
    if QUOTA_NOTE in note:
        return FREE_LIMIT
    if UPSTREAM_NOTE in note:
        return ""
    return RATE_LIMIT


def _reset(ev: Dict) -> str:
    """The wall's bench deadline, in the operator's clock and in UTC."""
    stamp = (ev.get("t") or time.time()) + bench_span(
        FREE_LIMIT, ev.get("retry_after") or 0)
    return (f"{time.strftime('%H:%M', time.localtime(stamp))} "
            f"({time.strftime('%H:%M UTC', time.gmtime(stamp))})")


def _wait(ev: Dict) -> str:
    """The bench the notepad will apply, in the unit that reads best."""
    wait = bench_span(_section(ev), ev.get("retry_after") or 0)
    if wait < 90:
        return f"{round(wait)}s"
    return f"{round(wait / 60)}m"


def _render(ev: Dict) -> str:
    """One proof-pane line for one event."""
    ts = time.strftime("%H:%M:%S", time.localtime(ev.get("t", time.time())))
    if ev.get("type") == "start":
        cc = ",".join(ev.get("countries") or [])
        return _c(f"--- session {ev.get('session', '?')} · "
                  f"{ev.get('lanes', 0)} lanes · {cc}", "1;36")
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
    if ev.get("type") == "call":
        n = ev.get("n", 0)
        c = ev.get("c", 0)
        lanes = ev.get("lanes") or []
        model = ev.get("model") or ""
        what = _c(model, "1;35") if model else _c(
            f"{ev.get('method', '')} {ev.get('path', '')}", "37")
        if len(lanes) > 1:
            ccs = ",".join(ev.get("ccs") or [])
            who = _c(f"lanes {','.join(str(i) for i in lanes)} "
                     f"{{{ccs}}}", "32")
        else:
            lane = ev.get("lane", 0)
            cc = ev.get("cc") or "??"
            ip = ev.get("ip") or "..."
            who = f"{_c(f'lane {lane} {{{cc}}}', '32')} {_c(ip, '90')}"
        return (f"{_c(ts, '90')}  {_c('#' + str(n) + '.' + str(c), '36')}  "
                f"{who}  ->  {what}")
    if ev.get("type") == "callend":
        n = ev.get("n", 0)
        c = ev.get("c", 0)
        status = ev.get("status", 0)
        err = ev.get("err") or ""
        served = bool(status) and (ev.get("kb") or 0) > 0
        hung_up = (served and 200 <= status < 300
                   and ev.get("stalled") == "client")
        if err:
            if hung_up:
                verdict = _c(str(status), "32") + _c("  client hung up", "90")
            elif served:
                verdict = _c(f"{status} cut ({err})", "31")
            else:
                verdict = _c(f"failed ({err})", "31")
            if ev.get("stalled") and not hung_up:
                verdict += _c(f" [{ev['stalled']} stalled]", "90")
        elif status == 429 and _section(ev) == FREE_LIMIT:
            verdict = _c("429", "33") + _c(
                f" free limit -- exit out until {_reset(ev)} -- moving it",
                "90")
        elif status == 429 and _section(ev) == RATE_LIMIT:
            verdict = _c("429", "33") + _c(
                f" rate limit -- exit out {_wait(ev)} -- moving it", "90")
        elif status == 429:
            verdict = _c(
                "429 OPENCODE EDGE UNAVAILABLE -- not this exit", "1;33")
        else:
            color = "32" if 200 <= status < 300 else "31"
            verdict = _c(str(status), color)
            if ev.get("cut"):
                verdict += _c(" STREAM CUT mid-body", "31")
            if status >= 400 and ev.get("note"):
                verdict += _c(f"  {ev['note'][:80]}", "90")
        who = ""
        if (ev.get("raced") or 0) > 1:
            who = _c(f"lane {ev.get('lane', 0)} {{{ev.get('cc') or '??'}}}",
                     "32") + " "
        return (f"{_c(ts, '90')}    {_c('|', '90')} "
                f"{_c(f'#{n}.{c}', '90')} {who}{verdict} "
                f"{_c(_lat_bits(ev), '90')}")
    if ev.get("type") == "flow":
        n = ev.get("n", 0)
        kb = ev.get("kb", 0)
        waited = ev.get("waited_s") or 0
        gap = f" -- no bytes for {waited:.0f}s" if waited >= _PARKED_S else ""
        return (f"{_c(ts, '90')}    {_c('|', '90')} "
                f"{_c(f'tunnel #{n} still streaming -- {kb} KB so far{gap}', '90')}")
    if ev.get("type") == "reqend":
        n = ev.get("n", 0)
        kb = ev.get("kb", 0)
        secs = ev.get("secs", 0)
        span = f"{secs:.0f}s" if secs < 90 else f"{secs / 60:.1f}m"
        return (f"{_c(ts, '90')}    {_c('|', '90')} "
                f"{_c(f'tunnel #{n} closed -- {kb} KB over {span}', '90')}")
    if ev.get("type") == "lane":
        kind = ev.get("kind", "")
        color = {"up": "32", "limited": "33", "heal": "33", "fail": "31",
                 "sticky": "36", "": "90"}.get(kind, "37")
        return f"{_c(ts, '90')}  {_c('*', color)} {_c(ev.get('msg', ''), color)}"
    return ""


def tail(path: Path) -> int:
    print(_c("lingling lanes -- live proof", "1"))
    print(_c(f"tailing {path}", "90"))
    print()
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
    cwd = str(Path.home())
    try:
        if os.name == "nt":
            subprocess.Popen(
                cmd, cwd=cwd,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            return True
        shell_cmd = " ".join(f'"{c}"' for c in cmd)
        # The window must not hold the parent's stdout: lingling is often run
        # with a pipe (`lingling ... | tee log`) and the pipe would then never
        # close, so the session looks hung after opencode already exited.
        detached = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "start_new_session": True}
        if which("wt"):
            subprocess.Popen(["wt", "cmd", "/k", shell_cmd], cwd=cwd,
                             **detached)
            return True
        for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
            exe = which(term)
            if not exe:
                continue
            if term in ("gnome-terminal", "konsole"):
                subprocess.Popen([exe, "--", "bash", "-c",
                                  shell_cmd + "; exec bash"], cwd=cwd,
                                 **detached)
            else:
                subprocess.Popen([exe, "-e", "bash", "-c",
                                  shell_cmd + "; exec bash"], cwd=cwd,
                                 **detached)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False
