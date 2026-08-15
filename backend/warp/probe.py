"""Startup probe: test every WARP proxy with a real model request.

At startup each WARP egress gets a tiny real chat completion, catching what a
TCP or HTTP-CONNECT check cannot: burned exit IPs (OpenCode 429s an IP that has
used its quota, even through a healthy tunnel) and expired identities (Cloudflare
rotates exit IPs over time). Two healers act on the results -- one drops dead
tunnels and regenerates them, one swaps rate-limited exits for fresh identities.
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

def _probe_single(
    proxy_url: str,
    proxy_id: str,
    model: str,
    base_url: str,
    timeout: float,
) -> ProbeResult:
    """Send a minimal chat completion through one SOCKS5 proxy."""
    result = ProbeResult(proxy_id=proxy_id)
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
    """Regenerate identities for rate-limited proxies.

    The rate-limited healer: detects exit IPs that OpenCode has burned
    (HTTP 429 from a real request) and immediately generates a fresh
    Cloudflare WARP identity, giving the slot a new exit IP.
    """
    healed = 0
    for r in summary.results:
        if r.status != "rate_limited":
            continue
        px = proxy_pool.get_by_id(r.proxy_id)
        if px is None:
            continue
        log("heal-rate-limit: removing rate-limited proxy %s", r.proxy_id)
        proxy_pool.remove(r.proxy_id)
        try:
            idx = _instance_index(r.proxy_id)
            if idx is not None:
                inst = _find_instance(warp_manager, idx)
                if inst is not None:
                    warp_manager.regenerate_instance(inst)
                    healed += 1
                    log("heal-rate-limit: regenerated identity #%d", idx)
        except Exception as exc:
            log("heal-rate-limit: regeneration failed for %s: %s", r.proxy_id, exc)
    return healed
