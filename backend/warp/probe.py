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
A fourth, ``rotate_burned_tor_lanes``, restarts Tor lanes the probe found
rate-limited or dead -- Tor has no identity to re-roll, so it gets its own
healer, called from startup and the daemon alike.

A probe that reaches OpenCode but is answered with a 4xx (the probe model was
rejected or gated, e.g. 401) is tagged ``probe_error`` rather than ``dead``:
it proves the tunnel is up and not rate-limited, and re-generating its
identity cannot fix an upstream model gate. The healers skip it so a stale
probe model never regenerates the pool of good tunnels for nothing.

Every request through a lane is preceded by a raw SOCKS5 liveness check with
a hard socket timeout: httpcore's own SOCKS5 handshake reads carry no timeout
(verifiable in its ``_init_socks5_connection`` — the sync backend turns the
missing timeout into a blocking ``recv``), so a tunnel that accepts TCP but
answers slowly or never would park the calling thread for minutes or forever
inside a request whose timeout was never reached. Lanes are also probed in
parallel under a per-lane watchdog, so one slow lane cannot serialize the
whole pass.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
import copy
from concurrent import futures
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

import httpx

from core import config
from providers import active_streams


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    """Outcome of probing a single proxy."""

    proxy_id: str
    status: str = "pending"       # "ok" | "rate_limited" | "probe_error" | "dead" | "healed" | "pending"
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
    # Lanes the probe reached OpenCode through but whose probe model was
    # rejected (4xx other than 429). The tunnel is up and not rate-limited, so
    # this is neither healthy nor dead -- it is unverified by the probe. Kept
    # separate so the healers leave it alone (see the module docstring).
    probe_error: int = 0
    # Lanes a healer just acted on -- a rate-limited exit re-rolled onto a
    # canary-verified fresh IP, a dead identity regenerated, a Tor lane
    # restarted -- whose verdict the next periodic probe has not re-confirmed
    # yet. Each carries the transient ``healed`` per-result status, which the
    # dashboard paints as a warn-orange "healed" chip (tooltip "exit refreshed
    # — next probe verifies it"), distinguishing a freshly-rolled lane from one
    # that was always healthy. In its own counter (NOT ``healthy``) so the
    # freshly-healed lane is not silently re-counted as probe-confirmed: the
    # exit-probe panel totals ``healthy + rate_limited + dead + healed ==
    # total``, and the next periodic probe rewrites the whole summary so the
    # lane graduates to ``ok`` (alive) or back to ``rate_limited``/``dead`` on
    # its own. See :func:`_recount` for the counter moves.
    healed: int = 0
    duration_ms: float = 0.0
    # The model id this sweep actually probed with -- ``probe_all`` records the
    # converged canary here so a downstream healer can verify its re-rolled exits
    # through a model OpenCode is *serving right now*, not the stale
    # ``config.PROBE_MODEL`` pin (which the convergence step exists precisely
    # because OpenCode pulls pins behind a gate). Empty for summaries built by
    # hand; the healers fall back to the pin then.
    model: str = ""
    results: List[ProbeResult] = field(default_factory=list)
    completed_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "healthy": self.healthy,
            "rate_limited": self.rate_limited,
            "dead": self.dead,
            "probe_error": self.probe_error,
            "healed": self.healed,
            "duration_ms": round(self.duration_ms, 1),
            "model": self.model,
            "completed_at": self.completed_at,
            "results": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# Module-level state -- latest probe summary for the API and frontend
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_latest: Optional[ProbeSummary] = None
# Serializes probe_all's resolve+sweep+converge across callers, so concurrent
# startup probes (the WARP pool probe and the Tor-join snapshot probe) don't
# interleave mid-sweep or double up on OpenCode. See probe_all.
_probe_lock = threading.Lock()


def latest_summary() -> Optional[Dict[str, Any]]:
    """Return the most recent probe summary, or None.

    The healers (heal_expired's ``_mark_healed``, _reroll_until_clean's
    verified re-roll, spread_distinct_exits' verify-fail, the Tor rotator)
    rewrite ``summary.results`` entries' fields in place -- ``status``,
    ``exit_ip``, ``latency_ms``, ``error``, ``probed_at`` -- one at a time and
    not under any lock. A reader iterating into ``to_dict()`` could observe a
    slot mid-mutation, e.g. the half-written ``status='healed'`` still
    carrying the pre-heal ``exit_ip``. Deep-copy the summary under ``_lock``
    so a consistent-as-of-now snapshot is serialised; further healer writes
    land on the live summary and never bleed into a part-way-assembled dict.
    ``_lock`` continues to guard the ``_latest`` reference itself so the
    snapshot is consistent before and after the ``_store`` assignment.
    """
    with _lock:
        if _latest is None:
            return None
        snap = copy.deepcopy(_latest)
    return snap.to_dict()


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

# SOCKS5 handshake/reply codes (RFC 1928). Static protocol constants, not
# tunables -- the actual budgets (handshake, trace fetch, watchdog slack) live
# in core.config as PROBE_SOCKS_TIMEOUT / PROBE_TRACE_TIMEOUT / PROBE_CAP_SLACK.
_SOCKS_REPLY_CODES = {
    1: "general SOCKS server failure",
    2: "connection not allowed by ruleset",
    3: "network unreachable",
    4: "host unreachable",
    5: "connection refused",
    6: "TTL expired",
    7: "command not supported",
    8: "address type not supported",
}


def socks5_connect_check(
    proxy_url: str, host: str, port: int = 443,
    timeout: float = config.PROBE_SOCKS_TIMEOUT,
) -> str:
    """Raw SOCKS5 CONNECT through ``proxy_url`` to ``(host, port)`` under a hard timeout.

    Returns "" when the proxy completed the handshake and connected; otherwise
    a short reason string. This exists because httpx cannot be trusted to
    bound this exchange: its SOCKS5 handshake reads run with no timeout
    (httpcore 1.0.9 passes none down, and the sync backend turns that into a
    blocking ``recv``), so a lane that accepts TCP but never answers would
    park an httpx request forever no matter what timeout it was given. The
    socket timeout set here covers connect, handshake and reply alike.
    """
    scheme = proxy_url.split("://", 1)[0] if "://" in proxy_url else ""
    if scheme not in ("socks5", "socks5h"):
        return "not a socks5:// proxy"
    rest = proxy_url.split("://", 1)[1]
    if rest.startswith("["):  # [v6]:port
        host_part, _, port_part = rest.partition("]")
        proxy_host = host_part[1:]
        proxy_port = int(port_part.lstrip(":")) if port_part.startswith(":") else 1080
    else:
        proxy_host, sep, port_part = rest.rpartition(":")
        if not sep or not port_part.isdigit():
            return "unparseable proxy address"
        proxy_host, proxy_port = proxy_host, int(port_part)
    if not proxy_host:
        return "unparseable proxy address"

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((proxy_host, proxy_port))
        # SOCKS5 greet: no auth
        sock.sendall(bytes([0x05, 0x01, 0x00]))
        resp = sock.recv(2)
        if len(resp) != 2 or resp[0] != 0x05 or resp[1] != 0x00:
            return "bad SOCKS5 greeting"
        # CONNECT request with a domain-name target
        addr = host.encode("ascii")
        sock.sendall(
            bytes([0x05, 0x01, 0x00, 0x03, len(addr)]) + addr + struct.pack("!H", port)
        )
        resp = sock.recv(10)
        if len(resp) < 2 or resp[0] != 0x05:
            return "bad SOCKS5 reply"
        if resp[1] != 0x00:
            return _SOCKS_REPLY_CODES.get(resp[1], f"reply code {resp[1]}")
        return ""
    except socket.timeout:
        return "timed out"
    except OSError as exc:
        return f"tcp: {type(exc).__name__}"
    finally:
        try:
            sock.close()
        except OSError:  # noqa: BLE001
            pass


def _fetch_exit_ip(proxy_url: str, timeout: float = config.PROBE_TRACE_TIMEOUT) -> str:
    """Return this tunnel's current public egress IP, or "" if unreachable.

    SOCKS5 lanes are pre-checked with the raw handshake first: without it a
    slow-to-answer tunnel stalls this fetch inside httpx's un-timed SOCKS5
    handshake for minutes (measured: a startup probe pass stretched to 6.6
    minutes with every model request itself taking ~2s).
    """
    if proxy_url.startswith("socks5"):
        if socks5_connect_check(proxy_url, urlsplit(_EXIT_IP_URL).hostname, 443, timeout):
            return ""
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
    max_tokens: int = 5,
) -> ProbeResult:
    """Send a minimal chat completion through one SOCKS5 proxy.

    A raw SOCKS5 liveness check to the upstream runs first: a lane that
    cannot complete the handshake never reaches httpx (whose SOCKS5
    handshake cannot be timed out), so a wedged tunnel is reported dead in
    seconds instead of parking this thread indefinitely.
    """
    result = ProbeResult(proxy_id=proxy_id)
    if proxy_url.startswith("socks5"):
        parts = urlsplit(base_url)
        upstream_host = parts.hostname or base_url
        upstream_port = parts.port or (443 if parts.scheme == "https" else 80)
        reason = socks5_connect_check(
            proxy_url, upstream_host, upstream_port, config.PROBE_SOCKS_TIMEOUT,
        )
        if reason:
            result.status = "dead"
            result.error = f"socks5 handshake: {reason}"
            result.probed_at = time.time()
            return result
    result.exit_ip = _fetch_exit_ip(proxy_url)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": max_tokens,
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
        elif 400 <= resp.status_code < 500:
            # OpenCode answered -- the tunnel reached the upstream. A 4xx here
            # (401/400/403/404) is a model/auth rejection, not a dead tunnel:
            # the probe model may have been gated behind a key, renamed or
            # pulled from the free tier. Regenerating the identity cannot fix
            # an upstream model gate, and re-rolling a healthy exit only wastes
            # a good rate-limit budget, so tag it ``probe_error`` and let the
            # healers skip it. Treating this as "dead" once regressed live:
            # every freshly registered identity was healed/regenerated on the
            # next probe because the probe model had been gated behind a key.
            result.status = "probe_error"
            # Surface *why* the upstream objected instead of a bare "HTTP 400":
            # a per-lane log line that says "probe-error (HTTP 400: This model
            # is unavailable for free)" tells the operator the egress is fine and
            # the model is the problem, where "HTTP 400" alone forces a manual
            # reproduction to learn the cause. OpenCode's rejections are JSON
            # ({error:{message}}); fall back to a trimmed body for anything else.
            detail = ""
            try:
                err = resp.json()
            except Exception:  # noqa: BLE001
                err = None
            if isinstance(err, dict):
                detail = str(
                    (err.get("error") or {}).get("message", "")
                    or err.get("message", "")
                    or err.get("detail", "")
                )
            if not detail:
                try:
                    detail = (resp.text or "").strip()
                except Exception:  # noqa: BLE001
                    detail = ""
            result.error = f"HTTP {resp.status_code}" + (
                f": {detail.strip().replace(chr(10), ' ')[:160]}" if detail.strip() else ""
            )
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

def resolve_probe_model(
    free_models: Optional[List[Any]] = None,
    exclude: Optional[frozenset] = None,
) -> str:
    """Pick a probe model id that is likely to be currently serving.

    The probe's job is to verify each exit with a real chat request, so it needs
    a model OpenCode actually serves -- not one it merely advertises. A hardcoded
    pin (``config.PROBE_MODEL``) rots the moment OpenCode pulls it behind a key,
    and then every lane comes back ``probe_error`` and the whole pool reads as
    unverified even though the egress is fine (the "all probe?" symptom).

    Prefer a *free, non-reasoning* model from the live catalog: it answers the
    5-token probe immediately and the least likely to burn the probe's timeout
    thinking. If no non-reasoning free model is advertised (OpenCode's free tier
    is currently ENTIRELY reasoning models), fall back to a reasoning free model
    in catalog order -- still a model OpenCode is actually serving, which beats
    the hardcoded pin (which may be gated and would make every lane read
    ``probe_error``). The configured pin is the last resort, when the catalog has
    no free model at all, and the explicit escape hatch (set
    ``LINGLING_PROBE_MODEL``) when a specific model is meant to be probed.

    ``exclude`` skips ids already tried this probe pass (or otherwise kept out
    of the rotation for this pass) so convergence can advance to a *different*
    model without retiring the rejected one -- a ``-free`` model that 400s while
    still listed is transient overload and stays routable, so the probe merely
    borrows another model for the rest of the pass instead of dropping it.
    """
    exclude = exclude or frozenset()
    if free_models:
        # Prefer a free, non-reasoning model: it answers the 5-token probe
        # immediately and is the least likely to burn the probe's timeout
        # thinking. Reasoning models are skipped *when* a non-reasoning free
        # model exists.
        for lm in free_models:
            if getattr(lm, "reasoning", False):
                continue
            mid = getattr(lm, "id", None)
            if mid and mid not in exclude:
                return mid
        # No non-reasoning free model -- use a reasoning one in catalog order
        # before the pin. A live catalog model is one OpenCode is actually
        # serving; the pin may be gated, which is exactly the "every lane
        # probe_error" trap convergence exists to escape. The probe's per-lane
        # watchdog bounds a model that thinks before its first token.
        for lm in free_models:
            mid = getattr(lm, "id", None)
            if mid and mid not in exclude:
                return mid
    return config.PROBE_MODEL


def _probe_timeout_for(
    model_id: str, catalog: Optional[Any], base: float,
) -> float:
    """Per-lane probe timeout, extended for a reasoning probe model.

    A model that thinks before its first token can stretch the trivial "hi"
    probe past ``PROBE_TIMEOUT``, which reads as a "dead" lane and churns the
    healers (regenerating identities) for lanes that were merely thinking -- the
    same false-dead churn the 4xx-as-probe_error guard exists to prevent. When
    the probe is on a reasoning model (per the live catalog, or
    ``LONG_THINKING_MODELS``) and the extended budget is larger than the base,
    use the extended budget. ``PROBE_REASONING_TIMEOUT`` of 0 / <= base disables
    the extension. The per-lane watchdog cap scales with the timeout, so a
    wedged lane is only slower-to-cut while the probe is on a reasoning model.
    """
    extended = config.PROBE_REASONING_TIMEOUT
    if not extended or extended <= base:
        return base
    reasoning = model_id in config.LONG_THINKING_MODELS
    if not reasoning and catalog is not None:
        lm = catalog.by_id(model_id)
        reasoning = lm is not None and bool(getattr(lm, "reasoning", False))
    return extended if reasoning else base


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

def _sweep_pool(
    proxy_pool: Any,
    model: str,
    base_url: str,
    timeout: float,
    log: Callable[..., Any],
) -> ProbeSummary:
    """One parallel pass of the pool with one fixed model. The body of the old
    ``probe_all``; ``probe_all`` now wraps this with probe-model convergence so a
    model OpenCode rejects (advertised but not served) is retried with a
    different free model rather than 400-ing every lane."""
    proxies = proxy_pool.get_all_proxies()
    if not proxies:
        summary = ProbeSummary(total=0, model=model, completed_at=time.time())
        _store(summary)
        return summary

    workers = max(1, min(len(proxies), max(1, config.PROBE_CONCURRENCY)))
    # Absolute worst case for one lane: liveness pre-check + trace fetch +
    # upstream pre-check + the model request, plus scheduling slack.
    cap = (
        timeout + config.PROBE_SOCKS_TIMEOUT
        + config.PROBE_TRACE_TIMEOUT + config.PROBE_CAP_SLACK
    )
    log(
        "probe: poking %d exits with the canary %s (%d at a time) ...",
        len(proxies), model, workers,
    )
    started = time.time()
    results: List[ProbeResult] = []

    ex = futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ll-probe")
    try:
        pending = [
            (px, ex.submit(_probe_single, px.url, px.id, model, base_url, timeout))
            for px in proxies
        ]
        for px, fut in pending:
            try:
                r = fut.result(timeout=cap)
            except futures.TimeoutError:
                r = ProbeResult(
                    proxy_id=px.id, status="dead",
                    error=f"no answer in {cap:.0f}s (lane wedged)",
                    probed_at=time.time(),
                )
                log(
                    "probe: %s -- no answer in %.0fs, cutting it off; "
                    "the health cycle will restart its tunnel",
                    px.id, cap,
                )
            except Exception as exc:  # noqa: BLE001
                r = ProbeResult(
                    proxy_id=px.id, status="dead",
                    error=f"{type(exc).__name__}: {str(exc)[:150]}",
                    probed_at=time.time(),
                )
            results.append(r)
            if config.WARP_VERBOSE:
                if r.status == "ok":
                    log("probe: %s -- healthy (%.0fms)", px.id, r.latency_ms)
                elif r.status == "rate_limited":
                    log("probe: %s -- rate-limited", px.id)
                elif r.status == "probe_error":
                    # Aligns with the summary line, which counts this as probe-error
                    # not dead. Logging it as "dead" made the per-lane lines and the
                    # aggregate disagree (every lane "dead" while summary said 10
                    # probe-error) -- and a dead tunnel regenerates an identity,
                    # while a probe_error is left alone.
                    log("probe: %s -- probe-error (%s)", px.id, r.error)
                else:
                    log("probe: %s -- dead (%s)", px.id, r.error)
    finally:
        # wait=False: a wedged worker must not hold the whole pass (or server
        # shutdown) hostage. cancel_futures drops lanes that never started.
        ex.shutdown(wait=False, cancel_futures=True)

    elapsed = (time.time() - started) * 1000.0
    summary = ProbeSummary(
        total=len(results),
        healthy=sum(1 for r in results if r.status == "ok"),
        rate_limited=sum(1 for r in results if r.status == "rate_limited"),
        dead=sum(1 for r in results if r.status == "dead"),
        probe_error=sum(1 for r in results if r.status == "probe_error"),
        duration_ms=round(elapsed, 1),
        model=model,
        results=results,
        completed_at=time.time(),
    )
    _store(summary)

    log(
        "probe: done — %d/%d healthy, %d rate-limited, %d dead, %d told-us-no (%.0fms)",
        summary.healthy, summary.total, summary.rate_limited,
        summary.dead, summary.probe_error, summary.duration_ms,
    )
    return summary


def probe_all(
    proxy_pool: Any,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: Optional[float] = None,
    log: Callable[..., Any] = lambda *a, **k: None,
    catalog: Optional[Any] = None,
    max_model_attempts: int = 4,
) -> ProbeSummary:
    """Probe every proxy in the pool with a real model request.

    Lanes run in parallel (``config.PROBE_CONCURRENCY`` at a time) under a
    per-lane watchdog deadline (see :func:`_sweep_pool`). The original sequential
    loop let one lane that was slow to answer stretch a pass to minutes — or,
    before the raw SOCKS5 pre-check existed, park forever inside httpx's
    un-timed handshake. A lane the watchdog cuts off is reported dead so the
    healers restart its tunnel; killing that tunnel's process also unblocks the
    parked worker's socket, so no thread is stranded for good.

    Probe-model convergence: OpenCode keeps advertising free models it is
    temporarily not serving (the `/models` list says ``deepseek-v4-flash-free``
    is free, but using it answers ``400: ... Model is unavailable`` under heavy
    load). A probe pinned to such a model therefore reports *every* lane as
    ``probe_error`` and the pool looks dead while the egress is fine — exactly
    the "all probe?" symptom. When a pass comes back ``probe_error`` on every
    lane (zero healthy, zero rate-limited, zero dead), the model -- not the
    lanes -- is the problem. The model is NOT retired here: a ``-free`` model
    that 400s while still listed is transient upstream overload, not removal —
    OpenCode pulls a genuinely-dropped model from the free section outright, and
    retiring a still-listed one would hide it from routing/dispatcher for the
    full TTL just because the upstream was busy. The probe just borrows the next
    free non-reasoning model for this pass so lane health is still reported;
    genuine removal is detected separately per-request (see
    ``_flag_model_retired`` in app.py, on a fresh catalog refresh that no longer
    lists the model). Bounded by ``max_model_attempts`` so a long outage cannot
    loop. Requires a ``catalog``; without one the pin is swept once (the legacy
    behaviour).

    Runs synchronously (call from a background thread at startup).
    Returns a ProbeSummary and stores it module-level for /api/warp/probe.
    """
    base_url = (base_url or config.OPENCODE_BASE_URL).rstrip("/")
    base_timeout = timeout or config.PROBE_TIMEOUT

    # Serialize the resolve+sweep across callers. The WARP startup probe and
    # the Tor-join snapshot probe run concurrently in their own bootstrap
    # threads; running them serially keeps the per-lane verdicts and the stored
    # summary consistent (no interleaved sweeps, no burst of simultaneous probe
    # traffic hitting OpenCode) and lets the second probe reuse any progress
    # the first made. The dashboard's Probe button and the daemon's periodic
    # probe also come through here, so nothing overlaps a sweep.
    with _probe_lock:
        # Resolve the first model: an explicit pin wins, else the catalog's
        # first free non-reasoning model, else the configured pin.
        chosen = model if model is not None else resolve_probe_model(
            catalog.free() if catalog is not None else None
        )

        tried: set = set()
        summary: Optional[ProbeSummary] = None
        for _ in range(max(1, max_model_attempts)):
            if not chosen:
                break
            # A caller may hand us a model that became unavailable since it
            # resolved it (seeded, or genuinely removed by a concurrent request
            # via _flag_model_retired). resolve_probe_model already excludes
            # unavailable ids, so this normally advances at most once; failing
            # that there is nothing sweepable left, so stop without 400-ing the
            # whole pool again.
            if catalog is not None and catalog.is_unavailable(chosen):
                nxt = resolve_probe_model(catalog.free(), exclude=tried)
                if not nxt or catalog.is_unavailable(nxt):
                    break
                chosen = nxt
                continue
            sweep_timeout = _probe_timeout_for(chosen, catalog, base_timeout)
            summary = _sweep_pool(proxy_pool, chosen, base_url, sweep_timeout, log)
            # Retry with a different model ONLY when the chosen model was
            # rejected on every lane: every probe_error, and nothing healthy,
            # rate-limited, or dead. Any healthy/rate-limited lane means the
            # model WAS served; any dead lane means the pool is genuinely mixed
            # and a model swap would mask it. This signature is "the egress is
            # up, the model is gated".
            converged = (
                catalog is not None
                and summary.total > 0
                and summary.healthy == 0
                and summary.rate_limited == 0
                and summary.dead == 0
                and summary.probe_error == summary.total
            )
            if not converged:
                break
            # A "-free" model rejected on every lane while *still listed* is
            # transient overload on OpenCode's side (huge traffic), not removal:
            # when OpenCode actually drops a model it pulls it from the free
            # section outright. The model is NOT retired here -- retiring it
            # would hide a still-offered model from routing and the dispatcher
            # for the full TTL (7 days) just because the upstream was busy. The
            # probe just borrows a different free model for this pass so lane
            # health is still reported; the busy model stays routable. Genuine
            # removal is detected separately per-request (see _flag_model_retired
            # in app.py), on a fresh catalog refresh that no longer lists it.
            log(
                "probe: %s rejected on every lane (%d probe-error); re-probing "
                "with another free model (not retiring -- still in the free "
                "section, likely transient overload)",
                chosen, summary.probe_error,
            )
            # Borrow another free model for the rest of this pass. ``tried``
            # skips the rejected id (and any earlier ones) so convergence
            # actually advances instead of re-picking the same rejected model
            # -- without dropping it from the catalog.
            tried.add(chosen)
            nxt = resolve_probe_model(catalog.free(), exclude=tried)
            if not nxt or nxt in tried or catalog.is_unavailable(nxt):
                # nothing else to borrow, or resolve fell back to a pin we
                # already tried / that is itself retired -- stop rather than
                # sweep another known-rejected model.
                break
            chosen = nxt

        if summary is None:
            # chosen was empty from the start (no model resolved and none
            # pinned): still produce a summary over the configured pin so
            # callers and the dashboard get a row rather than an exception.
            fallback = _probe_timeout_for(config.PROBE_MODEL, catalog, base_timeout)
            summary = _sweep_pool(
                proxy_pool, config.PROBE_MODEL, base_url, fallback, log,
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


def _recount(summary: ProbeSummary, old_status: str, new_status: str) -> None:
    """Move a summary counter after a healer rewrites a result.

    The counters are computed once at probe time; a heal flips a result's
    status in place, so the matching counter must move too or the aggregate
    panel (driven by the counters) would keep reporting pre-heal verdicts
    while the per-slot chips already show the healed state. Every verdict
    the per-result status can hold must move in both directions so the totals
    reconcile after any transition -- including a ``probe_error`` slot that a
    spread re-rolls onto a fresh exit, where the slot's status flips to
    ``ok`` but ``probe_error`` would otherwise stay wrongly counted.
    ``healed`` is the transient "rolled or regenerated but not yet
    re-confirmed" verdict; it lands in its own :attr:`ProbeSummary.healed`
    counter (NOT ``healthy``) so the panel totals
    ``healthy + rate_limited + dead + probe_error + healed == total`` even
    during the heal → next-probe window when the lane is not yet
    probe-confirmed.
    """
    if old_status == "ok":
        summary.healthy = max(0, summary.healthy - 1)
    elif old_status == "rate_limited":
        summary.rate_limited = max(0, summary.rate_limited - 1)
    elif old_status == "dead":
        summary.dead = max(0, summary.dead - 1)
    elif old_status == "probe_error":
        summary.probe_error = max(0, summary.probe_error - 1)
    elif old_status == "healed":
        summary.healed = max(0, summary.healed - 1)
    if new_status == "ok":
        summary.healthy += 1
    elif new_status == "rate_limited":
        summary.rate_limited += 1
    elif new_status == "dead":
        summary.dead += 1
    elif new_status == "probe_error":
        summary.probe_error += 1
    elif new_status == "healed":
        summary.healed += 1


def _mark_healed(summary: ProbeSummary, r: ProbeResult) -> None:
    """Flip a probe result to the transient ``healed`` state.

    The healer acted (identity regenerated, Tor lane restarted) but the new
    exit has not been verified yet. Rewriting the stored summary in place is
    what lets the dashboard reflect the heal immediately instead of holding
    the stale ``rate_limited``/``dead`` verdict until the next probe pass.
    """
    old = r.status
    r.status = "healed"
    r.exit_ip = ""
    r.latency_ms = 0.0
    r.error = ""
    r.probed_at = time.time()
    _recount(summary, old, "healed")


def heal_expired(
    proxy_pool: Any,
    summary: ProbeSummary,
    warp_manager: Any,
    log: Callable[..., Any] = lambda *a, **k: None,
) -> int:
    """Remove dead proxies and regenerate their identities.

    The expired-IP healer: removes identities whose WARP tunnel has expired
    or whose exit IP is unreachable, then regenerates a fresh identity so
    the slot is not permanently lost. A successful regeneration rewrites the
    probe result to ``healed`` (see ``_mark_healed``) so the dashboard drops
    the stale ``dead`` verdict right away.
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
        if config.DEFER_REROLL_WHEN_BUSY and active_streams.active(r.proxy_id) > 0:
            log("heal-expired: %s re-roll deferred -- stream in flight "
                "(re-checked next pass)", r.proxy_id)
            continue
        log("heal-expired: removing dead proxy %s (%s)", r.proxy_id, r.error)
        proxy_pool.remove(r.proxy_id)
        try:
            idx = _instance_index(r.proxy_id)
            inst = _find_instance(warp_manager, idx) if idx is not None else None
            if inst is not None and warp_manager.regenerate_instance(inst):
                healed += 1
                _mark_healed(summary, r)
                log("heal-expired: regenerated identity #%d", idx)
            elif inst is not None:
                # ``regenerate_instance`` returns False on a port-find /
                # registration / restart-after-regen failure (manager.py). The
                # identity never came back, so do NOT chip ``healed``: an
                # optimistic pill would hide a still-down lane from the operator
                # until a real request rediscovered the dead state at request
                # time. Leave the verdict ``dead`` so the next probe pass sees
                # the lane still needs work -- the pool already dropped it.
                log("heal-expired: regeneration failed for #%d -- lane stays dead", idx)
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

    Slots that cannot escape the burned IPs (or whose polite canary probe
    still comes back ``probe_error`` because OpenCode refused even the
    converged model on the new exit) are parked out of :meth:`pick` until the
    next periodic probe re-evaluates them, via ``proxy_pool.extend_cooldown``
    rather than ``mark_failure``. ``mark_failure`` would inflate the
    real-request burn counters that feed ``_dump_burned_identities`` -- a
    probe signal is the old observation, not a fresh request failure -- so the
    healer uses the no-attribution gate. Without it, ``pick`` returned the
    known-burned exit to a live request and the request rediscovered its 429
    at request time (the symptom of a request through a rate-limited lane).

    The per-roll verification probe uses ``summary.model`` -- the canary
    ``probe_all`` converged on and proved serving this pass -- not the stale
    ``config.PROBE_MODEL`` pin. OpenCode can pull the pin behind a gate while
    the catalog still serves another free model, and then every freshly
    re-rolled exit answered "400 Model is unavailable" -> ``probe_error``
    "leaving for the health cycle", so the heal never succeeded until the pin
    came back. Verifying with a serving canary re-rolls an exit to a
    *known-usable* address rather than a merely-different one.
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
    verify_model = getattr(summary, "model", "") or config.PROBE_MODEL
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
        if config.DEFER_REROLL_WHEN_BUSY and active_streams.active(r.proxy_id) > 0:
            log("heal-rate-limit: %s re-roll deferred -- stream in flight", r.proxy_id)
            # An in-flight stream may finish on the burned IP, but the *next*
            # request must not pile onto it; gate it until the next probe pass
            # re-evaluates rather than dropping a known-burned exit back to pick.
            proxy_pool.extend_cooldown(px, config.PROBE_INTERVAL_S)
            continue
        try:
            if _reroll_until_clean(
                proxy_pool, warp_manager, inst, px, r.proxy_id, burned, log,
                max_attempts=max_attempts, result=r, summary=summary,
                verify_model=verify_model,
            ):
                healed += 1
            else:
                # Could not get this slot onto an unburned, serving exit this
                # pass. Without gating, ``proxy_pool.pick`` would route the
                # next request through this known-burned IP and rediscover the
                # 429 at request time -- exactly the symptom of a request
                # through a rate-limited proxy. Park it out of selection until
                # the next periodic probe re-probes+re-heals it. A real-request
                # 429 (should it still get picked) still escalates
                # ``consecutive_failures`` via ``mark_failure`` on its own.
                proxy_pool.extend_cooldown(px, config.PROBE_INTERVAL_S)
        except Exception as exc:  # noqa: BLE001
            log("heal-rate-limit: re-roll failed for %s: %s", r.proxy_id, exc)
            proxy_pool.extend_cooldown(px, config.PROBE_INTERVAL_S)
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
    result: Optional[ProbeResult] = None,
    summary: Optional[ProbeSummary] = None,
    verify_model: Optional[str] = None,
) -> bool:
    """Re-establish one tunnel until its exit IP is unburned; verify for real.

    Rolls are aimed: the learned edge->exit map is consulted first (edges that
    recently reached a free exit), with unmapped edges kept for exploration.
    Every observed (endpoint, exit) pair feeds the map back, so each roll
    makes the next one smarter even when it fails.

    Returns True once a real model request through the new exit succeeds.
    A newly reached IP that turns out to be limited too joins the burned set,
    so no later slot wastes rolls landing on it.

    When ``result`` is given, a successful verify rewrites it in place to the
    transient ``status="healed"`` (not ``ok``) with the new exit IP and
    latency. The dashboard's per-slot chip has a warn-orange "healed" pill
    (tooltip "exit refreshed — next probe verifies it") that distinguishes a
    lane freshly rolled off a burned exit from one that was always healthy;
    bypassing it (writing straight to ``ok``) was what made the heal look
    invisible — a graduated lane snapped from red to green with no tag, as
    if nothing had happened. The fresh exit IP + measured latency stay
    (unlike :func:`_mark_healed` for the regeneration / Tor-restart paths,
    which cannot observe the new exit yet), because this re-roll was
    canary-verified end-to-end. The next periodic probe pass rewrites the
    whole summary, so the lane flips to ``ok`` once it survives a probe on
    its own. ``healed`` lands in :attr:`ProbeSummary.healed`, not
    ``healthy``, so the panel totals reconcile.

    ``verify_model`` pins the per-roll verification probe to the canary
    ``probe_all`` proved serving this pass (see :func:`heal_rate_limited`),
    not the configured ``PROBE_MODEL`` pin. OpenCode can pull the pin behind a
    gate while the catalog still serves another free model, leaving every
    freshly re-rolled exit reading ``probe_error`` and the slot "left for the
    health cycle" until the pin comes back -- so the heal never succeeded.
    Falling back to ``config.PROBE_MODEL`` when ``summary.model`` is empty
    keeps the legacy behaviour for hand-built / direct callers.
    """
    from warp import egress_map

    max_attempts = max_attempts or config.WARP_REROLL_MAX_ATTEMPTS
    verify_model = verify_model or config.PROBE_MODEL
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
            if config.WARP_VERBOSE:
                log(
                    "heal-rate-limit: #%d rolled onto %s — re-rolling",
                    inst.index, ip or "an unknown exit",
                )
            continue
        check = probe_proxy(px.url, proxy_id, model=verify_model)
        if check.status == "ok":
            proxy_pool.mark_success(px)
            proxy_pool.reset_counters(px.id)
            log(
                "heal-rate-limit: #%d now exits via %s (endpoint %s, %.0fms) — clean",
                inst.index, ip, pinned, check.latency_ms,
            )
            if result is not None:
                old = result.status
                # ``healed`` (not ``ok``) so the dashboard shows the warn-orange
                # "healed" pill — the operator-visible signal a lane was freshly
                # rolled off a burned exit. The fresh exit IP + measured latency
                # stay (this re-roll is canary-verified, not the unobservable
                # regeneration / Tor-restart paths routed through _mark_healed).
                # Next periodic probe overwrites the whole summary, so the lane
                # graduates to ``ok`` if it survives a probe on its own.
                result.status = "healed"
                result.exit_ip = ip
                result.latency_ms = check.latency_ms
                result.error = ""
                result.probed_at = check.probed_at
                if summary is not None:
                    _recount(summary, old, "healed")
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


def rotate_burned_tor_lanes(
    proxy_pool: Any,
    tor_manager: Any,
    summary: ProbeSummary,
    log: Callable[..., Any] = lambda *a, **k: None,
) -> int:
    """Restart Tor lanes whose exit the probe found rate-limited or dead.

    A tor restart re-picks the route (guard/middle/exit), so the lane comes
    back on a fresh exit IP within seconds -- the Tor equivalent of a WARP
    tunnel re-roll, and just as free. This is the only healer that touches
    ``tor-*`` lanes: ``heal_expired`` and ``heal_rate_limited`` deliberately
    skip non-WARP lanes (an identity re-roll is a WARP-only concept), so
    without this call Tor lanes the startup probe found burned stay burned
    until the daemon's first periodic probe runs ``PROBE_INTERVAL_S`` later.

    Returns 0 silently when ``tor_manager`` is None or carries no instances,
    so startup, the daemon and the API can all invoke it unconditionally
    regardless of whether Tor is enabled -- the call site does not need its
    own ``LINGLING_TOR_ENABLED`` branch.

    A successful restart rewrites the probe result to ``healed`` so the
    dashboard stops showing the lane as rate-limited/dead before the next
    probe verifies its fresh exit.
    """
    if tor_manager is None or not getattr(tor_manager, "instances", None):
        return 0
    by_index = {i.index: i for i in tor_manager.instances}
    rotated = 0
    for r in summary.results:
        if not r.proxy_id.startswith("tor-"):
            continue
        if r.status not in ("rate_limited", "dead"):
            continue
        try:
            idx = int(r.proxy_id.split("-", 1)[1])
        except (ValueError, IndexError):
            continue
        inst = by_index.get(idx)
        if inst is None:
            continue
        if config.DEFER_REROLL_WHEN_BUSY and active_streams.active(r.proxy_id) > 0:
            log("[tor] lane #%d re-roll deferred -- stream in flight", idx)
            continue
        log("[tor] lane #%d is %s -- rotating its exit", idx, r.status)
        px = proxy_pool.get_by_id(r.proxy_id)
        try:
            if tor_manager.restart_instance(inst, log=log):
                proxy_pool.reset_counters(r.proxy_id)
                _mark_healed(summary, r)
                rotated += 1
            elif px is not None:
                # The Tor restart failed -- the lane stays on the same burned
                # exit but ``pick()`` would still route through it, letting a
                # real request re-429 before request-time mark_failure could
                # cool it. Park the lane until the next probe pass re-checks
                # the exit; extend_cooldown (the probe-side mirror of
                # mark_failure) raises cooldown_until without bumping the
                # failure tally -- a Tor-restart verdict is not a request burn.
                proxy_pool.extend_cooldown(px, config.PROBE_INTERVAL_S)
                log("[tor] lane #%d restart failed -- parked for next probe", idx)
        except Exception as exc:  # noqa: BLE001
            log("[tor] rotate failed for #%d: %s", idx, exc)
    return rotated


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

    by_id = {res.proxy_id: res for res in summary.results}
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

    # Spread's verify probe uses the same serving canary probe_all converged
    # on, so a pulled pin cannot make a would-be move read ``probe_error`` and
    # skip -- the slot stays where it is while a fresh exit it could have lived
    # on is silently refused. Match the heal path's canary.
    verify_model = getattr(summary, "model", "") or config.PROBE_MODEL
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
            if config.DEFER_REROLL_WHEN_BUSY and active_streams.active(proxy_id) > 0:
                log("spread: %s re-roll deferred -- stream in flight", proxy_id)
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
                if config.WARP_VERBOSE:
                    log(
                        "spread: %s rolled onto %s (aimed at %s) — next pass retries",
                        proxy_id, got or "an unknown exit", target,
                    )
                continue
            check = probe_proxy(px.url, proxy_id, model=verify_model)
            if check.status == "ok":
                proxy_pool.mark_success(px)
                proxy_pool.reset_counters(proxy_id)
                moved += 1
                log("spread: %s -> %s (via %s) — locked in", proxy_id, got, pinned)
                moved_result = by_id.get(proxy_id)
                if moved_result is not None:
                    old = moved_result.status
                    moved_result.status = "ok"
                    moved_result.exit_ip = got
                    moved_result.latency_ms = check.latency_ms
                    moved_result.error = ""
                    moved_result.probed_at = check.probed_at
                    _recount(summary, old, "ok")
            else:
                log("spread: %s reached %s but the probe said %s",
                    proxy_id, got, check.status)
                # The slot moved onto an exit the verify probe found burned. A
                # real request would route through this lane and rediscover the
                # 429 at request time before mark_failure cooled it -- so park
                # the lane until the next probe pass re-evaluates the exit.
                # extend_cooldown (the probe-side mirror of mark_failure) gates
                # pick() without inflating the failure tally: a probe verdict on
                # a freshly-rolled exit is an observation, not a request burn.
                px_moved = proxy_pool.get_by_id(proxy_id)
                if px_moved is not None:
                    proxy_pool.extend_cooldown(px_moved, config.PROBE_INTERVAL_S)
                if check.status == "rate_limited" and got:
                    burned.add(got)
                moved_result = by_id.get(proxy_id)
                if moved_result is not None:
                    old = moved_result.status
                    moved_result.status = check.status
                    moved_result.exit_ip = got or moved_result.exit_ip
                    moved_result.latency_ms = check.latency_ms
                    moved_result.error = check.error
                    moved_result.probed_at = check.probed_at
                    _recount(summary, old, check.status)
    return moved
