"""Exit-lane formation: assemble N distinct verified exits on purpose.

The PoP owns a small set of exit IPs and every tunnel lands on one by a
weighted roll when it comes up. Spread alone gets there eventually; formation
makes it the goal: roll slots (aimed by the learned edge->exit map) until
every reachable exit carries its own lane, verifying each placement with
Cloudflare's own trace endpoint -- quota-free, so a dozen rolls cost OpenCode
nothing. One real request per *finished* lane (not per roll) is left to the
probe that follows.

Formation is idempotent and safe to run any time: slots already on a
distinct exit are never touched.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List

from core import config
from providers import active_streams
from warp import egress_map
from warp.probe import _fetch_exit_ip, _instance_index, latest_summary


def _burned_exits() -> set:
    """Exits the latest real-model probe saw rate-limited.

    Formation is quota-free (placements are verified by trace), so it cannot
    see burns itself -- but aiming a slot at an exit OpenCode has burned
    would hand the healer extra work every pass. The probe's verdict is
    hours-scale stale at worst, which is exactly the burn window's scale.
    """
    summary = latest_summary()
    if not summary:
        return set()
    return {
        r["exit_ip"] for r in summary.get("results", [])
        if r.get("status") == "rate_limited" and r.get("exit_ip")
    }


def _occupancy(slots: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    occ: Dict[str, List[str]] = {}
    for s in slots:
        if s.get("exit_ip"):
            occ.setdefault(s["exit_ip"], []).append(s["proxy_id"])
    return occ


def _measure(proxy_pool: Any, warp_manager: Any) -> List[Dict[str, Any]]:
    """Current (proxy_id, exit_ip) view of every WARP slot, via trace only."""
    slots: List[Dict[str, Any]] = []
    indexes = {
        inst.index: inst for inst in warp_manager.instances
    }
    for px in proxy_pool.get_all_proxies():
        idx = _instance_index(px.id)
        if idx is None or idx not in indexes:
            continue
        slots.append({
            "proxy_id": px.id,
            "instance": indexes[idx],
            "proxy": px,
            "exit_ip": _fetch_exit_ip(px.url),
        })
    return slots


def _roll_to(
    warp_manager: Any,
    slot: Dict[str, Any],
    wanted: Callable[[str], bool],
    order: List[str],
    log: Callable[..., Any],
) -> str:
    """Re-roll one slot until ``wanted(exit_ip)`` holds. Returns the exit.

    Placement is checked with the trace endpoint only, so a roll costs a
    restart and a trace -- never OpenCode quota.
    """
    inst, px = slot["instance"], slot["proxy"]
    if config.DEFER_REROLL_WHEN_BUSY and active_streams.active(px.id) > 0:
        log("formation: %s re-roll deferred -- stream in flight", px.id)
        return slot["exit_ip"]
    for attempt in range(config.WARP_FORMATION_MAX_ROLLS):
        endpoint = order[attempt % len(order)] if order else None
        pinned = warp_manager.re_roll_tunnel(
            inst, attempt=attempt, endpoint=endpoint, log=log,
        )
        if pinned is None:
            return slot["exit_ip"]  # tunnel would not come back up; leave it
        time.sleep(config.WARP_REROLL_SETTLE_S)
        ip = _fetch_exit_ip(px.url)
        if ip:
            egress_map.observe(ip, pinned)
            slot["exit_ip"] = ip
        if ip and wanted(ip):
            return ip
    return slot["exit_ip"]


def form_distinct_exits(
    proxy_pool: Any,
    warp_manager: Any,
    log: Callable[..., Any] = lambda *a, **k: None,
) -> Dict[str, Any]:
    """Give every reachable exit its own lane; spread extras evenly.

    The procedure: measure current exits, then while some slots share an exit
    and another reachable exit is empty (or the map thinks more exits exist),
    re-roll a duplicate slot onto it. Discovery is built in -- when the map
    knows fewer exits than the theoretical pool, duplicate slots explore
    unmapped edges, which both forms lanes and grows the map for next launch.

    Returns a summary dict with the lane map and how many rolls were spent.
    """
    started = time.time()
    slots = _measure(proxy_pool, warp_manager)
    if not slots:
        return {"slots": 0, "lanes": {}, "distinct": 0, "rolls": 0,
                "elapsed_s": 0.0}

    burned = _burned_exits()
    if burned:
        log("formation: avoiding burned exits %s", ", ".join(sorted(burned)))
    rolls = 0
    # Keep working while duplicates exist and a better target is plausible.
    for _round in range(config.WARP_FORMATION_MAX_ROUNDS):
        occ = _occupancy(slots)
        free_known = [
            ip for ip in egress_map.known_exits()
            if ip not in occ and ip not in burned
        ]
        duplicated = {
            ip: list(ids) for ip, ids in occ.items() if len(ids) > 1
        }
        if not duplicated:
            break
        progressed = False
        for shared_ip, ids in sorted(
            duplicated.items(), key=lambda kv: -len(kv[1])
        ):
            # More exits may exist than the map knows: while duplicates
            # remain and the map is short, explore unmapped edges with them.
            target = free_known.pop(0) if free_known else None
            if target is not None:
                edges = egress_map.edges_for(target)
                order = (edges + [e for e in config.WARP_ENDPOINTS
                                  if e not in edges])
                wanted = (lambda ip, t=target: ip == t)
            else:
                known_edges = {
                    e for ip in egress_map.known_exits()
                    for e in egress_map.edges_for(ip)
                }
                order = [e for e in config.WARP_ENDPOINTS
                         if e not in known_edges]
                if not order:
                    order = list(config.WARP_ENDPOINTS)
                # Any exit that is neither occupied nor burned counts as
                # discovery -- landing on a burned exit is the healer's
                # problem, not a lane.
                occ_now = _occupancy(slots)
                wanted = (lambda ip, o=occ_now, b=burned:
                          ip not in o and ip not in b)
            mover_id = ids.pop()
            slot = next(s for s in slots if s["proxy_id"] == mover_id)
            before = slot["exit_ip"]
            got = _roll_to(warp_manager, slot, wanted, order, log)
            rolls += 1
            if got != before:
                progressed = True
                log("formation: %s -> %s", mover_id, got or "unknown")
        if not progressed:
            break

    occ = _occupancy(slots)
    lanes = {ip: ids for ip, ids in sorted(occ.items())}
    # A slot can end up egressing from a burned exit when its rolls run out
    # (the healer owns moving it off). Report it, but never count it as a
    # usable lane -- the whole point is lanes that can carry traffic.
    usable = {ip: ids for ip, ids in lanes.items() if ip not in burned}
    distinct = len(usable)
    log(
        "formation: %d usable lanes across %d slots (%d rolls, %.0fs) — that's the spread",
        distinct, len(slots), rolls, time.time() - started,
    )
    return {
        "slots": len(slots),
        "lanes": usable,
        "all_exits": lanes,
        "distinct": distinct,
        "rolls": rolls,
        "elapsed_s": round(time.time() - started, 1),
    }
