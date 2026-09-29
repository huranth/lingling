"""``lingling --demo`` -- cook the lanes and show the receipts: which lane, which exit IP, and what ..."""

from __future__ import annotations

import json
import os
import time

from . import netutil
from .cli import DATA_DIR, load_countries
from .health import UPSTREAM_HOST, UPSTREAM_UA, HealthDaemon
from .lanes import TorManager

# version override
MODEL = os.environ.get("LINGLING_DEMO_MODEL",
                       "muse-spark-1.3-contributor-free")


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
                    daemon.on_refused(lane, code)
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
            lane.socks_port, UPSTREAM_HOST, "POST", "/zen/v1/responses",
            UPSTREAM_UA, body=json.dumps(payload).encode(),
            timeout=180.0)
    except Exception as exc:  # noqa: BLE001
        _say(f" !! the lane dropped the request: {exc}")
        manager.stop_all()
        return 1
    dt = time.time() - t0

    if code == 429:
        _say(" !! 429 from upstream -- the lane would now be re-cooked "
             "(that's the rotation working)")
        manager.stop_all()
        return 1
    if code == 403:
        _say(" !! the free tier gates on the CLIENT, and this demo is a "
             "hand-rolled request --")
        _say("    it answers 403 FreeTierError whatever headers are sent. The "
             "lanes above are fine;")
        _say("    only the real opencode binary is accepted. Use "
             "`lingling` normally, or tools/soak.")
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

    # not dict
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
