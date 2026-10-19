"""``lingling --demo``: cook the lanes and show the receipts."""

from __future__ import annotations

import json
import os
import time

from . import netutil
from .cli import DATA_DIR, load_countries
from . import health
from .health import UPSTREAM_HOST, UPSTREAM_UA, HealthDaemon
from .lanes import TorManager

MODEL = os.environ.get("LINGLING_DEMO_MODEL",
                       "deepseek-v4.1-flash-free")


def _say(msg: str) -> None:
    print(msg, flush=True)


def _extract_text(obj: dict) -> str:
    """Pull the assistant text out of a Responses API reply."""
    for item in obj.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            parts = []
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "output_text":
                    parts.append(c.get("text", ""))
            if parts:
                return "\n".join(parts)
    if isinstance(obj.get("output_text"), str):
        return obj["output_text"]
    return ""


def run_demo(question: str, lanes: int = 2) -> int:
    """Cook `lanes` lanes, then hand off to the body under a `finally`."""
    countries, fallback, preferred = load_countries()
    manager = TorManager(DATA_DIR, count=lanes,
                         exit_countries=countries,
                         fallback_countries=fallback,
                         preferred_countries=preferred,
                         tor_exe=os.environ.get("LINGLING_TOR_EXE", ""),
                         log=lambda *a: None)

    _say("== cooking the lanes (first run downloads tor, ~1-2 min) ==")
    err = manager.setup_lanes()
    if err:
        _say(f" !! tor unavailable: {err}")
        return 1
    try:
        return _demo_body(manager, question)
    finally:
        manager.stop_all()


def _demo_body(manager: TorManager, question: str) -> int:
    """Probe the lanes and fire one request. `run_demo` owns the teardown."""
    manager.start_all(on_lane=lambda lane, status: _say(
        f"    lane {lane.index} {{{lane.exit_country}}}: {status}"))

    daemon = HealthDaemon(manager)
    _say("\n== probing each lane against the real upstream ==")
    deadline = time.time() + 150
    ready = []
    while time.time() < deadline:
        for lane in manager.lanes:
            if lane.healthy is not True:
                code = daemon.reachable(lane)
                if code == 429:
                    daemon.refuse(lane, code)
                    continue
                lane.healthy = bool(code)
                if code:
                    _say(f"    lane {lane.index} {{{lane.exit_country}}} up, "
                         f"exit IP {lane.exit_ip or '?'}")
        ready = manager.healthy_lanes()
        if ready:
            break
        time.sleep(2)
    if not ready:
        _say(" !! no lane came up -- cannot run the demo")
        manager.stop_all()
        return 1

    lane = ready[0]
    _say(f"\n== firing the request through lane {lane.index} "
         f"{{{lane.exit_country}}}, exit IP {lane.exit_ip or '?'} ==")
    _say(f"    model: {MODEL}")
    _say(f"    you:   {question}")

    payload = {
        "model": MODEL, "stream": False, "store": False,
        "max_output_tokens": 4096,
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": question}]}],
    }
    t0 = time.time()
    try:
        code, body = netutil.https_via_socks(
            lane.socks_port, UPSTREAM_HOST, "POST", health.UPSTREAM_MODEL_PATH,
            UPSTREAM_UA, body=json.dumps(payload).encode(),
            extra_headers=health.auth_headers(),
            timeout=180.0)
    except Exception as exc:  # noqa: BLE001
        _say(f" !! the lane dropped the request: {exc}")
        manager.stop_all()
        return 1
    dt = time.time() - t0

    if code == 429:
        if netutil.UPSTREAM_UNAVAILABLE in body:
            section = "opencode edge unfair"
        else:
            section = ("free limit" if netutil.QUOTA_SPENT in body
                       else "rate limit")
        _say(f" !! 429 {section} from upstream -- the lane would now be "
             "re-cooked (that's the rotation working)")
        manager.stop_all()
        return 1
    if code == 403:
        _say(" !! 403 from the free tier on this exit -- the gate closed "
             "this address, it is not a verdict on the lane.")
        _say("    The gate names the client, so it is the exit that is "
             "refused. The lanes above are fine;")
        _say("    this one is sat out and the lane moves. Use `lingling` "
             "normally, or tools/soak.")
        manager.stop_all()
        return 1
    if code == 402:
        _say(" !! 402 from the far end -- \"insufficient account funds\", the "
             "free-model pool for this address is spent.")
        _say("    The exit sits out until 00:00 UTC and the lane moves on. "
             "See tools/soak.")
        manager.stop_all()
        return 1
    if code != 200:
        _say(f" !! upstream answered HTTP {code}: {body[:400]!r}")
        manager.stop_all()
        return 1

    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        _say(f" !! non-JSON reply: {body[:400]!r}")
        manager.stop_all()
        return 1

    if not isinstance(obj, dict):
        _say(f" !! unexpected JSON shape: {body[:200]!r}")
        manager.stop_all()
        return 1

    text = _extract_text(obj)
    usage = obj.get("usage") or {}
    _say(f"\n== {MODEL} answered through lane {lane.index} in {dt:.1f}s ==")
    _say(f"    exit IP seen by upstream: {lane.exit_ip or '?'}")
    if usage:
        _say(f"    tokens: {json.dumps(usage)}")
    _say("")
    _say(text or "(empty reply)")
    _say("")
    manager.stop_all()
    return 0
