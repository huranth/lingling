"""Lingling gateway -- a dumb routing proxy for free AI models.

Takes whatever an OpenAI-compatible client (Cline, Claude Code, Codex) sends and
routes it to free models on OpenCode, defeating the per-IP free-tier limit via a
rotating pool of egress proxies (Cloudflare WARP by default). The client owns
all prompting; Lingling only routes. See the README for the endpoint list.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import threading
import shutil
import time
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from core import config
from core import auth
from routing import dispatcher
from routing import effort
from routing import executor
from core import api_keys
from routing import parking
from routing import stream_guard
from routing import stream_idle
from routing import pacing_memory
from routing import sampler
from routing import responses_bridge
from claudecode import messages_bridge
from claudecode import messages_response
from claudecode import messages_stream
from claudecode import model_map
from claudecode import thinking as cc_thinking
from models import vision_bridge
from models.catalog import UnifiedCatalog
from providers import registry
from providers.base import extract_assistant_text, extract_usage
from usage.store import UsageStore
from warp.manager import WarpManager, _port_is_open
from warp import health as warp_health
from warp import formation as warp_formation
from warp import probe as warp_probe

VERSION = "0.2.0"
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(
    title="Lingling",
    version=VERSION,
    description="Dumb routing proxy for free AI models (OpenCode + IP rotation)",
)
# CORS is deliberately narrow: with allow_origins=["*"] any internet page could
# read /api/keys or fire POST /api/warp/refresh from a visitor's browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=auth.allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "x-api-key"],
)
log = logging.getLogger("uvicorn.error")

# Routes reachable with no credential. Everything else under /api/ and /v1/ is
# gated below. Compared after stripping a trailing slash, because this
# middleware runs before FastAPI's redirect_slashes -- so `/api/health/` from a
# monitoring probe is recognised as open rather than answered 401.
_OPEN_PATHS = frozenset({
    "/", "/api/health", "/v1", "/v1/models", "/docs", "/openapi.json", "/redoc",
    # Claude Code HEADs this on startup; gating it logged a scary auth-reject
    # line for a harmless liveness check.
    "/api/hello",
})


@app.middleware("http")
async def _gate(request: Request, call_next):
    """Require a session cookie or API key for every /api/ and /v1/ route.

    Previously only /v1/chat/completions was gated, leaving DELETE /api/usage,
    DELETE /api/keys/{id} and POST /api/warp/refresh open to any local process
    or LAN host -- too large a blast radius (wipe the ledger, revoke keys,
    destroy WARP identities) to leave ungated.
    """
    path = request.url.path
    guarded = path.startswith("/api/") or path.startswith("/v1/")
    normalized = path.rstrip("/") or "/"
    if request.method == "OPTIONS" or not guarded or normalized in _OPEN_PATHS:
        return await call_next(request)
    ok, actor = auth.identify(request)
    if not ok and config.REQUIRE_API_KEY:
        log.warning(
            "auth reject method=%s path=%s client=%s",
            request.method, path, request.client.host if request.client else "?",
        )
        return JSONResponse(
            {
                "detail": (
                    "Unauthorised. Browser clients: load the dashboard at / to obtain "
                    "a session. API clients: send 'Authorization: Bearer ll_...'."
                )
            },
            status_code=401,
        )
    # Downstream handlers can read the resolved actor for logging.
    request.state.actor = actor if ok else "open"
    return await call_next(request)

def _startup_sync_warp(*, start_daemon: bool = True) -> None:
    """Sync already-running WARP proxies into the pool, then start the daemon.

    The wireproxy processes persist across Lingling restarts, so rather than
    re-running start_all() we sync their URLs straight into the pool and let the
    health daemon take over. Daemon start is deferred to _bootstrap_warp when
    ``LINGLING_BOOTSTRAP_WARP=1`` so its first cycle doesn't race registration.
    """
    try:
        added = _sync_warp_to_pool()
        if added:
            log.info("startup: %d WARP proxies were already up — synced them into the pool", added)
        elif warp_manager.status()["proxies_running"] > 0:
            log.info("startup: %d WARP proxies already lounging in the pool", warp_manager.status()["proxies_running"])
        else:
            log.info("startup: no running WARP proxies to sync — we'll bootstrap some")
    except Exception as exc:  # noqa: BLE001
        log.warning("startup: WARP sync skipped (%s)", exc)

    # Start the background health daemon after initial sync (unless the caller
    # deferred it to _bootstrap_warp to avoid racing with identity registration).
    if start_daemon:
        try:
            warp_health_daemon.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("startup: WARP health daemon failed to start (%s)", exc)

    # Trim the request log. Without this it grows for the life of the install.
    try:
        removed = usage_store.prune(config.USAGE_RETENTION_DAYS)
        if removed:
            log.info(
                "startup: pruned %d usage rows older than %d days",
                removed, config.USAGE_RETENTION_DAYS,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("startup: usage prune skipped (%s)", exc)


def _bootstrap_tor() -> None:
    """Download Tor once and start the zero-account egress lanes.

    Off the event loop on purpose: the first download is ~25 MB and the first
    instance's directory fetch can take a minute; everything after that is
    seconds. Lanes join the proxy pool like any other SOCKS proxy, so the
    executor, probe and cooldown machinery need no changes to use them.
    """
    try:
        if not tor_manager.ensure_tools(log=log.info):
            return
        result = tor_manager.start_all(log=log.info)
        added = tor_manager.sync_to_pool(proxy_pool, log=log.info)
        log.info(
            "bootstrap: Tor lanes up — %d started, %d failed, %d in pool",
            result.get("started", 0), result.get("failed", 0), added,
        )
        # Tor lanes joined the pool *after* the WARP startup probe snapshotted
        # it, so that cached ProbeSummary says "10 lanes" while the live pool
        # now holds 13 -- and the dashboard showed both at once ("across 10
        # slots" beside a 13/13 gauge). Re-probe the now-full pool here so the
        # cached snapshot catches up, and rotate any Tor lane the probe finds
        # burned (its healer is a restart, which re-picks a fresh exit route)
        # rather than waiting PROBE_INTERVAL_S for the daemon's first tick.
        if added > 0 and config.PROBE_ON_STARTUP and len(proxy_pool) > 0:
            try:
                # probe_all resolves+converges on a model the catalog still
                # serves, so the refreshed snapshot reflects the full pool with a
                # model that actually answers instead of 400-ing every Tor lane.
                tor_summary = warp_probe.probe_all(
                    proxy_pool, catalog=catalog, log=log.info,
                )
                tor_rotated = warp_probe.rotate_burned_tor_lanes(
                    proxy_pool, tor_manager, tor_summary, log=log.info,
                )
                if tor_rotated:
                    log.info(
                        "probe: Tor join refreshed snapshot to %d lanes, "
                        "rotated %d burned Tor lanes",
                        tor_summary.total, tor_rotated,
                    )
                # Re-run the sampler over the now-full pool so Tor lanes feed
                # ok_exits() right away. Without this, ok_exits() for every
                # sampled model still reflects the WARP-only pool the
                # WARP-startup sampler snapshotted before Tor bootstrapped, so a
                # model burned on WARP but served by a fresh Tor lane reads
                # "cooked" (empty ok_set -> fail-fast on WARP) for up to the
                # SAMPLER_INTERVAL_S it takes the daemon's first sampler cycle to
                # catch up. The sampler shares ``tor_summary`` (the full-pool
                # canary) so the canary is not re-probed.
                if config.SAMPLER_ENABLED:
                    try:
                        sampler.sample_models(
                            proxy_pool, catalog, tor_summary, log=log.info,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning("sampler: post-Tor pass failed (%s)", exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("probe: Tor-join snapshot refresh failed (%s)", exc)
    except Exception as exc:  # noqa: BLE001
        # A failed Tor bootstrap must not affect the gateway or the WARP pool.
        log.warning("bootstrap: Tor setup failed (%s) - continuing without it", exc)


def _bootstrap_warp() -> None:
    """Register and start WARP identities in-process (LINGLING_BOOTSTRAP_WARP=1).

    Done here rather than over HTTP because /api/warp/setup and /start are
    authenticated -- start.bat would otherwise need a credential to bootstrap
    the server it just launched.
    """
    try:
        status = warp_manager.status()
        if status["identities_registered"] < warp_manager.count:
            log.info("bootstrap: minting WARP identities (one-time, slow)")
            warp_manager.setup_identities(log=log.info)
        _start_warp_at_startup()
        log.info(
            "bootstrap: WARP up — %d exits wrangled. opencode's free tier did not pre-game for this.",
            proxy_pool.status().get("total", 0),
        )
        # --- startup probe: test every proxy with a real model request ---
        if config.PROBE_ON_STARTUP and len(proxy_pool) > 0:
            try:
                # probe_all resolves the target from the live catalog (not the
                # hardcoded pin, which 400-s when OpenCode gates it behind a key)
                # and converges: a model rejected on every lane is retried with
                # another free model, so a stale-but-advertised id no longer
                # makes the whole pool read probe_error. (It is NOT retired --
                # "unavailable" while still listed is transient overload, not
                # removal.)
                summary = warp_probe.probe_all(
                    proxy_pool, catalog=catalog, log=log.info,
                )
                # Heal dead (expired) and rate-limited proxies.
                expired_healed = warp_probe.heal_expired(
                    proxy_pool, summary, warp_manager, log=log.info,
                )
                rl_healed = warp_probe.heal_rate_limited(
                    proxy_pool, summary, warp_manager, log=log.info,
                )
                # Tor lanes have no identity to re-roll -- their healer is a
                # restart, which re-picks guard/middle/exit for a fresh exit
                # IP. Called here, not from heal_rate_limited (which skips
                # non-WARP lanes by design), so a burned Tor lane does not
                # sit unhealed until the daemon's first periodic probe runs
                # PROBE_INTERVAL_S seconds later.
                tor_rotated = warp_probe.rotate_burned_tor_lanes(
                    proxy_pool, tor_manager, summary, log=log.info,
                )
                spread_moved = warp_probe.spread_distinct_exits(
                    proxy_pool, summary, warp_manager, log=log.info,
                )
                if expired_healed or rl_healed or tor_rotated or spread_moved:
                    log.info(
                        "probe: fixed up — healed %d expired identities, "
                        "%d burned exits, rotated %d Tor lanes, spread %d slots onto unused exits",
                        expired_healed, rl_healed, tor_rotated, spread_moved,
                    )
                # --- exit-lane formation: assemble distinct exits on purpose ---
                if config.WARP_FORM_ON_STARTUP and len(proxy_pool) > 0:
                    try:
                        formed = warp_formation.form_distinct_exits(
                            proxy_pool, warp_manager, log=log.info,
                        )
                        if formed.get("distinct"):
                            usage_store.log(
                                "lane-formation", "lane-formation", "probe",
                                reason=(
                                    f"{formed['distinct']} distinct exits across "
                                    f"{formed['slots']} slots "
                                    f"({formed['rolls']} rolls, {formed['elapsed_s']:.0f}s)"
                                ),
                                status="ok",
                                provider="warp",
                            )
                    except Exception as exc:  # noqa: BLE001
                        log.warning("formation: startup formation failed (%s)", exc)
                # --- post-heal multi-model sampler ---
                # The heal's canary summary is the "which exits are alive"
                # baseline; hand it to the sampler so per-(model, exit) health
                # (and the request path's per-model routing / fail-fast) is
                # populated immediately after the pool is fixed up, without
                # re-probing the canary. Enabled/configured via LINGLING_SAMPLER_*.
                if config.SAMPLER_ENABLED:
                    try:
                        sampler.sample_models(
                            proxy_pool, catalog, summary, log=log.info,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning("sampler: startup pass failed (%s)", exc)
                # Log a single summary row so the dashboard shows one entry.
                try:
                    usage_store.log(
                        "startup-probe", "startup-probe", "probe",
                        reason=(
                            f"{summary.healthy}/{summary.total} healthy, "
                            f"{summary.rate_limited} rate-limited, "
                            f"{summary.dead} dead"
                            + (f", {summary.probe_error} probe-error" if summary.probe_error else "")
                        ),
                        # probe_error lanes reached OpenCode but were answered
                        # with a model-rejecting 4xx: the egress layer is up,
                        # so they are not "exhausted" -- the probe model just
                        # could not verify them.
                        status="ok" if (
                            summary.healthy > 0
                            or (summary.probe_error > 0 and summary.dead == 0)
                        ) else "exhausted",
                        provider="warp",
                    )
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                log.warning("probe: startup probe failed (%s)", exc)
    except Exception as exc:  # noqa: BLE001
        # A failed bootstrap must not stop the gateway: it still routes fine
        # directly, just without IP rotation.
        log.warning("bootstrap: WARP setup failed (%s) - continuing without it", exc)
    finally:
        # Start the health daemon *after* bootstrap so its first cycle doesn't
        # race with identity registration and pool sync.
        try:
            warp_health_daemon.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("bootstrap: health daemon start failed (%s)", exc)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup/shutdown for the gateway (replaces the @app.on_event hooks)."""
    # Raise the sync-worker thread ceiling. Every streamed request holds one
    # thread for the life of the stream, so anyio's ~40 default is the real
    # concurrency limit; the work is I/O-bound, so the extra threads are cheap.
    try:
        import anyio
        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = config.THREADPOOL_MAX
        log.info("threadpool: lifted the sync executor ceiling to %d", config.THREADPOOL_MAX)
    except Exception as exc:  # noqa: BLE001
        log.warning("threadpool: could not raise limiter (%s)", exc)

    # Prune idle pooled httpx connections in the background so a long-idle
    # gateway does not keep dozens of sockets open forever.
    from providers.connection_pool import get_connection_pool
    conn_pool = get_connection_pool()
    _pool_prune_stop = threading.Event()

    def _prune_pool_loop() -> None:
        while not _pool_prune_stop.wait(config.CONNECTION_POOL_IDLE_S / 2):
            try:
                conn_pool.prune_idle(config.CONNECTION_POOL_IDLE_S)
            except Exception:  # noqa: BLE001
                pass

    _prune_thread = threading.Thread(
        target=_prune_pool_loop, name="conn-pool-prune", daemon=True,
    )
    _prune_thread.start()

    _startup_sync_warp(start_daemon=not config.BOOTSTRAP_WARP)
    if config.BOOTSTRAP_WARP:
        # Registration can take a minute; run it off the event loop so the
        # server starts answering /api/health immediately.
        threading.Thread(target=_bootstrap_warp, name="warp-bootstrap", daemon=True).start()
    if config.TOR_ENABLED and tor_manager.count > 0:
        # Tor lanes bootstrap independently of WARP: first run downloads the
        # bundle (~25 MB) and fetches the directory; later boots are seconds.
        threading.Thread(target=_bootstrap_tor, name="tor-bootstrap", daemon=True).start()
    log.info("LINGLING UP — opencode's free tier is about to have a day.")
    try:
        yield
    finally:
        _pool_prune_stop.set()
        try:
            conn_pool.shutdown()
        except Exception as exc:  # noqa: BLE001
            log.warning("shutdown: connection pool close failed (%s)", exc)
        try:
            warp_health_daemon.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("shutdown: health daemon stop failed (%s)", exc)
        try:
            # Stop the WARP proxies on the way out. On Windows the kill-on-close
            # job already takes them with this process no matter how it dies;
            # this is the polite path (and the only one on POSIX), so a closed
            # gateway never leaves a port busy.
            warp_manager.stop_all()
        except Exception as exc:  # noqa: BLE001
            log.warning("shutdown: warp stop failed (%s)", exc)
        try:
            tor_manager.stop_all()
        except Exception as exc:  # noqa: BLE001
            log.warning("shutdown: tor stop failed (%s)", exc)
        try:
            usage_store.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("shutdown: usage store close failed (%s)", exc)


app.router.lifespan_context = lifespan


# Request fields Lingling handles itself. Everything else is forwarded to
# the upstream provider unchanged. OpenCode already manages streaming,
# reasoning, and BPE decoding; we do not rewrite its output.
#
# The executor's own parameter names are excluded too. `**params` is splatted
# into `run_in_threadpool(executor.execute_*, ...)`, so a body carrying any of
# these collided with the positional or keyword argument of the same name and
# raised TypeError -> HTTP 500. `timeout` is the realistic one: a plausible
# field for a client to send, and it killed the streaming path while the
# non-streaming path survived (different call shape).
_PASSTHROUGH_EXCLUDE = frozenset({
    "model", "messages", "stream", "lingling", "session_id", "lingling_recover",
    # executor.execute_nonstream / execute_stream signature
    "model_id", "providers", "proxy_pool", "timeout",
    # execute_stream-only: per-attempt lifecycle callback (see _log_stream_attempt)
    "on_attempt",
    # provider stream_chat / chat_completions signature (bound explicitly by the
    # executor, so a client-supplied value would collide and raise TypeError)
    "proxy_url", "proxy_id", "secret", "self",
    # starlette.concurrency.run_in_threadpool signature
    "func",
})
def _passthrough_params(body: Dict[str, Any]) -> Dict[str, Any]:
    """Forward every OpenAI-compatible request parameter except managed fields."""
    return {k: v for k, v in body.items() if k not in _PASSTHROUGH_EXCLUDE}


def _header_safe(value: str) -> str:
    """Make a string safe as an HTTP header value.

    Starlette encodes headers as latin-1, so any character above U+00FF raises.
    The dispatcher's ``reason`` is model-generated and routinely has em-dashes or
    smart quotes, so this is a normal path. Only the X-Lingling-* mirror is
    sanitised; the full reason still reaches the client in the body.
    """
    if not value:
        return ""
    for uni, ascii_ in (
        ("\u2014", "--"), ("\u2013", "-"), ("\u2018", "'"), ("\u2019", "'"),
        ("\u201c", '"'), ("\u201d", '"'), ("\u2026", "..."), ("\u2022", "*"),
    ):
        value = value.replace(uni, ascii_)
    # Control chars must go before the latin-1 pass: CR/LF/NUL are valid latin-1
    # and survive it, and Starlette then rejects the header with RuntimeError,
    # aborting the whole response.
    value = "".join(" " if ch in "\r\n\t" else ch for ch in value if ch >= " ")
    return value.encode("latin-1", "replace").decode("latin-1")


def _failure_provenance_headers(
    target: str, routed_by: str, reason: str,
) -> Dict[str, str]:
    """The X-Lingling-Routed-* provenance a success carries, for an exhausted 503.

    An operator already sees ``X-Lingling-Routed-Model``/``By``/``Reason`` on a
    successful response; a failure used to drop them, so debugging the "429
    prison" meant crossing into the log to reconstruct which model was attempted
    and who chose it. The three keys that don't depend on a winning provider are
    surfaced here too -- target (which model was tried) and its routing story.
    Provider/Account are omitted: every provider was tried and refused (or none
    projected for the model), so a single value would mislead. ``HTTPException``
    accepts a ``headers=`` dict and Starlette merges it into the 503 response.
    """
    return {
        "X-Lingling-Routed-Model": _header_safe(target),
        "X-Lingling-Routed-By": _header_safe(routed_by),
        "X-Lingling-Reason": _header_safe(reason),
    }


def _harvest_stream_usage(raw: bytes, into: Dict[str, int]) -> None:
    """Read token counts off an SSE line without altering the byte stream.

    OpenCode's final content chunk carries an OpenAI ``usage`` object, and it
    also emits a proprietary ``normalizedUsage`` frame. Both are read; later
    frames win. Anything unparseable is ignored -- telemetry must never break a
    response.
    """
    if not raw.startswith(b"data:"):
        return
    payload = raw[5:].strip()
    if not payload or payload == b"[DONE]":
        return
    try:
        obj = json.loads(payload.decode("utf-8", "replace"))
    except (JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(obj, dict):
        return

    usage = obj.get("usage")
    if isinstance(usage, dict):
        if isinstance(usage.get("prompt_tokens"), int):
            into["tokens_in"] = usage["prompt_tokens"]
        if isinstance(usage.get("completion_tokens"), int):
            into["tokens_out"] = usage["completion_tokens"]
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), int):
            into["reasoning_tokens"] = details["reasoning_tokens"]

    norm = obj.get("normalizedUsage")
    if isinstance(norm, dict):
        for src_key, dst_key in (("inputTokens", "tokens_in"),
                                 ("outputTokens", "tokens_out"),
                                 ("reasoningTokens", "reasoning_tokens")):
            value = norm.get(src_key)
            if isinstance(value, int):
                into[dst_key] = value



# Core singletons (module-level so tests can introspect/patch them).
providers = registry.build_providers()
proxy_pool = registry.build_proxy_pool()
catalog = UnifiedCatalog(providers)
# Point the OpenCode provider at the live catalog so its "bypass the egress
# pool" set stays computed from whatever free models exist right now.
from providers import opencode as _opencode_module  # noqa: E402
_opencode_module.set_catalog_ref(catalog)
usage_store = UsageStore()
# Cloudflare WARP identity pool -- N free WARP accounts turned into local
# SOCKS5 proxies, auto-registered into proxy_pool when started.
warp_manager = WarpManager(
    root_dir=Path(config.DATA_DIR) / "warp",
    count=getattr(config, "WARP_IDENTITY_COUNT", 10),
)

# Tor egress lanes -- zero-account exit IPs beside the WARP pool. Each tor.exe
# is one SOCKS5 lane with a random exit; a restart rotates the exit. Disabled
# entirely with LINGLING_TOR_ENABLED=0.
from warp.tor_egress import TorEgressManager  # noqa: E402
tor_manager = TorEgressManager(
    root_dir=Path(config.DATA_DIR) / "tor",
    count=getattr(config, "TOR_LANE_COUNT", 3) if config.TOR_ENABLED else 0,
)

# Auto-healing WARP health daemon — runs in background, periodically checks
# every WARP proxy and regenerates dead/burned ones automatically. Tor lanes
# come along for the probe + rotation ride when enabled.
warp_health_daemon = warp_health.WarpHealthDaemon(
    warp_manager, proxy_pool,
    tor_manager=tor_manager if config.TOR_ENABLED and tor_manager.count > 0 else None,
    catalog=catalog,
    log=log.info,
)


def _sync_warp_to_pool() -> int:
    """Register every running WARP SOCKS5 proxy into the ProxyPool.

    This does a simple port check only — the background health daemon
    (warp_health_daemon) performs deeper HTTP probes and removes unhealthy
    proxies within its next cycle. This ensures the pool is populated
    immediately at startup rather than being empty because a few proxies
    were slow to establish their WARP tunnel.
    """
    existing = {p.id: p for p in proxy_pool.get_all_proxies()}
    added = 0

    for inst in warp_manager.instances:
        if not inst.private_key:
            continue
        if not inst._is_running():
            continue

        pid = f"warp-{inst.index}"
        if pid in existing:
            # Through the pool, so the write happens under its lock: `url` is the
            # field the executor reads when building an httpx client.
            proxy_pool.set_url(pid, inst.proxy_url)
        else:
            proxy_pool.add(
                inst.proxy_url,
                proxy_id=pid,
                label=f"WARP identity #{inst.index}",
            )
            added += 1
    return added



def _start_warp_at_startup() -> Dict[str, Any]:
    """Auto-start existing WARP identities and sync them to the proxy pool.

    Returns a status dict: ``started`` (bool); ``reason`` set on early-out when
    tools are missing, no identities are registered, or ``start_all`` raises;
    ``pool`` (proxy-pool snapshot); and on success ``synced_to_pool`` -- the
    count of new ``warp-N`` entries added. Existing entries get their URL
    refreshed in place so port migrations are reflected immediately.
    """
    if not warp_manager.tools_ready():
        return {"started": False, "reason": "tools not ready", "pool": proxy_pool.status()}
    status = warp_manager.status()
    if status["identities_registered"] == 0:
        return {"started": False, "reason": "no identities registered", "pool": proxy_pool.status()}

    try:
        res = warp_manager.start_all(log=log.info)
    except Exception as exc:  # noqa: BLE001
        log.warning("warp auto-start failed: %s", exc)
        return {"started": False, "reason": f"start_all failed: {exc}", "pool": proxy_pool.status()}

    added = _sync_warp_to_pool()
    res["pool"] = proxy_pool.status()
    res["synced_to_pool"] = added
    res["started"] = True
    return res

# ---------------------------------------------------------------------------
# Health, models, providers, key router, usage
# ---------------------------------------------------------------------------
@app.api_route("/api/hello", methods=["GET", "HEAD"])
def hello() -> Dict[str, Any]:
    """Liveness probe Claude Code HEADs on startup. Answering it avoids a scary
    401 auth-reject line on every session start."""
    return {"ok": True, "name": "Lingling"}


@app.get("/api/health")
def health(request: Request) -> Dict[str, Any]:
    """Liveness plus a secret-free summary of providers, catalog and pool.

    ``auth`` reports whether the gate is on and how *this* caller was
    identified, so the dashboard can display the real state instead of
    assuming it.
    """
    _, actor = auth.identify(request)
    return {
        "status": "ok",
        "name": "Lingling",
        "version": VERSION,
        "auth": {
            "required": config.REQUIRE_API_KEY,
            "actor": actor,
            "keys_issued": len(api_keys.list_keys()),
        },
        "usage_retention_days": config.USAGE_RETENTION_DAYS,
        "providers": {pid: prov.status() for pid, prov in providers.items()},
        "catalog": catalog.meta(),
        "proxies": proxy_pool.status(),
    }


def _multimodel_entry() -> Dict[str, Any]:
    """The hard-coded ``lingling-auto`` entry: the multi-model router itself.

    Not a model the gateway forwards -- the alias a client picks to ask Lingling
    to *choose*. The dispatcher reads the turn and routes onto a real free model,
    so ``lingling-auto`` has no upstream of its own: ``context_length`` and
    ``max_output`` are ``None`` (no ceiling to advertise, not "unbounded
    upstream"). It is baked into the catalog responses -- ``/v1/models`` lists it
    first, making it the default pick in OpenAI/Codex model pickers -- rather than
    fetched from OpenCode, which is exactly why the recycler can never touch it:
    ``catalog.mark_unavailable`` retires ids the upstream once listed, and the
    router never was one. Vision and reasoning are claimed True so a capability
    probe ("is there a model here that can see this image?") does not filter the
    catch-all out before the dispatcher picks a real vision/reasoning model; the
    providers list names every upstream so the dashboard's "served by" reads
    sensibly, while failover runs over the chosen model's *real* providers, not
    this alias.
    """
    return {
        "id": config.MULTIMODEL_ID,
        "name": config.MULTIMODEL_NAME,
        "description": config.MULTIMODEL_DESCRIPTION,
        "free": True,
        "vision": True,
        "reasoning": True,
        "multi_model": True,
        "dispatcher": config.DISPATCHER_MODEL,
        "providers": [p.display_name for p in providers.values()],
        "context_length": None,
        "max_output": None,
        "modalities": ["text", "image"],
        "provider": "lingling",
    }


def _openai_model_entry(model: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a Lingling catalog model into the OpenAI `/v1/models` shape."""
    mid = model.get("id") or ""
    return {
        "id": mid,
        "object": "model",
        "created": 0,
        "owned_by": (
            model.get("provider")
            or (model.get("providers") or ["lingling"])[0]
        ),
        "root": mid,
        "parent": None,
        "permission": [],
        "name": model.get("name") or mid,
        "description": model.get("description") or "",
        "context_length": model.get("context_length"),
        "max_output": model.get("max_output"),
        "modalities": model.get("modalities") or (["text", "image"] if model.get("vision") else ["text"]),
    }


@app.get("/api/models")
def get_models(refresh: int = 0) -> Dict[str, Any]:
    """The multi-model entry plus the pooled FREE models from all providers.

    Premium models are filtered out; each model lists the providers that serve it.
    """
    catalog.refresh(force=bool(refresh))
    return {
        "multimodel": _multimodel_entry(),
        "models": [lm.to_dict() for lm in catalog.free()],
        "meta": catalog.meta(),
    }


@app.get("/v1")
def v1_info() -> Dict[str, Any]:
    """Small compatibility landing page for clients pointed at `/v1`.

    OpenAI-compatible clients normally call `/v1/models` and either
    `/v1/chat/completions` (Codex < 0.144, Cline, etc.) or `/v1/responses`
    (Codex >= 0.144); Anthropic-shaped clients like Claude Code use
    `/v1/messages`. Surfacing all three keeps a browser/manual probe of the
    base URL honest about what's actually served.
    """
    return {
        "object": "api.info",
        "name": "Lingling OpenAI-compatible API",
        "version": VERSION,
        "endpoints": {
            "models": "/v1/models",
            "chat_completions": "/v1/chat/completions",
            "responses": "/v1/responses",
            "messages": "/v1/messages",
        },
    }


@app.get("/v1/models")
def v1_models(refresh: int = 0) -> Dict[str, Any]:
    """OpenAI-compatible model list for Codex/Cline model pickers.

    Response shape intentionally matches OpenAI:

        {"object":"list", "data":[{"id":"...", "object":"model", ...}]}

    The endpoint is keyless because it exposes no secrets and many local clients
    load the model picker before/without sending auth. `/v1/chat/completions`
    remains key-gated when LINGLING_REQUIRE_KEY=1.
    """
    catalog.refresh(force=bool(refresh))
    # lingling-auto goes first so it is the default model an OpenAI/Codex picker
    # lands on; everything after is the live free list (retired-by-recycler ids
    # are already excluded by catalog.free()).
    models = [_openai_model_entry(_multimodel_entry())]
    models.extend(_openai_model_entry(m.to_dict()) for m in catalog.free())
    return {"object": "list", "data": models}


@app.post("/api/models/refresh")
def refresh_models() -> Dict[str, Any]:
    catalog.refresh(force=True)
    return {
        "multimodel": _multimodel_entry(),
        "models": [lm.to_dict() for lm in catalog.free()],
        "meta": catalog.meta(),
    }


@app.get("/api/providers")
def get_providers() -> Dict[str, Any]:
    return {"providers": [prov.status() for prov in providers.values()]}


def _get_provider(pid: str):
    prov = providers.get(pid)
    if prov is None:
        raise HTTPException(404, f"unknown provider '{pid}'")
    return prov


@app.get("/api/providers/{pid}/keys")
def get_keys(pid: str) -> Dict[str, Any]:
    return _get_provider(pid).keys.status()


@app.delete("/api/providers/{pid}/keys/{kid}")
def remove_key(pid: str, kid: str) -> Dict[str, Any]:
    prov = _get_provider(pid)
    removed = prov.keys.remove(kid)
    if not removed:
        raise HTTPException(404, f"key '{kid}' not found")
    return {"removed": kid, "pool": prov.keys.status()}


# OpenCode shortcut (the OpenCode key router), kept for convenience.
@app.get("/api/accounts")
def get_accounts() -> Dict[str, Any]:
    return _get_provider("opencode").keys.status()


# ---------------------------------------------------------------------------
# Egress proxy pool (defeats OpenCode's IP-based free-tier limits)
# ---------------------------------------------------------------------------
class ProxyIn(BaseModel):
    """An egress proxy URL to add to the rotation pool.

    The URL may be supplied under any of these keys (``url`` preferred).
    Supported schemes: http://, https://, socks5://, socks4://.
    """
    url: Optional[str] = None
    proxy: Optional[str] = None
    server: Optional[str] = None
    label: str = ""
    id: str = ""

    def resolved_url(self) -> str:
        return self.url or self.proxy or self.server or ""


@app.get("/api/proxies")
def get_proxies_list() -> Dict[str, Any]:
    """Secret-free status of every proxy in the pool (credentials redacted)."""
    return proxy_pool.status()


@app.post("/api/proxies")
def add_proxy(body: ProxyIn) -> Dict[str, Any]:
    url = body.resolved_url().strip()
    if not url:
        raise HTTPException(400, "a proxy url (url/proxy/server) is required")
    if not (url.startswith("http://") or url.startswith("https://")
            or url.startswith("socks5://") or url.startswith("socks4://")):
        raise HTTPException(400, "url must use http(s):// or socks5:// scheme")
    px = proxy_pool.add(url, body.label, body.id)
    return {"added": px.status(), "pool": proxy_pool.status()}


@app.delete("/api/proxies/{pid}")
def remove_proxy(pid: str) -> Dict[str, Any]:
    removed = proxy_pool.remove(pid)
    if not removed:
        raise HTTPException(404, f"proxy '{pid}' not found")
    return {"removed": pid, "pool": proxy_pool.status()}


# ---------------------------------------------------------------------------
# Cloudflare WARP rotation (free, unlimited -- defeats OpenCode's IP limit)
# ---------------------------------------------------------------------------
class WarpCountIn(BaseModel):
    count: Optional[int] = None


@app.get("/api/warp")
def warp_status() -> Dict[str, Any]:
    """WARP manager status: how many identities registered, how many running."""
    return warp_manager.status()


@app.post("/api/warp/setup")
def warp_setup(body: WarpCountIn = WarpCountIn()) -> Dict[str, Any]:
    """One-time: download wgcf+wireproxy and register N free WARP identities.

    Idempotent -- existing identities are kept. Pass ``count`` to register more
    (only honored if it exceeds the current count; restart required otherwise).
    """
    if body.count and body.count > warp_manager.count:
        raise HTTPException(
            400,
            f"this manager was built for {warp_manager.count} identities. "
            f"Restart Lingling with WARP_IDENTITY_COUNT={body.count} to grow it.",
        )
    try:
        return warp_manager.setup_identities()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"WARP setup failed: {exc}")


@app.post("/api/warp/start")
def warp_start() -> Dict[str, Any]:
    """Launch all WARP wireproxy instances and feed them into the proxy pool."""
    if not warp_manager.tools_ready() or warp_manager.status()["identities_registered"] == 0:
        raise HTTPException(409, "WARP not set up yet. POST /api/warp/setup first.")
    return _start_warp_at_startup()


@app.post("/api/warp/stop")
def warp_stop() -> Dict[str, Any]:
    """Stop all WARP wireproxy instances (proxies stay in the pool, just offline)."""
    return warp_manager.stop_all()


@app.get("/api/warp/health")
def warp_health_status(probe: bool = False) -> Dict[str, Any]:
    """Health check: TCP-connect each WARP SOCKS5 port (optional HTTP probe).

    The HTTP probe uses opencode.ai as target to verify the tunnel is actually
    working (not just the port open).
    """
    results = []
    alive = 0
    dead = 0

    for inst in warp_manager.instances:
        port_listening = _port_is_open("127.0.0.1", inst.port, timeout=0.5)
        process_tracked = inst.process is not None and inst.process.poll() is None
        identity_ok = bool(inst.private_key)
        http_probe_ok: Optional[bool] = None

        if probe and port_listening:
            try:
                http_probe_ok = warp_health._socks5_http_probe(
                    inst.proxy_url, timeout=5.0
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("warp health HTTP probe failed for #%d: %s", inst.index, exc)
                http_probe_ok = False

        is_alive = port_listening and identity_ok

        if is_alive:
            alive += 1
        else:
            dead += 1

        results.append({
            "index": inst.index,
            "port": inst.port,
            "alive": is_alive,
            "port_listening": port_listening,
            "process_tracked": process_tracked,
            "has_identity": identity_ok,
            "http_probe_ok": http_probe_ok,
        })

    return {
        "total": len(warp_manager.instances),
        "alive": alive,
        "dead": dead,
        "probe": probe,
        "instances": results,
    }


@app.post("/api/warp/refresh")
def warp_refresh() -> Dict[str, Any]:
    """Atomically refresh all WARP identities: stop, wipe, re-register, restart, sync."""
    if not warp_manager.tools_ready():
        raise HTTPException(409, "WARP tools not available. POST /api/warp/setup first.")

    # Hold the daemon frozen for the whole stop/wipe/re-register/restart: a
    # periodic probe / heal cycle running into the middle of this -- probing
    # proxies we are about to remove from the pool, or regenerate_instance on
    # an identity we just wiped -- would race and restart half-wiped lanes.
    # The daemon's locks are non-blocking in its own acquire, so this just
    # skips its cycles; no deadlock.
    with warp_health_daemon.frozen():
        try:
            # 1. Stop all running wireproxy instances
            stop_result = warp_manager.stop_all()

            # 2. Remove every WARP entry from the proxy pool
            removed_ids: List[str] = []
            for px in proxy_pool.get_all_proxies():
                if px.id.startswith("warp-"):
                    proxy_pool.remove(px.id)
                    removed_ids.append(px.id)

            # 3. Wipe identity directories and reset state
            if warp_manager.identities_dir.exists():
                for ident_dir in warp_manager.identities_dir.iterdir():
                    if ident_dir.is_dir() and ident_dir.name.startswith("warp-"):
                        shutil.rmtree(ident_dir)
            for inst in warp_manager.instances:
                inst.process = None
                inst.private_key = ""
                inst.address_v4 = ""
                inst.address_v6 = ""

            # 4. Re-register all identities
            setup_result = warp_manager.setup_identities(log=log.info)

            # 5. Start all + sync to pool
            start_result = _start_warp_at_startup()

            instances = warp_manager.status()["instances"]

            return {
                "message": "WARP identities refreshed",
                "stopped": stop_result.get("stopped", 0),
                "removed_from_pool": removed_ids,
                "setup": setup_result,
                "start": start_result,
                "pool": proxy_pool.status(),
                "instances": instances,
            }
        except Exception as exc:  # noqa: BLE001
            log.exception("WARP refresh failed: %s", exc)
            raise HTTPException(500, f"WARP refresh failed: {exc}")


@app.get("/api/warp/probe")
def warp_probe_results() -> Dict[str, Any]:
    """Latest real-model probe results: per-proxy health status.

    Shows which WARP exit IPs are healthy, rate-limited, or dead. The probe
    runs at startup, periodically from the health daemon (interval_s below),
    and on demand via POST to this path. The Egress view renders the results
    in the identity rack so the operator can see at a glance which exits are
    usable.
    """
    results = warp_probe.latest_summary()
    if results is None:
        return {
            "probed": False,
            "message": "no probe has run yet",
            "interval_s": config.PROBE_INTERVAL_S,
            "server_time": time.time(),
        }
    return {
        "probed": True,
        "interval_s": config.PROBE_INTERVAL_S,
        "server_time": time.time(),
        **results,
    }


@app.post("/api/warp/probe")
def warp_probe_run() -> Dict[str, Any]:
    """Probe every exit with a real model request now, healing as needed.

    The dashboard's Probe button lands here — not on /api/warp/health, whose
    SOCKS5 CONNECT check is blind to rate limits (a 429'd exit tunnels fine).
    Runs the same probe + both healers + verification as the daemon's periodic
    pass, so what the Egress rack shows afterwards is current.
    """
    if not any(p.id.startswith("warp-") for p in proxy_pool.get_all_proxies()):
        raise HTTPException(
            409, "the pool has no WARP exits to probe — start WARP first."
        )
    summary = warp_health_daemon.probe_now()
    if summary is None:
        # Another probe is mid-flight (periodic loop or a second click).
        return {"busy": True, **(warp_probe.latest_summary() or {})}
    # Mirror the startup probe's ledger row so the Ledger view shows manual
    # probe activity too; periodic daemon probes stay console-only so the
    # ledger doesn't grow a row every interval.
    try:
        usage_store.log(
            "exit-probe", "exit-probe", "probe",
            reason=(
                f"{summary.healthy}/{summary.total} healthy, "
                f"{summary.rate_limited} rate-limited, "
                f"{summary.dead} dead"
                + (f", {summary.probe_error} probe-error" if summary.probe_error else "")
            ),
            status="ok" if (
                summary.healthy > 0
                or (summary.probe_error > 0 and summary.dead == 0)
            ) else "exhausted",
            provider="warp",
        )
    except Exception:  # noqa: BLE001
        pass
    return {"busy": False, **summary.to_dict()}


@app.get("/api/sampler")
def sampler_status() -> Dict[str, Any]:
    """Post-heal multi-model sampler state for the dashboard.

    The latest per-(model, exit) pass, or an empty disabled envelope when the
    sampler has not run. The request path reads the same state via
    ``routing.sampler.ok_exits`` to route a model onto its sampler-green exits
    and fail fast on an OpenCode-side outage; this endpoint is its read-only
    mirror for the UI.
    """
    snap = sampler.latest()
    return snap or {
        "enabled": config.SAMPLER_ENABLED, "models": [], "canary_ok_exits": [],
        "skipped_reason": "disabled" if not config.SAMPLER_ENABLED else "pending",
    }


@app.post("/api/warp/formation")
def warp_formation_run() -> Dict[str, Any]:
    """Assemble distinct exit lanes now: one lane per reachable exit IP.

    Rolls slots (aimed by the learned edge->exit map) until duplicates are
    spread across every unburned exit the PoP offers, verifying placements
    with Cloudflare's trace endpoint -- no OpenCode quota is spent. Runs in
    the background (a full assemble can take minutes of tunnel restarts, far
    beyond a sane HTTP timeout); progress lands in the log, the Ledger, and
    the Egress view's next poll. Shares the daemon's probe lock, so it cannot
    race a heal or probe pass.
    """
    if not any(p.id.startswith("warp-") for p in proxy_pool.get_all_proxies()):
        raise HTTPException(
            409, "the pool has no WARP exits to form — start WARP first."
        )

    def _run() -> None:
        result = warp_health_daemon.formation_now()
        if result and result.get("distinct"):
            try:
                usage_store.log(
                    "lane-formation", "lane-formation", "probe",
                    reason=(
                        f"{result['distinct']} distinct exits across "
                        f"{result['slots']} slots "
                        f"({result['rolls']} rolls, {result['elapsed_s']:.0f}s)"
                    ),
                    status="ok",
                    provider="warp",
                )
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_run, name="warp-formation", daemon=True).start()
    return {"started": True}


@app.get("/api/usage")
def get_usage(display_limit: int = 500, window_minutes: int = 60,
              bucket_seconds: int = 60) -> Dict[str, Any]:
    """Usage summary, recent feed, and both time series.

    ``display_limit`` caps the recent-request feed (UI display only, not a
    rate limit). ``window_minutes`` / ``bucket_seconds`` size the live series;
    the default hour-at-minute-resolution is what lets the dashboard show a
    request seconds after it happens, which the day-granular series cannot.
    """
    return {
        "summary": usage_store.summary(),
        "recent": usage_store.recent(display_limit),
        "daily": usage_store.daily(30),
        "live": usage_store.buckets(window_minutes, bucket_seconds),
        "server_time": time.time(),
    }


@app.get("/api/usage/since/{after_id}")
def get_usage_since(after_id: int, limit: int = 200) -> Dict[str, Any]:
    """Rows newer than ``after_id``, plus refreshed aggregates.

    The dashboard polls this instead of refetching the whole log, so the
    ledger stays live without re-transferring hundreds of rows each tick.
    """
    return {
        "rows": usage_store.since(after_id, limit),
        "summary": usage_store.summary(),
        "live": usage_store.buckets(60, 60),
        "server_time": time.time(),
    }


@app.delete("/api/usage")
def clear_usage() -> Dict[str, Any]:
    """Delete every logged request.

    Irreversible -- the ledger is the only record of past traffic.
    """
    removed = usage_store.reset()
    log.info("usage ledger cleared rows=%d", removed)
    return {"cleared": removed, "summary": usage_store.summary()}


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------
def _call_dispatcher_model(messages: List[Dict[str, Any]], model: str, session_id: str = "") -> str:
    """Run the dispatcher model across providers (itself failover-capable)."""
    provs = catalog.providers_for(model)
    if not provs:
        raise RuntimeError(f"no provider serves dispatcher model '{model}'")
    resp, _prov, _key, _attempts = executor.execute_nonstream(
        messages, model, provs, proxy_pool=proxy_pool, session_id=session_id,
        timeout=config.DISPATCH_TIMEOUT
    )
    return extract_assistant_text(resp)


def _run_dispatcher(messages: List[Dict[str, Any]], had_images: bool, session_id: str = ""):
    """Run the routing brain over the turn; return ``(target, reason, routed_by)``.

    Thin wrapper over :func:`dispatcher.decide` that closes over the session id,
    so the ``/v1/chat/completions``, ``/v1/responses`` and ``/v1/messages``
    handlers all route through one call site. ``routed_by`` is ``"dispatcher"`` on
    a clean decision and ``"fallback"`` when the router itself broke, so the
    ledger can tell "the dispatcher picked this" from "the dispatcher fell over
    and we picked for it". Any failure of the router model -- a 429, malformed
    JSON, an id nobody serves -- is caught here: a routing outage must never drop
    a request, so the deterministic, request-aware
    :func:`dispatcher.fallback_model` answers instead (see the inline note on why
    ``messages`` is passed).
    """
    def call_model(msgs, mdl):
        return _call_dispatcher_model(msgs, mdl, session_id=session_id)
    try:
        target, reason, _ = dispatcher.decide(
            messages, catalog, call_model, force_images=had_images
        )
        return target, reason, "dispatcher"
    except Exception as exc:  # never let a dispatcher failure drop the request
        # `messages` matters here: without it the fallback ignored the request and
        # returned the router's own model, so a dispatcher outage sent a refactor
        # or an image to whatever DISPATCHER_MODEL happened to be.
        target = dispatcher.fallback_model(catalog, had_images, messages=messages)
        return target, f"dispatcher unavailable: {exc}", "fallback"



# ---------------------------------------------------------------------------
# User API keys (for authenticating clients like Cline / Claude Code)
# ---------------------------------------------------------------------------
class ApiKeyIn(BaseModel):
    label: str = ""


@app.get("/api/keys")
def list_api_keys() -> Dict[str, Any]:
    return {"keys": api_keys.list_keys()}


@app.post("/api/keys")
def create_api_key(body: ApiKeyIn) -> Dict[str, Any]:
    key = api_keys.create_key(body.label)
    return {"created": key}


@app.delete("/api/keys/{kid}")
def revoke_api_key(kid: str) -> Dict[str, Any]:
    if not api_keys.revoke_key(kid):
        raise HTTPException(404, f"key '{kid}' not found")
    return {"revoked": kid}


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------

def _resolve_effort(
    params: Dict[str, Any], target: str, previous: Optional[Any] = None,
) -> Optional[str]:
    """Translate a client's ``reasoning_effort`` into one the target honours.

    Runs here, not in the request parsers, because the legal values depend on
    the model routing chose. Mutates ``params`` in place; the parameter is
    dropped when the label is unusable or the model exposes no effort control
    (OpenCode returns 200 for a value it ignores, so forwarding one would look
    like it worked while changing nothing).

    ``previous`` re-resolves against a different model on the failover path:
    without it the primary's already-clamped value would be re-clamped as if the
    client asked for it (`max` clamped to deepseek's `max`, then carried onto
    ling, which lacks it). Returns the value actually sent, for logging.
    """
    requested = params.pop("reasoning_effort", None)
    if previous is not None:
        requested = previous
    if requested is None:
        return None
    lm = catalog.by_id(target)
    caps = getattr(lm, "capabilities", None) or {}
    allowed = caps.get("effort")
    resolved = effort.resolve(requested, allowed)
    if resolved is None:
        log.info(
            "effort: dropped %r for %s (honours %s)",
            requested, target, allowed or "no effort control",
        )
        return None
    if resolved != requested:
        log.info("effort: %r -> %r for %s", requested, resolved, target)
    params["reasoning_effort"] = resolved
    return resolved


def _stream_pacing(
    model_id: str, body: Optional[Dict[str, Any]] = None,
) -> Tuple[float, float]:
    """Idle-watchdog budget and httpx read timeout for a stream on ``model_id``.

    A hidden-reasoning model is silent on the wire while it thinks, so the
    default budgets cut that off as a broken stream and stream_guard retries it
    to death. Reasoning models get the longer "thinking patience" from
    :func:`stream_idle.pacing_for`. The output is not touched: only how long a
    silent pause is tolerated changes. See that helper and
    ``config.STREAM_THINKING_TIMEOUT`` for the tradeoff.

    "Reasoning" is decided in four layers so a *future* hidden-reasoning model
    needs no per-model fix: (1) an operator override
    (``config.LONG_THINKING_MODELS``), (2) the live catalog flag
    (``LogicalModel.reasoning``), (3) a model learned from its wire behaviour
    (:mod:`routing.pacing_memory` -- reasoning tokens in a chunk, or a stall
    before any visible content), (4) the request body asking for reasoning this
    turn (``reasoning_effort`` / ``thinking`` / ``reasoning``). Layer (4) also
    *learns* the model, so a hidden-reasoning model whose listing does not
    advertise reasoning self-heals after its first requested-thinking turn.
    """
    reasoning = model_id in config.LONG_THINKING_MODELS
    if not reasoning:
        lm = catalog.by_id(model_id)
        reasoning = lm is not None and bool(getattr(lm, "reasoning", False))
    if not reasoning:
        reasoning = pacing_memory.is_reasoning(model_id)
    if not reasoning and _body_asks_reasoning(body):
        # The client asked the model to reason -- it supports it for this turn
        # and will stay silent while it thinks. Grant patience now and learn the
        # model, so turns without the param (the common case for a model whose
        # reasoning is hidden) are patient too instead of stalling every time.
        pacing_memory.mark_reasoning(model_id)
        reasoning = True
    return stream_idle.pacing_for(reasoning)


def _body_asks_reasoning(body: Optional[Dict[str, Any]]) -> bool:
    """The request asks the model to reason under any of the common vocabularies.

    Covers the OpenAI/Chat skill (``reasoning_effort``), the Codex Responses
    shape (``reasoning: {effort: ...}`` or a truthy flag), and the Anthropic /
    Claude Code thinking toggle (``thinking: {type: "enabled"|"adaptive"}`` or a
    budget). A model the client asks to reason goes silent while it thinks, so
    this both grants patience for the turn and -- on first sight -- learns the
    model for turns that arrive without the param.
    """
    if not isinstance(body, dict):
        return False
    if body.get("reasoning_effort"):
        return True
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        return True
    if reasoning in (True, "true", "yes", 1, "1"):
        return True
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and (
        thinking.get("type") in ("enabled", "adaptive")
        or thinking.get("budget_tokens")
    ):
        return True
    return False


def _messages_for_model(
    messages: List[Dict[str, Any]], model_id: str, has_images: bool,
) -> List[Dict[str, Any]]:
    """Make ``messages`` safe for ``model_id``, stripping images it cannot see.

    A text-only model rejects image parts (OpenCode 400), so every fallback site
    swaps them for the same placeholder the primary path uses. Idempotent, and a
    no-op for a vision-capable model or a text-only request.
    """
    if not has_images:
        return messages
    lm = catalog.by_id(model_id)
    if lm is not None and not lm.vision:
        return vision_bridge.strip_images_for_text_model(messages)
    return messages


# Per-model last "is this model actually gone from the free section?" recheck,
# used to gate _flag_model_retired below. A transient "Model is unavailable"
# burst (huge upstream traffic) fires that recheck on many concurrent requests
# at once; without a cooldown each would force a catalog refresh.
_retire_recheck_at: Dict[str, float] = {}
# Guards _retire_recheck_at across the check-then-set window between the
# cooldown test and the next-write slot: a burst of concurrent "unavailable"
# 400s would each pass the cooldown test before any of them updated the
# timestamp, scheduling N redundant /models refreshes for the same model.
# The refresh itself runs outside this lock so one model's outage cannot
# serialise the others.
_retire_lock = threading.Lock()


def _flag_model_retired(exc: Exception, model_id: str) -> bool:
    """Hide a model from the catalog when its free-tier chat starts 400-ing.

    A ``-free`` model that comes back from upstream with HTTP 400 "Model is
    unavailable" has stopped being served free, even when OpenCode keeps
    advertising it in /models -- and it keeps advertising them: the chronic
    deepseek/musespark case is "still listed, but the backend refuses chat".
    So the model is retired here on that signal alone; the chat path's own
    AllFailedError already propagates to failover, and this drop keeps the
    same model off the dashboard, /v1/models, the sampler and the dispatcher
    until either the operator clears backend/data/retired_models.json or
    LINGLING_RETIRED_MODEL_TTL_DAYS (7 days) drops the entry on the next
    gateway start. There is no probation-pop anymore: a parked model stays
    parked across refreshes and across restarts (a loaded entry is re-stamped
    ``now`` in _load_unavailable so a fresh restart doesn't trip any "old
    entry, must resurrect" logic; see catalog.refresh -- the self-heal pop
    loop was removed because chronic offenders kept popping straight back to
    "everywhere" within one refresh). A per-model cooldown keeps a burst of
    "unavailable" 400s from firing more than one retirement per window.
    """
    err = getattr(exc, "last_error", None)
    if err is None or getattr(err, "status_code", None) != 400:
        return False
    if "unavailable" not in str(getattr(err, "detail", "")).lower():
        return False

    now = time.time()
    with _retire_lock:
        last = _retire_recheck_at.get(model_id, 0.0)
        if now - last < config.RETIRE_RECHECK_COOLDOWN_S:
            # Decisive retirement attempt already made recently for this model;
            # don't hammer the lock / persisted-retired set on every concurrent
            # 400 in this burst. The parked entry is already effective across
            # /v1/models, the dashboard, the sampler and the dispatcher.
            return False
        _retire_recheck_at[model_id] = now

    if catalog.is_unavailable(model_id):
        # Already parked -- a parked model stays parked across refreshes and
        # across restarts (catalog._load_unavailable re-stamps the persisted
        # entry to ``now`` on load), so a concurrent in-flight 400 for a model
        # already in the retired set has nothing to do here.
        return False

    try:
        catalog.mark_unavailable(model_id)
    except Exception:  # noqa: BLE001
        return False
    log.info(
        "catalog: retired model %s (upstream refused free-tier chat with "
        "'unavailable' 400; parked until retired_models.json is cleared or "
        "LINGLING_RETIRED_MODEL_TTL_DAYS drops it on the next gateway start)",
        model_id,
    )
    return True


def _log_stream_attempt(event: str, data: Dict[str, Any]) -> None:
    """Per-attempt stream lifecycle log, fired inside ``execute_stream``'s retry loop.

    The responses handler's only post-arrival line used to fire *after*
    ``execute_stream`` returned -- by which point minutes of silent
    first-chunk waiting across burned exit IPs were invisible in the
    backend log, indistinguishable from a client that never sent a
    request at all. Each retry now emits a ``dispatch`` line (with the
    proxy picked, so a wait that turns silent is no longer
    indistinguishable from "no request was ever sent"), then either a
    ``first_token`` line confirming the lane is live or a ``failure``
    line with the status code naming why an IP burned. A typical
    succeed-first turn is 2 lines; a burn-and-retry turn is 3 lines per
    burned attempt plus a survivor pair -- a heartbeat the previous
    single-after-the-fact summary never had.
    """
    proxy = data.get("proxy") or "direct"
    n = data.get("n", "?")
    prov = data.get("prov")
    if event == "dispatch":
        log.info(
            "stream attempt: dispatched #%d prov=%s proxy=%s model=%s",
            n, prov, proxy, data.get("model"),
        )
    elif event == "first_token":
        log.info(
            "stream attempt: first_token #%d after %.0fms prov=%s proxy=%s",
            n, data.get("elapsed_ms", -1), prov, proxy,
        )
    elif event == "failure":
        log.info(
            "stream attempt: failed #%d after %.0fms status=%s prov=%s proxy=%s",
            n, data.get("elapsed_ms", -1), data.get("status"), prov, proxy,
        )


async def _execute_with_egress_wait(fn, *args, **kwargs):
    """Run an executor call, waiting out a fully-cooled egress pool once.

    On AllFailedError this asks the pool whether the failure was exhaustion
    (every exit cooling) and, if so, holds the request until the soonest exit
    returns then retries once. ``wait_for_egress`` returns 0 when waiting can't
    help, and the original error propagates -- so the caller behaves exactly as
    before parking existed, model fallback included.
    """
    # Use the caller's proxy_pool (passed via kwargs), not the module-level one,
    # or the egress-wait check could consult the wrong pool (visible in tests).
    pool = kwargs.get("proxy_pool", proxy_pool)
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except executor.AllFailedError as exc:
        # fn is execute_nonstream/execute_stream; args[1] is the model id. If the
        # upstream retired its free tier, stop offering the model.
        if len(args) > 1 and isinstance(args[1], str):
            _flag_model_retired(exc, args[1])
        waited = await parking.wait_for_egress(pool, config.EGRESS_WAIT_BUDGET, log)
        if not waited:
            raise
        log.info("egress: waited %.1fs for a fresh exit, retrying the request", waited)
        return await run_in_threadpool(fn, *args, **kwargs)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    request_started = time.time()
    client = request.client.host if request.client else "unknown"

    try:
        body = await request.json()
    except JSONDecodeError as exc:
        log.warning("chat invalid_json client=%s error=%s", client, exc)
        raise HTTPException(400, "Invalid JSON request body.")
    if not isinstance(body, dict):
        # A bare list/string/null is well-formed JSON but not a request. Every
        # field read below assumes a mapping, so this became an AttributeError
        # inside the handler and surfaced as a 500 rather than a 400.
        raise HTTPException(400, "Request body must be a JSON object.")

    requested = body.get("model")
    if not isinstance(requested, str) or not requested:
        raise HTTPException(400, "Missing or invalid 'model' field.")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, "Missing or invalid 'messages' field.")
    if not all(isinstance(m, dict) for m in messages):
        raise HTTPException(400, "Each entry in 'messages' must be an object.")

    stream = bool(body.get("stream"))
    session_id = body.get("session_id", "")
    if not isinstance(session_id, str):
        session_id = ""
    had_images = vision_bridge.messages_have_images(messages)

    # Decide target model.
    if requested == config.MULTIMODEL_ID:
        # _run_dispatcher calls the dispatcher model over sync httpx and can take
        # seconds; off the event loop it goes, same as the executor below.
        target, reason, routed_by = await run_in_threadpool(
            _run_dispatcher, messages, had_images, session_id
        )
    else:
        target = requested
        if not catalog.providers_for(target):
            # Allow provider-prefixed ids such as opencode/deepseek-v4-flash-free.
            bare = target.split("/", 1)[1] if "/" in target else target
            if bare != target and catalog.providers_for(bare):
                target = bare
            else:
                # Two distinct "no" cases, deliberately separated.
                # is_unavailable means the recycler retired it -- the model WAS
                # listed free and OpenCode dropped it (or the operator seeded it
                # in LINGLING_RETIRED_MODELS), so the message points the client
                # at /v1/models, which won't list it. The else is a genuinely
                # unknown id -- a typo, an unrecognised provider-prefixed name, a
                # premium id -- that was never free here, so "unknown" rather
                # than the misleading "no longer served".
                if catalog.is_unavailable(target):
                    raise HTTPException(400, f"Model {requested!r} is no longer served for free by OpenCode; pick from /v1/models.")
                raise HTTPException(400, f"Unknown or unsupported model: {requested!r}")
        reason = "user requested"
        routed_by = "user"

    target_providers = catalog.providers_for(target)
    if not target_providers:
        raise HTTPException(
            503, f"No provider available for model '{target}'.",
            headers=_failure_provenance_headers(target, routed_by, reason),
        )

    target_lm = catalog.by_id(target)
    if target_lm is not None and not target_lm.vision:
        messages = vision_bridge.strip_images_for_text_model(messages)

    params = _passthrough_params(body)
    original_effort = params.get("reasoning_effort")
    _resolve_effort(params, target)

    routing_meta = {
        "requested_model": requested,
        "routed_model": target,
        "routed_by": routed_by,
        "reason": reason,
    }

    # Non-streaming path.
    if not stream:
        started = time.time()
        try:
            # The executor and the provider beneath it are synchronous
            # (httpx.Client), so awaiting them on the event loop would block
            # every other request for the whole upstream call.
            resp, prov, key, attempts = await _execute_with_egress_wait(
                executor.execute_nonstream,
                messages, target, target_providers, proxy_pool=proxy_pool,
                session_id=session_id, **params
            )
        except executor.AllFailedError as exc:
            fallback = dispatcher.fallback_model(
                catalog, had_images, exclude={target} | set(sampler.cooked_models()), messages=messages,
            )
            fallback_providers = catalog.providers_for(fallback)
            if fallback and fallback != target and fallback_providers:
                try:
                    retry_params = dict(params)
                    # Each model publishes its own legal effort values, so the
                    # primary's clamped value is re-resolved from the client's
                    # original label rather than carried across untouched.
                    _resolve_effort(retry_params, fallback, previous=original_effort)
                    resp, prov, key, attempts2 = await _execute_with_egress_wait(
                        executor.execute_nonstream,
                        _messages_for_model(messages, fallback, had_images),
                        fallback, fallback_providers, proxy_pool=proxy_pool,
                        session_id=session_id, **retry_params
                    )
                    attempts = exc.attempts + attempts2
                    target = fallback
                    reason = f"primary model failed ({exc.last_error}); fell back to {fallback}"
                    routed_by = "fallback"
                    routing_meta.update(
                        {"routed_model": target, "routed_by": routed_by, "reason": reason}
                    )
                except (executor.AllFailedError, executor.NoProviderError) as fallback_exc:
                    usage_store.log(
                        requested, target, routed_by, reason, status="exhausted",
                        had_images=had_images,
                        error=str(getattr(fallback_exc, "last_error", fallback_exc))[:300],
                    )
                    raise HTTPException(
                        503, f"All providers exhausted for '{target}'.",
                        headers=_failure_provenance_headers(target, routed_by, reason),
                    )
            else:
                usage_store.log(
                    requested, target, routed_by, reason, status="exhausted",
                    had_images=had_images, error=str(exc.last_error)[:300],
                )
                raise HTTPException(
                    503, f"All providers exhausted for '{target}'.",
                    headers=_failure_provenance_headers(target, routed_by, reason),
                )

        latency = (time.time() - started) * 1000.0
        usage = extract_usage(resp)
        account_id = key.id if key is not None else None  # keyless -> no account
        usage_store.log(
            requested, target, routed_by, reason,
            tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
            reasoning_tokens=usage.get("reasoning_tokens", 0),
            latency_ms=latency, status="ok", had_images=had_images,
            account_id=account_id, provider=prov.id,
        )
        resp["lingling"] = {
            **routing_meta, "provider": prov.id, "account": account_id, "attempts": attempts,
        }
        log.info(
            "chat handled requested=%s target=%s provider=%s stream=false latency_ms=%.1f",
            requested, target, prov.id, (time.time() - request_started) * 1000.0,
        )
        return JSONResponse(resp)

    # Streaming path: open the upstream SSE connection and wait for the first
    # chunk so dead proxies can fail over before we return HTTP 200. After that,
    # the bytes are forwarded untouched, guarded by a single mid-flight retry.
    #
    # Recovery re-emits the whole answer after a reset marker, so a client that
    # ignores the marker would render it twice. Callers can opt out per request.
    recover = bool(body.get("lingling_recover", config.STREAM_RECOVERY))
    # A hidden-reasoning model is silent on the wire while it thinks; the default
    # watchdog/read budgets would cut that off as a broken stream, so a reasoning
    # target gets a longer "thinking patience" (output is untouched). pacing_for
    # returns (idle_budget, read_timeout); reorder into the names used below.
    stream_idle_budget, stream_read_to = _stream_pacing(target, body)
    started = time.time()
    try:
        # execute_stream blocks until the first upstream chunk arrives (up to the
        # per-stream read timeout below), so it cannot run on the event loop. The
        # generator it returns is safe: StreamingResponse iterates sync iterators
        # in a worker thread already.
        stream_iter, prov, key, attempts = await _execute_with_egress_wait(
            executor.execute_stream,
            messages, target, target_providers, proxy_pool=proxy_pool,
            session_id=session_id, timeout=stream_read_to,
            on_attempt=_log_stream_attempt, **params
        )
    except (executor.AllFailedError, executor.NoProviderError) as exc:
        # Surface the actual upstream rejection (status + detail), so the
        # dashboard, the log and the returned 503 name the real cause rather than
        # the generic "No upstream stream started within X s" every exhaustion
        # used to read as.
        error = str(getattr(exc, "last_error", exc))
        last_status = getattr(getattr(exc, "last_error", None), "status_code", None)
        log.warning(
            "chat stream: all upstream attempts failed target=%s last_status=%s error=%s",
            target, last_status, error[:300],
        )

        # Pre-HTTP-200 fallback: mirror the chat non-stream path's contract (and
        # the /v1/responses stream path). execute_stream raises while still
        # waiting for the first chunk, so no bytes are on the wire yet -- a model
        # swap is safe and invisible to the client, exactly the window the chat
        # non-stream handler already fell back in. Without this the stream path
        # returned a bare 503 on a transiently-exhausted model while a one-line
        # curl through the non-stream branch recovered -- the streaming-vs-non-
        # stream fallback asymmetry upstream tracks as LiteLLM #25843. Only route
        # onto a model the sampler has not proven cooked, so a model every green
        # exit already refused is not churned again.
        fallback = dispatcher.fallback_model(
            catalog, had_images,
            exclude={target} | set(sampler.cooked_models()), messages=messages,
        )
        fallback_providers = catalog.providers_for(fallback) if fallback else None
        if not (fallback and fallback != target and fallback_providers):
            usage_store.log(
                requested, target, routed_by, reason, status="exhausted",
                had_images=had_images, error=error[:300],
            )
            raise HTTPException(
                503,
                f"No upstream stream started within {stream_read_to:g}s for '{target}' "
                f"(last upstream status {last_status}).",
                headers=_failure_provenance_headers(target, routed_by, reason),
            )
        log.warning(
            "chat stream rerouting %s -> %s (primary exhausted, last_status=%s)",
            target, fallback, last_status,
        )
        retry_params = dict(params)
        _resolve_effort(retry_params, fallback, previous=original_effort)
        retry_messages = _messages_for_model(messages, fallback, had_images)
        # The retry may land on a non-reasoning model whose thinking patience is
        # shorter than the primary's -- re-derive pacing so the idle watchdog is
        # not tuned for the dead model.
        retry_idle_budget, retry_read_to = _stream_pacing(fallback, body)
        try:
            stream_iter, prov, key, attempts2 = await _execute_with_egress_wait(
                executor.execute_stream,
                retry_messages, fallback, fallback_providers, proxy_pool=proxy_pool,
                session_id=session_id, timeout=retry_read_to,
                on_attempt=_log_stream_attempt, **retry_params
            )
        except (executor.AllFailedError, executor.NoProviderError) as fallback_exc:
            fb_error = str(getattr(fallback_exc, "last_error", fallback_exc))
            fb_last_status = getattr(
                getattr(fallback_exc, "last_error", None), "status_code", None,
            )
            log.warning(
                "chat stream: fallback %s also exhausted target=%s last_status=%s "
                "error=%s",
                fallback, target, fb_last_status, fb_error[:300],
            )
            usage_store.log(
                requested, target, routed_by, reason, status="exhausted",
                had_images=had_images,
                error=f"{error[:150]} || {fb_error[:150]}",
            )
            raise HTTPException(
                503,
                f"All providers exhausted for '{target}' "
                f"(primary last status {last_status}; fallback {fallback} last "
                f"status {fb_last_status}).",
                headers=_failure_provenance_headers(target, routed_by, reason),
            )
        # The fallback opened cleanly -- rebind routing metadata the rest of the
        # handler streams against (the row opened below, `_reopen`, the idle
        # watchdog and the finalize envelope must all name the model the answer
        # actually came from). Same rewrite the chat non-stream path does on a
        # successful fallback, plus re-derived pacing so streaming recovery / idle
        # budgets match the model now riding the wire.
        attempts = list(getattr(exc, "attempts", [])) + list(attempts2)
        target = fallback
        target_providers = fallback_providers
        params = retry_params
        stream_idle_budget, stream_read_to = retry_idle_budget, retry_read_to
        reason = f"primary stream failed ({getattr(exc, 'last_error', exc)}); " \
                 f"fell back to {fallback}"
        routed_by = "fallback"

    account_id = key.id if key is not None else None  # keyless -> no account
    # Log at first chunk so a stream that dies mid-flight still leaves a record;
    # finalize() fills in the token counts once the terminal usage chunk lands.
    row_id = usage_store.log(
        requested, target, routed_by, reason,
        latency_ms=(time.time() - started) * 1000.0, status="ok_stream",
        had_images=had_images, account_id=account_id, provider=prov.id,
        streamed=True,
    )
    log.info(
        "chat: streaming requested=%s target=%s provider=%s first_chunk_ms=%.1f",
        requested, target, prov.id, (time.time() - request_started) * 1000.0,
    )

    # Mid-stream recovery may land on a different model, but only when the client
    # asked the router to choose. A request that named `deepseek-v4-flash-free`
    # gets that model on the retry too; `lingling-auto` delegated the choice, so
    # the retry is free to re-decide -- and re-decoding matters because the usual
    # reason a stream dies mid-flight is the model itself stalling, and reopening
    # on the same one spends the single retry on the thing that just failed.
    auto_routed = requested == config.MULTIMODEL_ID
    reroute = {"model": target, "reason": reason, "by": routed_by}

    def _reopen():
        """Open a replacement upstream stream for mid-flight recovery.

        Goes back through the executor for a fresh exit IP. An auto-routed turn
        also picks a fresh model (excluding the one that broke); the chosen model
        is recorded on `reroute` so the ledger reports where the answer came from.
        """
        retry_model = target
        retry_params = params
        if auto_routed:
            alternative = dispatcher.fallback_model(
                catalog, had_images, exclude={target} | set(sampler.cooked_models()), messages=messages,
            )
            alt_providers = catalog.providers_for(alternative)
            if alternative and alternative != target and alt_providers:
                retry_model = alternative
                # Effort is per-model, so it is re-resolved from the client's
                # original label rather than carried across from the dead model.
                retry_params = dict(params)
                _resolve_effort(retry_params, retry_model, previous=original_effort)
                reroute.update({
                    "model": retry_model,
                    "reason": f"stream broke on {target}; rerouted to {retry_model}",
                    "by": "reroute",
                })
                log.warning(
                    "chat stream rerouting mid-flight: %s -> %s", target, retry_model
                )
        providers_for_retry = catalog.providers_for(retry_model) or target_providers
        # A text-only retry target cannot see the images the stalled vision
        # model could; swap them for the same placeholder the non-streaming
        # fallback uses, or OpenCode answers 400 and the retry is wasted.
        retry_messages = _messages_for_model(messages, retry_model, had_images)
        # The retry may land on a different model: re-derive its thinking
        # patience so a reroute off a hidden-reasoning model does not carry the
        # long budget onto a normal one (and vice versa).
        retry_idle_budget, retry_read_to = _stream_pacing(retry_model, body)
        again, _prov, _key, _attempts = executor.execute_stream(
            retry_messages, retry_model, providers_for_retry, proxy_pool=proxy_pool,
            session_id=session_id, timeout=retry_read_to,
            on_attempt=_log_stream_attempt, **retry_params
        )
        return stream_idle.with_idle_timeout(again, retry_idle_budget, log)

    def _hold_for_egress():
        """Wait for a free exit before the mid-stream retry reopens.

        Same decision as the pre-first-token wait, but this one runs inside a
        live SSE response, so it emits keepalive comments instead of awaiting
        silently. Yields nothing when waiting cannot help.
        """
        yield from parking.hold_stream_for_egress(
            proxy_pool, config.EGRESS_WAIT_BUDGET, log,
        )

    def event_stream():
        # OpenCode's SSE bytes are forwarded verbatim. Two things layer on top:
        # each frame is read in passing to harvest token counts (else streamed
        # requests record zero usage), and a stream that dies before completion
        # is retried once on a fresh exit IP (see stream_guard).
        seen: Dict[str, int] = {}
        outcome = stream_guard.StreamOutcome()
        try:
            yield from stream_guard.guarded_stream(
                open_stream=_reopen,
                # Wrapped in the idle watchdog: a stream that stops speaking
                # without closing raises StreamStalled, which guarded_stream
                # already treats as a mid-flight death and retries once. The
                # budget is model-aware: a hidden-reasoning model is allowed a
                # longer silent thinking pause (see _stream_pacing).
                first=stream_idle.with_idle_timeout(
                    stream_iter, stream_idle_budget, log),
                outcome=outcome,
                on_chunk=lambda raw: _harvest_stream_usage(raw, seen),
                log=log,
                enabled=recover,
                hold=_hold_for_egress,
                # Reports the model the retry landed on, so the reset frame can
                # say "different model" rather than always claiming a new exit IP.
                retry_model=lambda: (
                    reroute["model"] if reroute["model"] != target else None
                ),
                # The resolved target: so a hidden-reasoning model can be learned
                # from its wire behaviour (reasoning tokens, or a stall before any
                # visible content) and given thinking patience on the retry / next
                # turn via _stream_pacing -> pacing_memory.
                model_id=target,
            )
        finally:
            if outcome.error:
                status = "stream_broken"
            elif outcome.recovered:
                status = "ok_recovered"
            else:
                status = None  # keep ok_stream
            # Total duration, not time-to-first-token: this column answers
            # "how long did that request take".
            usage_store.finalize(
                row_id,
                tokens_in=seen.get("tokens_in", 0),
                tokens_out=seen.get("tokens_out", 0),
                reasoning_tokens=seen.get("reasoning_tokens", 0),
                latency_ms=(time.time() - started) * 1000.0,
                status=status,
                error=outcome.error,
                # A mid-flight reroute means the answer came from a different
                # model than the row was opened with. Recording the original
                # would make the ledger name a model that produced nothing.
                routed_model=reroute["model"] if reroute["model"] != target else None,
                routed_by=reroute["by"] if reroute["model"] != target else None,
                reason=reroute["reason"] if reroute["model"] != target else None,
            )
            if outcome.recovered:
                log.info(
                    "chat: recovered target=%s attempts=%d",
                    reroute["model"], outcome.attempts,
                )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # X-Lingling-* mirror the routing decision onto the response headers
            # so a client or operator tool can see where a turn actually went
            # without parsing the body's ``lingling`` key (the /v1/responses and
            # /v1/messages entrypoints carry the same set). `_header_safe`
            # latin-1-safe sanitises the model-generated `reason` -- its em-dashes
            # and smart quotes would otherwise abort the whole response. Stream-Mode
            # tells the client whether a mid-flight retry ("guarded") is opt-out
            # for this turn (see ``lingling_recover`` / STREAM_RECOVERY).
            "X-Lingling-Routed-Model": _header_safe(target),
            "X-Lingling-Routed-By": _header_safe(routed_by),
            "X-Lingling-Reason": _header_safe(reason),
            "X-Lingling-Provider": _header_safe(prov.id),
            "X-Lingling-Account": _header_safe(account_id or ""),
            "X-Lingling-Stream-Mode": "guarded" if recover else "passthrough",
        },
    )


@app.post("/v1/responses")
async def responses(request: Request):
    """OpenAI Responses-compatible entrypoint for Codex.

    Codex 0.144+ removed ``wire_api = "chat"`` and now insists on
    ``/v1/responses``. Lingling still talks to providers through chat
    completions, so this endpoint translates once at the API edge and reuses the
    same dispatcher, executor, proxy pool, and usage ledger as `/v1/chat/completions`.
    """
    request_started = time.time()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object.")
        requested, messages, params = responses_bridge.request_to_chat(body)
    except JSONDecodeError as exc:
        log.warning("responses invalid_json error=%s", exc)
        raise HTTPException(400, "Invalid JSON request body.")
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    stream = bool(body.get("stream"))
    log.info(
        "responses: received POST requested=%s stream=%s",
        requested, stream,
    )
    session_id = request.headers.get("session-id") or body.get("prompt_cache_key", "")
    if not isinstance(session_id, str):
        session_id = ""
    had_images = vision_bridge.messages_have_images(messages)

    if requested == config.MULTIMODEL_ID:
        target, reason, routed_by = await run_in_threadpool(
            _run_dispatcher, messages, had_images, session_id
        )
    else:
        target = requested
        if not catalog.providers_for(target):
            bare = target.split("/", 1)[1] if "/" in target else target
            if bare != target and catalog.providers_for(bare):
                target = bare
            else:
                if catalog.is_unavailable(target):
                    raise HTTPException(400, f"Model {requested!r} is no longer served for free by OpenCode; pick from /v1/models.")
                raise HTTPException(400, f"Unknown or unsupported model: {requested!r}")
        reason = "user requested"
        routed_by = "user"

    target_providers = catalog.providers_for(target)
    if not target_providers:
        raise HTTPException(
            503, f"No provider available for model '{target}'.",
            headers=_failure_provenance_headers(target, routed_by, reason),
        )
    target_lm = catalog.by_id(target)
    if target_lm is not None and not target_lm.vision:
        messages = vision_bridge.strip_images_for_text_model(messages)
    # Capture the client's effort before _resolve_effort mutates ``params``:
    # afterwards ``reasoning_effort`` holds the clamped value the upstream got,
    # and failover would re-clamp the clamps against the fallback instead of the
    # value the client asked for (the same pitfall _resolve_effort's ``previous``
    # branch was added to escape).
    original_effort = params.get("reasoning_effort")
    _resolve_effort(params, target)

    if not stream:
        started = time.time()
        try:
            resp, prov, key, attempts = await _execute_with_egress_wait(
                executor.execute_nonstream,
                messages, target, target_providers, proxy_pool=proxy_pool,
                session_id=session_id, **params
            )
        except executor.AllFailedError as exc:
            # Fall back to another model, exactly as the chat path does. Without
            # this a rate-limited free tier returned a hard 503 to Codex while
            # Cline silently got an answer from a different model.
            fallback = dispatcher.fallback_model(
                catalog, had_images, exclude={target} | set(sampler.cooked_models()), messages=messages,
            )
            fallback_providers = catalog.providers_for(fallback)
            if not (fallback and fallback != target and fallback_providers):
                usage_store.log(
                    requested, target, routed_by, reason, status="exhausted",
                    had_images=had_images, error=str(exc.last_error)[:300],
                )
                raise HTTPException(
                    503, f"All providers exhausted for '{target}'.",
                    headers=_failure_provenance_headers(target, routed_by, reason),
                )
            try:
                retry_params = dict(params)
                _resolve_effort(retry_params, fallback, previous=original_effort)
                resp, prov, key, attempts2 = await _execute_with_egress_wait(
                    executor.execute_nonstream,
                    _messages_for_model(messages, fallback, had_images),
                    fallback, fallback_providers, proxy_pool=proxy_pool,
                    session_id=session_id, **retry_params
                )
            except (executor.AllFailedError, executor.NoProviderError) as fallback_exc:
                usage_store.log(
                    requested, target, routed_by, reason, status="exhausted",
                    had_images=had_images,
                    error=str(getattr(fallback_exc, "last_error", fallback_exc))[:300],
                )
                raise HTTPException(
                    503, f"All providers exhausted for '{target}'.",
                    headers=_failure_provenance_headers(target, routed_by, reason),
                )
            attempts = exc.attempts + attempts2
            target = fallback
            reason = f"primary model failed ({exc.last_error}); fell back to {fallback}"
            routed_by = "fallback"
        usage = extract_usage(resp)
        account_id = key.id if key is not None else None
        usage_store.log(
            requested, target, routed_by, reason,
            tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
            reasoning_tokens=usage.get("reasoning_tokens", 0),
            latency_ms=(time.time() - started) * 1000.0, status="ok",
            had_images=had_images, account_id=account_id, provider=prov.id,
        )
        out = responses_bridge.response_object(resp, requested, target, prov.id)
        out["lingling"].update({
            "requested_model": requested, "routed_by": routed_by,
            "reason": reason, "account": account_id, "attempts": attempts,
        })
        log.info(
            "responses: handled requested=%s target=%s provider=%s stream=false latency_ms=%.1f",
            requested, target, prov.id, (time.time() - request_started) * 1000.0,
        )
        return JSONResponse(out)

    # Hidden-reasoning models get a longer silent-thinking patience; see chat.
    stream_idle_budget, stream_read_to = _stream_pacing(target, body)
    # Same recovery contract as the chat path: a stream that breaks after
    # HTTP 200 is retried once on a fresh exit IP. Opt-out per request with
    # `lingling_recover: false` or via LINGLING_STREAM_RECOVERY. Without it, a
    # break here was terminal `stream_broken` (the dashboard counted 80 lost
    # muse-spark turns in one hour on this path while the chat path recovered
    # its 2). The Responses wire has no chat-path `lingling_reset` to discard
    # rendered deltas; the bridge flushes in-flight items with `.done`
    # (status `incomplete`) and the fresh-egress retry opens new
    # `response.output_item.added` events.
    recover = bool(body.get("lingling_recover", config.STREAM_RECOVERY))
    started = time.time()
    try:
        stream_iter, prov, key, attempts = await _execute_with_egress_wait(
            executor.execute_stream,
            messages, target, target_providers, proxy_pool=proxy_pool,
            session_id=session_id, timeout=stream_read_to,
            on_attempt=_log_stream_attempt, **params
        )
    except (executor.AllFailedError, executor.NoProviderError) as exc:
        # Surface the actual upstream rejection. The 503 message and the usage
        # ledger never show it today -- every exhaustion reads as the same
        # generic "No upstream stream started within X s" -- so a Codex-shaped
        # turn that OpenCode answers with, say, a 400 over a tool-calls payload
        # is indistinguishable from a 429 IP burn. The status + detail here let
        # the dashboard, the log and the returned 503 all name the real cause.
        error = str(getattr(exc, "last_error", exc))
        last_status = getattr(getattr(exc, "last_error", None), "status_code", None)
        log.warning(
            "responses stream: all upstream attempts failed target=%s last_status=%s "
            "error=%s",
            target, last_status, error[:300],
        )

        # Pre-HTTP-200 fallback: mirror the chat non-stream path's contract. The
        # streaming catch is fired *before* any bytes are on the wire (execute_stream
        # raises AllFailedError while waiting for the first chunk), so there is no
        # half-written answer to discard and a model swap is safe -- exactly the
        # window the chat non-stream handler already fell back in. The streaming
        # path had no analogue, so Codex 0.146 took the terminal 503 here as a
        # session-killing error while a one-line curl through /v1/chat/completions
        # (whose non-stream branch falls back) recovered. Only route onto a model
        # the sampler has not proven cooked -- retrying onto a model every green
        # exit already refused would just exhaust again.
        fallback = dispatcher.fallback_model(
            catalog, had_images,
            exclude={target} | set(sampler.cooked_models()), messages=messages,
        )
        fallback_providers = catalog.providers_for(fallback) if fallback else None
        if not (fallback and fallback != target and fallback_providers):
            usage_store.log(
                requested, target, routed_by, reason, status="exhausted",
                had_images=had_images, error=error[:300],
            )
            raise HTTPException(
                503,
                f"No upstream stream started within {stream_read_to:g}s for '{target}' "
                f"(last upstream status {last_status}).",
                headers=_failure_provenance_headers(target, routed_by, reason),
            )
        log.warning(
            "responses stream rerouting %s -> %s (primary exhausted, last_status=%s)",
            target, fallback, last_status,
        )
        retry_params = dict(params)
        _resolve_effort(retry_params, fallback, previous=original_effort)
        retry_messages = _messages_for_model(messages, fallback, had_images)
        # The retry may land on a non-reasoning model whose thinking patience is
        # shorter than muse-spark's -- re-derive pacing so the idle watchdog is
        # not tuned for the dead model.
        retry_idle_budget, retry_read_to = _stream_pacing(fallback, body)
        try:
            stream_iter, prov, key, attempts2 = await _execute_with_egress_wait(
                executor.execute_stream,
                retry_messages, fallback, fallback_providers, proxy_pool=proxy_pool,
                session_id=session_id, timeout=retry_read_to,
                on_attempt=_log_stream_attempt, **retry_params
            )
        except (executor.AllFailedError, executor.NoProviderError) as fallback_exc:
            fb_error = str(getattr(fallback_exc, "last_error", fallback_exc))
            fb_last_status = getattr(
                getattr(fallback_exc, "last_error", None), "status_code", None,
            )
            log.warning(
                "responses stream: fallback %s also exhausted target=%s "
                "last_status=%s error=%s",
                fallback, target, fb_last_status, fb_error[:300],
            )
            usage_store.log(
                requested, target, routed_by, reason, status="exhausted",
                had_images=had_images,
                error=f"{error[:150]} || {fb_error[:150]}",
            )
            raise HTTPException(
                503,
                f"All providers exhausted for '{target}' "
                f"(primary last status {last_status}; fallback {fallback} last "
                f"status {fb_last_status}).",
                headers=_failure_provenance_headers(target, routed_by, reason),
            )
        # The fallback opened cleanly -- rebind routing metadata the rest of the
        # handler streams against (its own headers, ledger, _reopen, watchdog
        # frames must name the model the answer actually came from). The same
        # rewrite the chat non-stream path does on a successful fallback, plus
        # re-derived pacing so streaming recovery / idle-timeout budgets match
        # the model now riding the wire.
        attempts = list(getattr(exc, "attempts", [])) + list(attempts2)
        target = fallback
        target_providers = fallback_providers
        params = retry_params
        stream_idle_budget, stream_read_to = retry_idle_budget, retry_read_to
        reason = f"primary stream failed ({getattr(exc, 'last_error', exc)}); " \
                 f"fell back to {fallback}"
        routed_by = "fallback"

    account_id = key.id if key is not None else None
    row_id = usage_store.log(
        requested, target, routed_by, reason,
        latency_ms=(time.time() - started) * 1000.0, status="ok_stream",
        had_images=had_images, account_id=account_id, provider=prov.id,
        streamed=True,
    )
    log.info(
        "responses: streaming requested=%s target=%s provider=%s first_chunk_ms=%.1f attempts=%d",
        requested, target, prov.id, (time.time() - request_started) * 1000.0, len(attempts),
    )

    # Mid-flight recovery may land on a different model, but only when the
    # client asked the router to choose -- mirrors the chat handler's _reopen. A
    # turn that named `muse-spark-1.2-contributor-free` is retried on the same
    # model; `lingling-auto` is free to re-decide, which matters because the
    # usual reason a stream dies mid-flight is the model itself stalling.
    auto_routed = requested == config.MULTIMODEL_ID
    reroute = {"model": target, "reason": reason, "by": routed_by}

    def _reopen():
        """Open a replacement upstream stream for mid-flight recovery.

        Mirrors the chat handler's `_reopen`: a fresh exit IP through the
        executor, a re-decided model only when the turn was auto-routed, and
        effort re-resolved against the client's original label. The retry yields
        chat-completions SSE -- the bridge translates it to Responses events the
        same way it does for the original stream.
        """
        retry_model = target
        retry_params = params
        if auto_routed:
            alternative = dispatcher.fallback_model(
                catalog, had_images,
                exclude={target} | set(sampler.cooked_models()), messages=messages,
            )
            alt_providers = catalog.providers_for(alternative)
            if alternative and alternative != target and alt_providers:
                retry_model = alternative
                retry_params = dict(params)
                _resolve_effort(retry_params, retry_model, previous=original_effort)
                reroute.update({
                    "model": retry_model,
                    "reason": f"stream broke on {target}; rerouted to {retry_model}",
                    "by": "reroute",
                })
                log.warning(
                    "responses stream rerouting mid-flight: %s -> %s", target, retry_model,
                )
        providers_for_retry = catalog.providers_for(retry_model) or target_providers
        # A text-only retry target cannot see images the stalled vision model
        # could; swap them for the placeholder the non-streaming fallback uses,
        # or OpenCode answers 400 and the retry is wasted.
        retry_messages = _messages_for_model(messages, retry_model, had_images)
        # The retry may land on a different model: re-derive its thinking
        # patience so a reroute off a hidden-reasoning model does not carry the
        # long budget onto a normal one (and vice versa).
        retry_idle_budget, retry_read_to = _stream_pacing(retry_model, body)
        again, _prov, _key, _attempts = executor.execute_stream(
            retry_messages, retry_model, providers_for_retry,
            proxy_pool=proxy_pool, session_id=session_id, timeout=retry_read_to,
            on_attempt=_log_stream_attempt, **retry_params
        )
        return stream_idle.with_idle_timeout(again, retry_idle_budget, log)

    def _hold_for_egress():
        """Wait for a free exit before the mid-stream retry reopens (responses).

        Same decision as the chat path's pre-first-token wait, but running inside
        a live SSE response, so it emits keepalive comments rather than awaiting
        silently. Yields nothing when waiting cannot help.
        """
        yield from parking.hold_stream_for_egress(
            proxy_pool, config.EGRESS_WAIT_BUDGET, log,
        )

    def event_stream():
        seen: Dict[str, int] = {}
        # stream_guard retries once on a fresh exit IP when the upstream dies
        # mid-flight, and keeps completion honest for OpenCode's no-[DONE] wire:
        # a usage/cost frame flips outcome.completed (the bridge's choice-level
        # finish_reason check misses it), so a fully-successful turn that simply
        # omitted finish_reason is no longer filed stream_broken. The bridge
        # reports the outcome, so a recovered break is filed as ok_recovered and
        # an unrecoverable one as stream_broken -- no more silent ok_stream on a
        # turn the editor rendered half of.
        outcome = stream_guard.StreamOutcome()
        try:
            yield from responses_bridge.stream_events(
                stream_guard.guarded_stream(
                    open_stream=_reopen,
                    # Idle watchdog first: an upstream that stops speaking
                    # without closing would otherwise hold the turn open
                    # indefinitely. StreamStalled is treated by guarded_stream
                    # as a mid-flight death and retried once on a fresh exit IP.
                    # The budget is model-aware so a hidden-reasoning model
                    # (muse-spark) is not cut off mid-thought (see _stream_pacing).
                    first=stream_idle.with_idle_timeout(
                        stream_iter, stream_idle_budget, log),
                    outcome=outcome,
                    on_chunk=lambda raw: _harvest_stream_usage(raw, seen),
                    log=log,
                    enabled=recover,
                    hold=_hold_for_egress,
                    # The model the retry actually landed on, so the bridge's
                    # reset path (and the ledger) can name it; None when the
                    # retry did not move (same model, fresh exit IP).
                    retry_model=lambda: (
                        reroute["model"] if reroute["model"] != target else None
                    ),
                    # The resolved target so pacing_memory can learn a
                    # hidden-reasoning model from its wire behaviour (a chunk
                    # that carries reasoning tokens, or a stall before any
                    # visible content) and give it thinking patience next turn.
                    model_id=target,
                ),
                requested,
                outcome,
            )
        finally:
            if outcome.error:
                log.warning(
                    "responses stream broke target=%s provider=%s - %s",
                    target, prov.id, outcome.error,
                )
            if outcome.recovered:
                status = "ok_recovered"
            elif outcome.error:
                status = "stream_broken"
            else:
                status = None  # keep ok_stream
            usage_store.finalize(
                row_id,
                tokens_in=seen.get("tokens_in", 0),
                tokens_out=seen.get("tokens_out", 0),
                reasoning_tokens=seen.get("reasoning_tokens", 0),
                latency_ms=(time.time() - started) * 1000.0,
                status=status,
                error=outcome.error,
                # A mid-flight reroute means the answer came from a different
                # model than the row was opened with. Recording the original
                # would make the ledger name a model that produced nothing.
                routed_model=reroute["model"] if reroute["model"] != target else None,
                routed_by=reroute["by"] if reroute["model"] != target else None,
                reason=reroute["reason"] if reroute["model"] != target else None,
            )
            if outcome.recovered:
                log.info(
                    "responses: recovered target=%s attempts=%d",
                    reroute["model"], outcome.attempts,
                )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Lingling-Routed-Model": _header_safe(target),
            "X-Lingling-Routed-By": _header_safe(routed_by),
            "X-Lingling-Reason": _header_safe(reason),
            "X-Lingling-Provider": _header_safe(prov.id),
            "X-Lingling-Account": _header_safe(account_id or ""),
            # Mirrors the chat path: tells a client (and the dashboard) whether
            # mid-flight retry is opt-out for this turn (lingling_recover /
            # STREAM_RECOVERY) or this is a passthrough.
            "X-Lingling-Stream-Mode": "guarded" if recover else "passthrough",
        },
    )

@app.post("/v1/messages")
async def messages(request: Request):
    """Anthropic Messages entrypoint, for Claude Code.

    Kept separate from the Codex/Responses handler on purpose: the two wire
    formats share no shape, and folding them together is how one harness's edge
    case becomes the other's regression. Everything below the translation is the
    same machinery -- dispatcher, executor, WARP egress, parking, ledger.
    """
    request_started = time.time()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object.")
        requested, messages_in, params = messages_bridge.request_to_chat(body)
    except JSONDecodeError as exc:
        log.warning("messages invalid_json error=%s", exc)
        raise HTTPException(400, "Invalid JSON request body.")
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    stream = bool(body.get("stream"))
    # Reasoning only reaches the client when it asked for thinking. Anthropic
    # redacts thinking by default and Claude Code relies on that; Lingling cannot
    # redact plain reasoning text, so it drops it instead of flooding the terminal.
    show_thinking = cc_thinking.wants_thinking(body)
    # Claude Code identifies a conversation with its own header; falling back to
    # the metadata user_id keeps sticky routing working for clients that send it.
    session_id = request.headers.get("x-claude-code-session-id") or ""
    if not session_id:
        meta = body.get("metadata")
        if isinstance(meta, dict) and isinstance(meta.get("user_id"), str):
            session_id = meta["user_id"]
    had_images = vision_bridge.messages_have_images(messages_in)

    # Claude Code asks for Anthropic model ids and makes background calls on its
    # haiku alias; neither exists here, so they resolve to a free model by size
    # class before routing sees them.
    target = model_map.resolve(requested, catalog)
    if target == config.MULTIMODEL_ID:
        target, reason, routed_by = await run_in_threadpool(
            _run_dispatcher, messages_in, had_images, session_id
        )
    else:
        reason = f"claude code asked for {requested}"
        routed_by = "user" if target == model_map.strip_alias(requested) else "alias"

    target_providers = catalog.providers_for(target)
    if not target_providers:
        raise HTTPException(
            503, f"No provider available for model '{target}'.",
            headers=_failure_provenance_headers(target, routed_by, reason),
        )

    target_lm = catalog.by_id(target)
    if target_lm is not None and not target_lm.vision:
        messages_in = vision_bridge.strip_images_for_text_model(messages_in)

    original_effort = params.get("reasoning_effort")
    # Clamp the depth label to what this model actually publishes. Must happen
    # after routing: `low` is legal for ling and meaningless for deepseek, and
    # OpenCode answers 200 for a value it does not implement.
    _resolve_effort(params, target)

    if not stream:
        started = time.time()
        try:
            resp, prov, key, attempts = await _execute_with_egress_wait(
                executor.execute_nonstream,
                messages_in, target, target_providers, proxy_pool=proxy_pool,
                session_id=session_id, **params
            )
        except executor.AllFailedError as exc:
            # Same fallback the other two entrypoints do: answering from a
            # different free model beats handing Claude Code a hard failure,
            # which ends its turn.
            fallback = dispatcher.fallback_model(
                catalog, had_images, exclude={target} | set(sampler.cooked_models()), messages=messages_in,
            )
            fallback_providers = catalog.providers_for(fallback)
            if not (fallback and fallback != target and fallback_providers):
                usage_store.log(
                    requested, target, routed_by, reason, status="exhausted",
                    had_images=had_images, error=str(exc.last_error)[:300],
                )
                raise HTTPException(
                    503, f"All providers exhausted for '{target}'.",
                    headers=_failure_provenance_headers(target, routed_by, reason),
                )
            try:
                retry_params = dict(params)
                _resolve_effort(retry_params, fallback, previous=original_effort)
                resp, prov, key, attempts = await _execute_with_egress_wait(
                    executor.execute_nonstream,
                    _messages_for_model(messages_in, fallback, had_images),
                    fallback, fallback_providers, proxy_pool=proxy_pool,
                    session_id=session_id, **retry_params
                )
                target = fallback
                reason = f"primary model failed ({exc.last_error}); fell back to {fallback}"
                routed_by = "fallback"
            except (executor.AllFailedError, executor.NoProviderError) as fallback_exc:
                usage_store.log(
                    requested, target, routed_by, reason, status="exhausted",
                    had_images=had_images,
                    error=str(getattr(fallback_exc, "last_error", fallback_exc))[:300],
                )
                raise HTTPException(
                    503, f"All providers exhausted for '{target}'.",
                    headers=_failure_provenance_headers(target, routed_by, reason),
                )

        account_id = key.id if key is not None else None
        usage = extract_usage(resp)
        usage_store.log(
            requested, target, routed_by, reason,
            tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
            reasoning_tokens=usage.get("reasoning_tokens", 0),
            latency_ms=(time.time() - started) * 1000.0, status="ok",
            had_images=had_images, account_id=account_id, provider=prov.id,
        )
        out = messages_response.response_object(
            resp, requested, target, prov.id, show_thinking=show_thinking,
        )
        log.info(
            "messages: handled requested=%s target=%s provider=%s stream=false latency_ms=%.1f",
            requested, target, prov.id, (time.time() - request_started) * 1000.0,
        )
        return JSONResponse(out)

    # Hidden-reasoning models get a longer silent-thinking patience; see chat.
    stream_idle_budget, stream_read_to = _stream_pacing(target, body)
    started = time.time()
    try:
        stream_iter, prov, key, attempts = await _execute_with_egress_wait(
            executor.execute_stream,
            messages_in, target, target_providers, proxy_pool=proxy_pool,
            session_id=session_id, timeout=stream_read_to,
            on_attempt=_log_stream_attempt, **params
        )
    except (executor.AllFailedError, executor.NoProviderError) as exc:
        # Surface the actual upstream rejection so the 503 and the ledger name
        # the real cause (matches the chat/responses stream paths).
        error = str(getattr(exc, "last_error", exc))
        last_status = getattr(getattr(exc, "last_error", None), "status_code", None)
        log.warning(
            "messages stream: all upstream attempts failed target=%s last_status=%s "
            "error=%s",
            target, last_status, error[:300],
        )

        # Pre-HTTP-200 fallback: mirror the chat/responses stream paths and this
        # handler's own non-stream branch. execute_stream raises before any bytes
        # are on the wire, so a model swap is invisible to Claude Code. Without
        # this the stream path bare-503'd on a transiently-exhausted model while
        # the non-stream branch above recovered -- the streaming-vs-non-stream
        # fallback asymmetry upstream tracks as LiteLLM #25843. Mid-flight
        # recovery is deliberately NOT added here: Anthropic's streaming event
        # model has no "discard rendered deltas" marker and Claude Code honours
        # no synthetic reset, so reopening on a fresh exit would double-render the
        # partial answer -- the safe recovery window for Anthropic wire is
        # pre-200 only.
        fallback = dispatcher.fallback_model(
            catalog, had_images,
            exclude={target} | set(sampler.cooked_models()), messages=messages_in,
        )
        fallback_providers = catalog.providers_for(fallback) if fallback else None
        if not (fallback and fallback != target and fallback_providers):
            usage_store.log(
                requested, target, routed_by, reason, status="exhausted",
                had_images=had_images, error=error[:300],
            )
            raise HTTPException(
                503,
                f"No upstream stream started within {stream_read_to:g}s for '{target}' "
                f"(last upstream status {last_status}).",
                headers=_failure_provenance_headers(target, routed_by, reason),
            )
        log.warning(
            "messages stream rerouting %s -> %s (primary exhausted, last_status=%s)",
            target, fallback, last_status,
        )
        retry_params = dict(params)
        _resolve_effort(retry_params, fallback, previous=original_effort)
        retry_messages = _messages_for_model(messages_in, fallback, had_images)
        # Re-derive pacing so the idle watchdog is tuned to the fallback, not the
        # dead model (a hidden-reasoning primary's long budget would otherwise
        # carry onto a normal fallback).
        retry_idle_budget, retry_read_to = _stream_pacing(fallback, body)
        try:
            stream_iter, prov, key, attempts2 = await _execute_with_egress_wait(
                executor.execute_stream,
                retry_messages, fallback, fallback_providers, proxy_pool=proxy_pool,
                session_id=session_id, timeout=retry_read_to,
                on_attempt=_log_stream_attempt, **retry_params
            )
        except (executor.AllFailedError, executor.NoProviderError) as fallback_exc:
            fb_error = str(getattr(fallback_exc, "last_error", fallback_exc))
            fb_last_status = getattr(
                getattr(fallback_exc, "last_error", None), "status_code", None,
            )
            log.warning(
                "messages stream: fallback %s also exhausted target=%s last_status=%s "
                "error=%s",
                fallback, target, fb_last_status, fb_error[:300],
            )
            usage_store.log(
                requested, target, routed_by, reason, status="exhausted",
                had_images=had_images,
                error=f"{error[:150]} || {fb_error[:150]}",
            )
            raise HTTPException(
                503,
                f"All providers exhausted for '{target}' "
                f"(primary last status {last_status}; fallback {fallback} last "
                f"status {fb_last_status}).",
                headers=_failure_provenance_headers(target, routed_by, reason),
            )
        # The fallback opened cleanly -- rebind the routing metadata the rest of
        # the handler streams against (the row opened below, the SSE headers and
        # the finalize must all name the model the answer came from). Same
        # rewrite the non-stream branch above does on a successful fallback, plus
        # re-derived pacing so the live stream's idle budget matches the model now
        # riding the wire.
        attempts = list(getattr(exc, "attempts", [])) + list(attempts2)
        target = fallback
        target_providers = fallback_providers
        params = retry_params
        stream_idle_budget, stream_read_to = retry_idle_budget, retry_read_to
        reason = f"primary stream failed ({getattr(exc, 'last_error', exc)}); " \
                 f"fell back to {fallback}"
        routed_by = "fallback"

    account_id = key.id if key is not None else None
    row_id = usage_store.log(
        requested, target, routed_by, reason,
        latency_ms=(time.time() - started) * 1000.0, status="ok_stream",
        had_images=had_images, account_id=account_id, provider=prov.id,
        streamed=True,
    )
    log.info(
        "messages: streaming requested=%s target=%s provider=%s first_chunk_ms=%.1f",
        requested, target, prov.id, (time.time() - request_started) * 1000.0,
    )

    def event_stream():
        seen: Dict[str, int] = {}
        outcome = stream_guard.StreamOutcome()
        try:
            def tracked():
                # Idle watchdog first: an upstream that stops speaking without
                # closing would otherwise hold the turn open indefinitely -- one
                # measured session sat 885s with zero tokens before giving up.
                # StreamStalled is caught below and ends the turn honestly rather
                # than propagating into a broken SSE response. The budget is
                # model-aware so a hidden-reasoning model that thinks silently is
                # not cut off mid-thought (see _stream_pacing).
                guarded = stream_idle.with_idle_timeout(
                    stream_iter, stream_idle_budget, log)
                for raw in guarded:
                    frame = raw if isinstance(raw, bytes) else raw.encode("utf-8")
                    _harvest_stream_usage(frame, seen)
                    yield frame
            yield from messages_stream.stream_events(
                tracked(), requested, outcome, show_thinking=show_thinking,
            )
        finally:
            if outcome.error:
                log.warning(
                    "messages stream broke target=%s provider=%s - %s",
                    target, prov.id, outcome.error,
                )
            usage_store.finalize(
                row_id,
                tokens_in=seen.get("tokens_in", 0),
                tokens_out=seen.get("tokens_out", 0),
                reasoning_tokens=seen.get("reasoning_tokens", 0),
                latency_ms=(time.time() - started) * 1000.0,
                status=None if outcome.completed else "stream_broken",
                error=outcome.error,
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Lingling-Routed-Model": _header_safe(target),
            "X-Lingling-Routed-By": _header_safe(routed_by),
            "X-Lingling-Reason": _header_safe(reason),
            "X-Lingling-Provider": _header_safe(prov.id),
            "X-Lingling-Account": _header_safe(account_id or ""),
            # Parity with the chat/responses streams. Always "passthrough" here:
            # Anthropic's streaming event model has no discard-and-replay marker
            # and Claude Code honours no synthetic reset, so this path never
            # mid-flight-retries -- recovery is pre-200 only (see the catch above).
            "X-Lingling-Stream-Mode": "passthrough",
        },
    )


# ---------------------------------------------------------------------------
# Frontend (single-page app served from ../frontend, if present)
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        """Serve the dashboard and issue it a session cookie.

        HttpOnly so page scripts (and any XSS) cannot read it; SameSite=strict
        so browsers never attach it to a cross-site request. This is what the
        dashboard authenticates with -- no key needs to be embedded in the page.
        """
        resp = FileResponse(str(FRONTEND_DIR / "index.html"))
        resp.set_cookie(
            auth.COOKIE_NAME,
            auth.mint_session(),
            max_age=config.SESSION_TTL_SECONDS,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return resp


class _QuietPollFilter(logging.Filter):
    """Drop successful dashboard polls from the terminal access log.

    The frontend polls a handful of read-only endpoints every 1-8s, and each
    request is a 200 that floods the backend terminal -- probe verdicts,
    heals and bootstrap lines get buried under a wall of access lines nobody
    can read. This filter drops those high-frequency GETs while leaving the
    log honest: errors (any 4xx/5xx) and real API calls (POST completions,
    the manual Probe button, ...) still surface.
    """

    # Exact paths that poll on a beat -- suppress only when GET succeeds.
    _QUIET_EXACT = {"", "/", "/api/warp", "/api/proxies",
                    "/api/health", "/api/models"}
    # Path prefixes whose variants all poll (e.g. /api/usage/since/100).
    _QUIET_PREFIX = ("/api/warp/probe", "/api/usage", "/static/")

    def filter(self, record):  # noqa: A003 -- logging's own API name
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        # uvicorn.access args: (client_addr, method, path, http_version, status).
        try:
            status = int(args[4])
        except (TypeError, ValueError):
            return True
        if not (200 <= status < 400):
            return True  # never hide an error
        if str(args[1]) != "GET":
            return True  # chat completions, manual Probe (POST), ...
        path = str(args[2])
        if path in self._QUIET_EXACT or path.startswith(self._QUIET_PREFIX):
            return False
        return True


def _build_log_config() -> Dict[str, Any]:
    """Uvicorn's default LOGGING_CONFIG + the quiet-poll filter + a file handler.

    The file handler mirrors every record to ``data/backend.log`` so the launcher
    no longer redirects stdout there: the minimized window shows the live
    (minimal, WARP-verbose-off) output instead of a blank pane, while the file
    still holds a runtime crash trace after the window closes. ``uvicorn`` and
    ``uvicorn.access`` keep uvicorn's default ``propagate = False`` (so the root
    handler set below cannot double-emit them); ``uvicorn.error`` has no own
    handlers and propagates up to ``uvicorn`` for one pass through console + file.
    The file handler carries the quiet-poll filter too, so dashboard GET-200 polls
    do not flood the file. Root catches third-party libs (httpx/urllib3/socksio)
    at warning level only, so the window stays minimal.
    """
    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault("filters", {})["quiet_poll"] = {"()": _QuietPollFilter}
    cfg["handlers"]["access"]["filters"] = ["quiet_poll"]

    config.ensure_data_dir()
    cfg["handlers"]["file"] = {
        "class": "logging.FileHandler",
        "formatter": "default",
        "filename": str(config.DATA_DIR / "backend.log"),
        "mode": "a",
        "encoding": "utf-8",
        "filters": ["quiet_poll"],
    }
    cfg["root"] = {"handlers": ["default", "file"], "level": "WARNING"}
    uvi = cfg["loggers"]["uvicorn"]
    uvi["handlers"] = uvi.get("handlers", []) + ["file"]
    acc = cfg["loggers"]["uvicorn.access"]
    acc["handlers"] = acc.get("handlers", []) + ["file"]
    return cfg


if __name__ == "__main__":
    """Run the gateway directly: `python app.py`.

    Host and port are overridable via LINGLING_HOST / LINGLING_PORT; the default
    binds loopback only.
    """
    import os

    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.getenv("LINGLING_HOST", "127.0.0.1"),
        port=int(os.getenv("LINGLING_PORT", "8000")),
        log_level=os.getenv("LINGLING_LOG_LEVEL", "info"),
        log_config=_build_log_config(),
    )
