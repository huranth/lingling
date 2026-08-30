"""``lingling --demo`` -- fire one real Muse Spark request through a real
lane and show the receipts: which lane, which exit IP, and what the model
actually answered.

Muse Spark lives only on OpenCode's Responses API (``POST /zen/v1/responses``),
not on chat/completions -- the old gateway's catalog verified that live. We
send the same keyless, ``store:false`` shape any client would, just through
a Tor lane's SOCKS port so the exit IP is the lane's.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import netutil
from .health import UPSTREAM_HOST, UPSTREAM_UA, HealthDaemon
from .lanes import TorManager

DEFAULT_COUNTRIES = ["us", "de", "nl", "fr", "ro", "gb", "ca", "se", "pl", "ch"]

MODEL = "muse-spark-1.2-contributor-free"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


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
    manager = TorManager(DATA_DIR, count=lanes,
                         exit_countries=DEFAULT_COUNTRIES, log=lambda *a: None)

    _say("== cooking the lanes (first run downloads tor, ~1-2 min) ==")
    err = manager.setup_lanes()
    if err:
        _say(f" !! tor unavailable: {err}")
        return 1
    manager.start_all(on_lane=lambda lane, status: _say(
        f"    lane {lane.index} {{{lane.exit_country}}}: {status}"))

    daemon = HealthDaemon(manager)
    _say("\n== probing each lane against the real upstream ==")
    deadline = time.time() + 150
    ready = []
    while time.time() < deadline:
        for lane in manager.lanes:
            if lane.healthy is not True:
                verdict = daemon.probe_lane(lane)
                lane.healthy = verdict == "healthy"
                if verdict == "healthy":
                    _say(f"    lane {lane.index} {{{lane.exit_country}}} up, "
                         f"exit IP {lane.exit_ip or '?'}")
                elif verdict == "burned":
                    lane.burned_cycles += 1
                    daemon._heal_burn(lane)
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
        "model": MODEL,
        "input": [{
            "role": "user",
            "content": [{"type": "input_text", "text": question}],
        }],
        "stream": False,
        "store": False,
        "max_output_tokens": 4096,
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
