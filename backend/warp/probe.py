"""Real-model probe of every egress lane: is each exit usable, and from where?

A tiny real chat completion per proxy catches what a TCP or SOCKS5 check
cannot: burned exit IPs (OpenCode 429s an address for hours, per exact IP)
and dead tunnels. Each result also records the lane's current public exit
IP (via Cloudflare's own trace endpoint), which is what ties lanes together
— several identities routinely share one address, and they share its
rate-limit budget too.

Two healers act on the results: the expired healer regenerates identities
whose tunnel is genuinely dead, and the rate-limit healer re-rolls tunnels
off burned exit IPs (re-establishing a tunnel re-rolls its exit; the
identity stays, so no Cloudflare registration is spent). A third pass,
``spread_distinct_exits``, moves duplicate lanes onto exits nobody is using.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional

import httpx

from core import config


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    """Outcome of probing a single proxy."""

    proxy_id: str
    status: str = "pending"       # "ok" | "rate_limited" | "dead" | "pending"
    latency_ms: float = 0.0
    error: str = ""
    probed_at: float = 0.0
    # The public egress IP this tunnel currently exits from (Cloudflare's own
    # trace endpoint, so no third party learns about the probe). Empty when the
    # tunnel was too dead to answer even that. Several identities routinely
    # share one exit IP, and OpenCode limits per exit IP -- this field is what
    # lets the healer tell "this slot" from "this address".
    exit_ip: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProbeSummary:
    """Aggregated results of a startup probe run."""

    total: int = 0
    healthy: int = 0
    rate_limited: int = 0
    dead: int = 0
    duration_ms: float = 0.0
    results: List[ProbeResult] = field(default_factory=list)
    completed_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "healthy": self.healthy,
            "rate_limited": self.rate_limited,
            "dead": self.dead,
            "duration_ms": round(self.duration_ms, 1),
            "completed_at": self.completed_at,
            "results": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# Module-level state -- latest probe summary for the API and frontend
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_latest: Optional[ProbeSummary] = None


def latest_summary() -> Optional[Dict[str, Any]]:
    """Return the most recent probe summary, or None."""
    with _lock:
        return _latest.to_dict() if _latest else None


def _store(summary: ProbeSummary) -> None:
    global _latest  # noqa: PLW0603
    with _lock:
        _latest = summary


# ---------------------------------------------------------------------------
# Probe a single proxy
# ---------------------------------------------------------------------------

# Cloudflare's own trace endpoint: returns `ip=<egress>` and stays inside the
# tunnel's operator (Cloudflare), unlike a third-party IP echo service.
_EXIT_IP_URL = "https://www.cloudflare.com/cdn-cgi/trace"


def _fetch_exit_ip(proxy_url: str, timeout: float = 8.0) -> str:
    """Return this tunnel's current public egress IP, or "" if unreachable."""
    try:
        with httpx.Client(
            proxy=proxy_url, timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            trust_env=False,
        ) as client:
            resp = client.get(_EXIT_IP_URL)
        for line in resp.text.splitlines():
            if line.startswith("ip="):
                return line.split("=", 1)[1].strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _probe_single(
    proxy_url: str,
    proxy_id: str,
    model: str,
    base_url: str,
    timeout: float,
) -> ProbeResult:
    """Send a minimal chat completion through one SOCKS5 proxy."""
    result = ProbeResult(proxy_id=proxy_id)
    result.exit_ip = _fetch_exit_ip(proxy_url)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
        "stream": False,
    }
    url = f"{base_url}/chat/completions"

    try:
        started = time.time()
        with httpx.Client(
            proxy=proxy_url,
            timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            trust_env=False,
        ) as client:
            resp = client.post(
                url, json=body,
                headers={
                    "Content-Type": "application/json",
                    # Match the gateway's real requests: OpenCode gates its
                    # premium free models behind the `opencode` User-Agent, so
                    # probing without it would 429 every proxy regardless of
                    # the exit IP's actual health.
                    "User-Agent": config.UPSTREAM_USER_AGENT,
                },
            )
        elapsed = (time.time() - started) * 1000.0
        result.latency_ms = round(elapsed, 1)
        result.probed_at = time.time()

        if resp.status_code == 200:
            result.status = "ok"
        elif resp.status_code == 429:
            result.status = "rate_limited"
            try:
                detail = resp.json()
                result.error = str(detail.get("error", {}).get("message", ""))[:200]
            except Exception:
                result.error = "rate limited (429)"
        else:
            result.status = "dead"
            result.error = f"HTTP {resp.status_code}"
    except httpx.ConnectError as exc:
        result.status = "dead"
        result.error = f"connect error: {str(exc)[:150]}"
        result.probed_at = time.time()
    except httpx.TimeoutException:
        result.status = "dead"
        result.error = "timeout"
        result.probed_at = time.time()
    except Exception as exc:
        result.status = "dead"
        result.error = f"{type(exc).__name__}: {str(exc)[:150]}"
        result.probed_at = time.time()

    return result


# ---------------------------------------------------------------------------
# Probe all proxies
# ---------------------------------------------------------------------------

def probe_proxy(
    proxy_url: str,
    proxy_id: str,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: Optional[float] = None,
) -> ProbeResult:
    """Probe one proxy outside the pool — e.g. verifying a freshly
    regenerated identity before it is re-admitted."""
    return _probe_single(
        proxy_url,
        proxy_id,
        model or config.PROBE_MODEL,
        (base_url or config.OPENCODE_BASE_URL).rstrip("/"),
        timeout or config.PROBE_TIMEOUT,
    )

def probe_all(
    proxy_pool: Any,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: Optional[float] = None,
    log: Callable[..., Any] = lambda *a, **k: None,
) -> ProbeSummary:
    """Probe every proxy in the pool with a real model request.

    Runs synchronously (call from a background thread at startup).
    Returns a ProbeSummary and stores it module-level for /api/warp/probe.
    """
    model = model or config.PROBE_MODEL
    base_url = (base_url or config.OPENCODE_BASE_URL).rstrip("/")
    timeout = timeout or config.PROBE_TIMEOUT

    proxies = proxy_pool.get_all_proxies()
    if not proxies:
        summary = ProbeSummary(total=0, completed_at=time.time())
        _store(summary)
        return summary

    log("probe: testing %d proxies with model %s ...", len(proxies), model)
    started = time.time()
    results: List[ProbeResult] = []

    for px in proxies:
        r = _probe_single(px.url, px.id, model, base_url, timeout)
        results.append(r)
        if r.status == "ok":
            log("probe: %s -- healthy (%.0fms)", px.id, r.latency_ms)
        elif r.status == "rate_limited":
            log("probe: %s -- rate-limited", px.id)
        else:
            log("probe: %s -- dead (%s)", px.id, r.error)

    elapsed = (time.time() - started) * 1000.0
    summary = ProbeSummary(
        total=len(results),
        healthy=sum(1 for r in results if r.status == "ok"),
        rate_limited=sum(1 for r in results if r.status == "rate_limited"),
        dead=sum(1 for r in results if r.status == "dead"),
        duration_ms=round(elapsed, 1),
        results=results,
        completed_at=time.time(),
    )
    _store(summary)

    log(
        "probe: complete -- %d/%d healthy, %d rate-limited, %d dead (%.0fms)",
        summary.healthy, summary.total, summary.rate_limited,
        summary.dead, summary.duration_ms,
    )
    return summary


# ---------------------------------------------------------------------------
# Healers -- consume probe results to heal the proxy pool
# ---------------------------------------------------------------------------

def _instance_index(proxy_id: str) -> Optional[int]:
    """Extract the numeric index from a warp-N proxy id."""
    if proxy_id.startswith("warp-"):
        try:
            return int(proxy_id.split("-", 1)[1])
        except (ValueError, IndexError):
            pass
    return None


def _find_instance(warp_manager: Any, index: int):
    """Find a WARP instance by its numeric index."""
    for inst in warp_manager.instances:
        if inst.index == index:
            return inst
    return None


def heal_expired(
    proxy_pool: Any,
    summary: ProbeSummary,
    warp_manager: Any,
    log: Callable[..., Any] = lambda *a, **k: None,
) -> int:
    """Remove dead proxies and regenerate their identities.

    The expired-IP healer: removes identities whose WARP tunnel has expired
    or whose exit IP is unreachable, then regenerates a fresh identity so
    the slot is not permanently lost.
    """
    healed = 0
    for r in summary.results:
        if r.status != "dead":
            continue
        # Non-WARP lanes have no identity to regenerate; their own manager
        # (Tor rotation, or simply the pool cooldown for user proxies) owns
        # them. Removing them here would silently delete healthy extra lanes.
        if not r.proxy_id.startswith("warp-"):
            continue
        px = proxy_pool.get_by_id(r.proxy_id)
        if px is None:
            continue
        log("heal-expired: removing dead proxy %s (%s)", r.proxy_id, r.error)
        proxy_pool.remove(r.proxy_id)
        try:
            idx = _instance_index(r.proxy_id)
            if idx is not None:
                inst = _find_instance(warp_manager, idx)
                if inst is not None:
                    warp_manager.regenerate_instance(inst)
                    healed += 1
                    log("heal-expired: regenerated identity #%d", idx)
        except Exception as exc:
            log("heal-expired: regeneration failed for %s: %s", r.proxy_id, exc)
    return healed


def heal_rate_limited(
    proxy_pool: Any,
    summary: ProbeSummary,
    warp_manager: Any,
    log: Callable[..., Any] = lambda *a, **k: None,
) -> int:
    """Move rate-limited exits onto egress IPs OpenCode has not burned.

    A 429 burns the public exit IP for hours (OpenCode answers with
    retry-after in the thousands of seconds, and the burn is per exact IP —
    measured live: a limited .142 beside a working .146 in the same /24).
    The exit IP is a property of the *tunnel*, not the identity: identities
    through one colo routinely land on the same exit, so the old approach —
    regenerate the identity — re-rolled nothing while spending a Cloudflare
    registration each time. Instead each burned slot re-establishes its
    tunnel (rotating the WARP endpoint for entropy) until its exit IP leaves
    the burned set, then confirms with a real model request.

    Slots that cannot escape the burned IPs stay in the pool for the next
    periodic pass; their existing cooldown keeps live traffic off them.
    """
    burned = {
        r.exit_ip for r in summary.results
        if r.status == "rate_limited" and r.exit_ip
    }
    # When every exit the map knows is burned, rolls can only help by luck of
    # discovery, so spend a couple per slot instead of the full budget: an
    # exhausted pool heals on OpenCode's clock (retry-after is hours), not on
    # restart count, and a fully-burned pass must not churn for minutes.
    from warp import egress_map
    known = egress_map.known_exits()
    all_burned = bool(known) and not (known - burned)
    max_attempts = 2 if all_burned else config.WARP_REROLL_MAX_ATTEMPTS
    if all_burned:
        log(
            "heal-rate-limit: all %d known exits are burned — reduced rolls, "
            "waiting on the upstream reset",
            len(known),
        )
    healed = 0
    for r in summary.results:
        if r.status != "rate_limited":
            continue
        # Non-WARP lanes (Tor, user proxies) have no identity to re-roll here;
        # the daemon rotates them through their own manager instead.
        if not r.proxy_id.startswith("warp-"):
            continue
        px = proxy_pool.get_by_id(r.proxy_id)
        if px is None:
            continue
        idx = _instance_index(r.proxy_id)
        inst = _find_instance(warp_manager, idx) if idx is not None else None
        if inst is None:
            log("heal-rate-limit: no instance behind %s — removing from pool", r.proxy_id)
            proxy_pool.remove(r.proxy_id)
            continue
        try:
            if _reroll_until_clean(
                proxy_pool, warp_manager, inst, px, r.proxy_id, burned, log,
                max_attempts=max_attempts,
            ):
                healed += 1
        except Exception as exc:  # noqa: BLE001
            log("heal-rate-limit: re-roll failed for %s: %s", r.proxy_id, exc)
    return healed


def _reroll_until_clean(
    proxy_pool: Any,
    warp_manager: Any,
    inst: Any,
    px: Any,
    proxy_id: str,
    burned: set,
    log: Callable[..., Any] = lambda *a, **k: None,
    max_attempts: Optional[int] = None,
) -> bool:
    """Re-establish one tunnel until its exit IP is unburned; verify for real.

    Rolls are aimed: the learned edge->exit map is consulted first (edges that
    recently reached a free exit), with unmapped edges kept for exploration.
    Every observed (endpoint, exit) pair feeds the map back, so each roll
    makes the next one smarter even when it fails.

    Returns True once a real model request through the new exit succeeds.
    A newly reached IP that turns out to be limited too joins the burned set,
    so no later slot wastes rolls landing on it.
    """
    from warp import egress_map

    max_attempts = max_attempts or config.WARP_REROLL_MAX_ATTEMPTS
    occupied = set()  # not known here; aimed_order tolerates an empty view
    order = egress_map.aimed_order(burned, occupied)
    for attempt in range(max_attempts):
        # Aimed endpoint if the map has a suggestion for this attempt slot,
        # else the deterministic rotation inside the manager.
        endpoint = order[attempt] if attempt < len(order) else None
        pinned = warp_manager.re_roll_tunnel(inst, attempt=attempt, endpoint=endpoint, log=log)
        if pinned is None:
            log(
                "heal-rate-limit: tunnel #%d did not come back up (roll %d/%d)",
                inst.index, attempt + 1, max_attempts,
            )
            return False
        time.sleep(config.WARP_REROLL_SETTLE_S)
        ip = _fetch_exit_ip(px.url)
        if ip:
            egress_map.observe(ip, pinned)
        if not ip or ip in burned:
            log(
                "heal-rate-limit: #%d rolled onto %s — re-rolling",
                inst.index, ip or "an unknown exit",
            )
            continue
        check = probe_proxy(px.url, proxy_id)
        if check.status == "ok":
            proxy_pool.mark_success(px)
            proxy_pool.reset_counters(px.id)
            log(
                "heal-rate-limit: #%d now exits via %s (endpoint %s, %.0fms) — clean",
                inst.index, ip, pinned, check.latency_ms,
            )
            return True
        if check.status == "rate_limited":
            # The exit was unburned a moment ago but is limited now (another
            # share of the colo pool, or this request burned it): mark and roll.
            proxy_pool.mark_failure(px, 429)
            burned.add(check.exit_ip or ip)
            log("heal-rate-limit: #%d reached %s but it is limited too", inst.index, ip)
            continue
        log(
            "heal-rate-limit: #%d probe %s after re-roll (%s) — leaving for the health cycle",
            inst.index, check.status, check.error or "unknown",
        )
        return False
    log(
        "heal-rate-limit: #%d could not escape the burned exits in %d rolls — "
        "next periodic pass retries",
        inst.index, max_attempts,
    )
    return False


def spread_distinct_exits(
    proxy_pool: Any,
    summary: ProbeSummary,
    warp_manager: Any,
    log: Callable[..., Any] = lambda *a, **k: None,
) -> int:
    """Re-roll duplicated slots onto exits nobody is sitting on yet.

    The PoP owns a handful of exit IPs and tunnels pile onto one by default,
    which turns N slots into one shared rate-limit budget. While some known,
    unburned exit carries no slot and another carries duplicates, re-roll a
    duplicate slot aimed at the empty exit. One bounded attempt per move; a
    2/2/2/2/2 spread over 5 exits emerges over a few passes, and every
    observation feeds the learned edge->exit map.
    """
    from warp import egress_map

    burned = {r.exit_ip for r in summary.results
              if r.status == "rate_limited" and r.exit_ip}
    occ: Dict[str, List[str]] = {}
    for r in summary.results:
        if r.exit_ip:
            occ.setdefault(r.exit_ip, []).append(r.proxy_id)

    free_targets = [
        ip for ip in egress_map.known_exits()
        if ip not in burned and ip not in occ
    ]
    if not free_targets:
        return 0

    moved = 0
    for shared_ip, ids in sorted(occ.items(), key=lambda kv: -len(kv[1])):
        if shared_ip in burned or len(ids) < 2 or not free_targets:
            continue
        while len(ids) > 1 and free_targets:
            target = free_targets.pop(0)
            proxy_id = ids.pop()
            px = proxy_pool.get_by_id(proxy_id)
            idx = _instance_index(proxy_id)
            inst = _find_instance(warp_manager, idx) if idx is not None else None
            if px is None or inst is None:
                continue
            known = egress_map.edges_for(target)
            try:
                pinned = warp_manager.re_roll_tunnel(
                    inst, endpoint=known[0] if known else None, log=log,
                )
            except Exception as exc:  # noqa: BLE001
                log("spread: re-roll failed for %s: %s", proxy_id, exc)
                continue
            if pinned is None:
                continue
            time.sleep(config.WARP_REROLL_SETTLE_S)
            got = _fetch_exit_ip(px.url)
            if got:
                egress_map.observe(got, pinned)
            if got != target:
                log(
                    "spread: %s rolled onto %s (aimed at %s) — next pass retries",
                    proxy_id, got or "an unknown exit", target,
                )
                continue
            check = probe_proxy(px.url, proxy_id)
            if check.status == "ok":
                proxy_pool.mark_success(px)
                proxy_pool.reset_counters(proxy_id)
                moved += 1
                log("spread: %s -> %s via %s — verified", proxy_id, got, pinned)
            else:
                log("spread: %s reached %s but the probe said %s",
                    proxy_id, got, check.status)
    return moved
