"""Lingling gateway -- a dumb routing proxy for free AI models.

Takes whatever an OpenAI-compatible client (Cline, Claude Code, Codex) sends and
routes it to free models on OpenCode, defeating the per-IP free-tier limit via a
rotating pool of egress proxies (Cloudflare WARP by default). The client owns
all prompting; Lingling only routes. See the README for the endpoint list.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import shutil
import time
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Dict, List, Optional

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
from routing import normalize
from routing import parking
from routing import stream_guard
from routing import stream_idle
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
from warp.health import _socks5_http_probe
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
            log.info("startup: synced %d running WARP proxies into the pool", added)
        elif warp_manager.status()["proxies_running"] > 0:
            log.info("startup: WARP proxies already in pool (%d running)", warp_manager.status()["proxies_running"])
        else:
            log.info("startup: no running WARP proxies found to sync")
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
            "bootstrap: Tor lanes ready (%d started, %d failed, %d in pool)",
            result.get("started", 0), result.get("failed", 0), added,
        )
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
            log.info("bootstrap: registering WARP identities (one-time, slow)")
            warp_manager.setup_identities(log=log.info)
        _start_warp_at_startup()
        log.info(
            "bootstrap: WARP ready (%d in pool)",
            proxy_pool.status().get("total", 0),
        )
        # --- startup probe: test every proxy with a real model request ---
        if config.PROBE_ON_STARTUP and len(proxy_pool) > 0:
            try:
                summary = warp_probe.probe_all(
                    proxy_pool, log=log.info,
                )
                # Heal dead (expired) and rate-limited proxies.
                expired_healed = warp_probe.heal_expired(
                    proxy_pool, summary, warp_manager, log=log.info,
                )
                rl_healed = warp_probe.heal_rate_limited(
                    proxy_pool, summary, warp_manager, log=log.info,
                )
                spread_moved = warp_probe.spread_distinct_exits(
                    proxy_pool, summary, warp_manager, log=log.info,
                )
                if expired_healed or rl_healed or spread_moved:
                    log.info(
                        "probe: healed %d expired identities + %d burned exits, "
                        "spread %d slots onto unused exits",
                        expired_healed, rl_healed, spread_moved,
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
                # Log a single summary row so the dashboard shows one entry.
                try:
                    usage_store.log(
                        "startup-probe", "startup-probe", "probe",
                        reason=(
                            f"{summary.healthy}/{summary.total} healthy, "
                            f"{summary.rate_limited} rate-limited, "
                            f"{summary.dead} dead"
                        ),
                        status="ok" if summary.healthy > 0 else "exhausted",
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
        log.info("threadpool: raised sync executor ceiling to %d", config.THREADPOOL_MAX)
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
# OpenAI-compatible fields Lingling manages itself, excluded from passthrough.
# `**params` is splatted into the executor and provider calls, so a body field
# colliding with a positional/keyword arg there raised TypeError -> 500.
# `timeout` was the realistic one: a plausible field to send, and it killed the
# streaming path while the non-streaming path survived (different call shape).
_PASSTHROUGH_EXCLUDE = frozenset({
    "model", "messages", "stream", "lingling", "session_id", "lingling_recover",
    # executor.execute_nonstream / execute_stream signature
    "model_id", "providers", "proxy_pool", "timeout",
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

    Returns the number of *new* WARP proxies added to the pool. Existing
    ``warp-N`` entries have their URL refreshed in place so port migrations are
    reflected immediately.
    """
    if not warp_manager.tools_ready() or warp_manager.status()["identities_registered"] == 0:
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
    """The single hard-coded model: the multi-model router itself."""
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

    Codex/Cline and other OpenAI-compatible clients normally call `/v1/models` and
    `/v1/chat/completions`; this makes a browser/manual probe of the base URL
    less confusing.
    """
    return {
        "object": "api.info",
        "name": "Lingling OpenAI-compatible API",
        "version": VERSION,
        "endpoints": {
            "models": "/v1/models",
            "chat_completions": "/v1/chat/completions",
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


@app.post("/api/providers/{pid}/keys")
def add_key(pid: str, body: KeyIn) -> Dict[str, Any]:
    prov = _get_provider(pid)
    secret = body.resolved_secret().strip()
    if not secret:
        raise HTTPException(400, "a credential (secret/api_key/key/token) is required")
    key = prov.keys.add(secret, body.label, body.id)
    return {"added": key.status(), "pool": prov.keys.status()}


@app.delete("/api/providers/{pid}/keys/{kid}")
def remove_key(pid: str, kid: str) -> Dict[str, Any]:
    prov = _get_provider(pid)
    removed = prov.keys.remove(kid)
    if not removed:
        raise HTTPException(404, f"key '{kid}' not found")
    return {"removed": kid, "pool": prov.keys.status()}


# OpenCode shortcuts (the OpenCode key router), kept for convenience.
@app.get("/api/accounts")
def get_accounts() -> Dict[str, Any]:
    return _get_provider("opencode").keys.status()


@app.post("/api/accounts")
def add_account(body: KeyIn) -> Dict[str, Any]:
    return add_key("opencode", body)


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
def warp_health(probe: bool = False) -> Dict[str, Any]:
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
                http_probe_ok = _socks5_http_probe(
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
            ),
            status="ok" if summary.healthy > 0 else "exhausted",
            provider="warp",
        )
    except Exception:  # noqa: BLE001
        pass
    return {"busy": False, **summary.to_dict()}


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


def _dump_rc_failure(messages: Any, exc: Exception) -> None:
    """Snapshot a request the upstream rejected over ``reasoning_content``.

    Reasoning models require the field on replayed assistant turns, and clients
    drop or mangle it in shapes that are hard to reproduce synthetically. The
    snapshot lets a still-failing request be inspected offline.
    """
    text = str(getattr(exc, "last_error", exc))
    if "reasoning_content" not in text and "invalid json" not in text:
        return
    try:
        snapshot = config.DATA_DIR / "debug_rc_400.json"
        with open(snapshot, "w", encoding="utf-8") as fh:
            json.dump({"error": text[:300], "messages": messages}, fh,
                      ensure_ascii=False, indent=1)
        log.warning("rc400: upstream rejected reasoning_content; snapshot -> %s", snapshot)
    except Exception:  # noqa: BLE001 - diagnostics must never break the request
        pass


def _flag_model_retired(exc: Exception, model_id: str) -> bool:
    """Hide a model from the catalog when the upstream retired its free tier.

    OpenCode advertises a ``-free`` model even after it stops serving it, in
    three shapes (verified live):

    * 400 "unavailable for free" -- the classic retired-tier answer;
    * 401 "Model <id> is not supported" -- dropped from the model list;
    * 403 with a bare ``{"model": "<id>"}`` body -- gated behind a paid tier.

    On that failure the model is recorded as retired (persisted, TTL), dropping
    it from /v1/models, the dashboard, dispatcher candidates and the
    Codex/Claude listings.
    """
    err = getattr(exc, "last_error", None)
    detail = str(getattr(err, "detail", ""))
    code = getattr(err, "status_code", None)
    if code == 400 and "unavailable" in detail.lower():
        pass
    elif code == 401 and "not supported" in detail.lower():
        pass
    elif code == 403 and detail.strip().startswith("{") and f'"{model_id}"' in detail:
        pass
    else:
        return False
    try:
        catalog.mark_unavailable(model_id)
    except Exception:  # noqa: BLE001
        return False
    log.info("catalog: retired model %s (upstream no longer serves it free)", model_id)
    return True


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
        _dump_rc_failure(args[0] if args else [], exc)
        waited = await parking.wait_for_egress(pool, config.EGRESS_WAIT_BUDGET, log)
        if not waited:
            raise
        log.info("egress: waited %.1fs for a fresh exit, retrying the request", waited)
        return await run_in_threadpool(fn, *args, **kwargs)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    request_started = time.time()
    client = request.client.host if request.client else "unknown"

    # Authorisation already happened in the _gate middleware.
    actor = getattr(request.state, "actor", "open")

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
                if catalog.is_unavailable(target):
                    raise HTTPException(400, f"Model {requested!r} is no longer served for free by OpenCode; pick from /v1/models.")
                raise HTTPException(400, f"Unknown or unsupported model: {requested!r}")
        reason = "user requested"
        routed_by = "user"

    target_providers = catalog.providers_for(target)
    if not target_providers:
        raise HTTPException(503, f"No provider available for model '{target}'.")

    target_lm = catalog.by_id(target)
    if target_lm is not None and not target_lm.vision:
        messages = vision_bridge.strip_images_for_text_model(messages)
    messages = normalize.normalize_reasoning_content(messages)

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
                catalog, had_images, exclude={target}, messages=messages,
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
                    raise HTTPException(503, f"All providers exhausted for '{target}'.")
            else:
                usage_store.log(
                    requested, target, routed_by, reason, status="exhausted",
                    had_images=had_images, error=str(exc.last_error)[:300],
                )
                raise HTTPException(503, f"All providers exhausted for '{target}'.")

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
            "chat complete requested=%s target=%s provider=%s stream=false latency_ms=%.1f",
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
    started = time.time()
    try:
        # execute_stream blocks until the first upstream chunk arrives (up to
        # STREAM_FIRST_TOKEN_TIMEOUT), so it cannot run on the event loop. The
        # generator it returns is safe: StreamingResponse iterates sync
        # iterators in a worker thread already.
        stream_iter, prov, key, attempts = await _execute_with_egress_wait(
            executor.execute_stream,
            messages, target, target_providers, proxy_pool=proxy_pool,
            session_id=session_id, timeout=config.STREAM_FIRST_TOKEN_TIMEOUT, **params
        )
    except (executor.AllFailedError, executor.NoProviderError) as exc:
        error = str(getattr(exc, "last_error", exc))
        usage_store.log(
            requested, target, routed_by, reason, status="exhausted",
            had_images=had_images, error=error[:300],
        )
        raise HTTPException(503, f"No upstream stream started within {config.STREAM_FIRST_TOKEN_TIMEOUT:g}s for '{target}'.")

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
        "chat stream started requested=%s target=%s provider=%s first_chunk_ms=%.1f",
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
                catalog, had_images, exclude={target}, messages=messages,
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
        again, _prov, _key, _attempts = executor.execute_stream(
            retry_messages, retry_model, providers_for_retry, proxy_pool=proxy_pool,
            session_id=session_id, timeout=config.STREAM_FIRST_TOKEN_TIMEOUT,
            **retry_params
        )
        return stream_idle.with_idle_timeout(again, config.STREAM_IDLE_TIMEOUT, log)

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
                # already treats as a mid-flight death and retries once.
                first=stream_idle.with_idle_timeout(
                    stream_iter, config.STREAM_IDLE_TIMEOUT, log),
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
                    "chat stream recovered target=%s attempts=%d",
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
        raise HTTPException(503, f"No provider available for model '{target}'.")
    target_lm = catalog.by_id(target)
    if target_lm is not None and not target_lm.vision:
        messages = vision_bridge.strip_images_for_text_model(messages)
    messages = normalize.normalize_reasoning_content(messages)
    _resolve_effort(params, target)
    original_effort = params.get("reasoning_effort")

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
                catalog, had_images, exclude={target}, messages=messages,
            )
            fallback_providers = catalog.providers_for(fallback)
            if not (fallback and fallback != target and fallback_providers):
                usage_store.log(
                    requested, target, routed_by, reason, status="exhausted",
                    had_images=had_images, error=str(exc.last_error)[:300],
                )
                raise HTTPException(503, f"All providers exhausted for '{target}'.")
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
                raise HTTPException(503, f"All providers exhausted for '{target}'.")
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
            "responses complete requested=%s target=%s provider=%s stream=false latency_ms=%.1f",
            requested, target, prov.id, (time.time() - request_started) * 1000.0,
        )
        return JSONResponse(out)

    started = time.time()
    try:
        stream_iter, prov, key, attempts = await _execute_with_egress_wait(
            executor.execute_stream,
            messages, target, target_providers, proxy_pool=proxy_pool,
            session_id=session_id, timeout=config.STREAM_FIRST_TOKEN_TIMEOUT, **params
        )
    except (executor.AllFailedError, executor.NoProviderError) as exc:
        error = str(getattr(exc, "last_error", exc))
        usage_store.log(
            requested, target, routed_by, reason, status="exhausted",
            had_images=had_images, error=error[:300],
        )
        raise HTTPException(503, f"No upstream stream started within {config.STREAM_FIRST_TOKEN_TIMEOUT:g}s for '{target}'.")

    account_id = key.id if key is not None else None
    row_id = usage_store.log(
        requested, target, routed_by, reason,
        latency_ms=(time.time() - started) * 1000.0, status="ok_stream",
        had_images=had_images, account_id=account_id, provider=prov.id,
        streamed=True,
    )
    log.info(
        "responses stream started requested=%s target=%s provider=%s first_chunk_ms=%.1f attempts=%d",
        requested, target, prov.id, (time.time() - request_started) * 1000.0, len(attempts),
    )

    def event_stream():
        seen: Dict[str, int] = {}
        # The bridge reports whether the upstream ever said finish_reason. A
        # stream that dies mid-flight would otherwise stay filed as ok_stream,
        # which is exactly the silent-truncation case stream_guard exists to
        # surface on the chat path.
        outcome = stream_guard.StreamOutcome()
        try:
            def tracked():
                # Idle watchdog first: an upstream that stops speaking without
                # closing would otherwise hold the turn open indefinitely.
                # StreamStalled is caught by stream_events and ends the turn
                # honestly rather than propagating into a broken SSE response.
                guarded = stream_idle.with_idle_timeout(
                    stream_iter, config.STREAM_IDLE_TIMEOUT, log)
                for raw in guarded:
                    _harvest_stream_usage(raw if isinstance(raw, bytes) else raw.encode("utf-8"), seen)
                    yield raw
            yield from responses_bridge.stream_events(tracked(), requested, outcome)
        finally:
            if outcome.error:
                log.warning(
                    "responses stream broke target=%s provider=%s - %s",
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
        raise HTTPException(503, f"No provider available for model '{target}'.")

    target_lm = catalog.by_id(target)
    if target_lm is not None and not target_lm.vision:
        messages_in = vision_bridge.strip_images_for_text_model(messages_in)
    messages_in = normalize.normalize_reasoning_content(messages_in)

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
                catalog, had_images, exclude={target}, messages=messages_in,
            )
            fallback_providers = catalog.providers_for(fallback)
            if not (fallback and fallback != target and fallback_providers):
                usage_store.log(
                    requested, target, routed_by, reason, status="exhausted",
                    had_images=had_images, error=str(exc.last_error)[:300],
                )
                raise HTTPException(503, f"All providers exhausted for '{target}'.")
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
                raise HTTPException(503, f"All providers exhausted for '{target}'.")

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
            "messages complete requested=%s target=%s provider=%s stream=false latency_ms=%.1f",
            requested, target, prov.id, (time.time() - request_started) * 1000.0,
        )
        return JSONResponse(out)

    started = time.time()
    try:
        stream_iter, prov, key, attempts = await _execute_with_egress_wait(
            executor.execute_stream,
            messages_in, target, target_providers, proxy_pool=proxy_pool,
            session_id=session_id, timeout=config.STREAM_FIRST_TOKEN_TIMEOUT, **params
        )
    except (executor.AllFailedError, executor.NoProviderError) as exc:
        error = str(getattr(exc, "last_error", exc))
        usage_store.log(
            requested, target, routed_by, reason, status="exhausted",
            had_images=had_images, error=error[:300],
        )
        raise HTTPException(503, f"No upstream stream started within {config.STREAM_FIRST_TOKEN_TIMEOUT:g}s for '{target}'.")

    account_id = key.id if key is not None else None
    row_id = usage_store.log(
        requested, target, routed_by, reason,
        latency_ms=(time.time() - started) * 1000.0, status="ok_stream",
        had_images=had_images, account_id=account_id, provider=prov.id,
        streamed=True,
    )
    log.info(
        "messages stream started requested=%s target=%s provider=%s first_chunk_ms=%.1f",
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
                # than propagating into a broken SSE response.
                guarded = stream_idle.with_idle_timeout(
                    stream_iter, config.STREAM_IDLE_TIMEOUT, log)
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
    )
