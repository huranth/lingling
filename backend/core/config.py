"""Central configuration for the Lingling routing proxy.

Lingling is a dumb routing layer: it takes whatever an OpenAI-compatible
client (Cline, Claude Code, any harness) sends and routes it to free models
on OpenCode, defeating OpenCode's per-IP free-tier limit via a rotating pool
of egress proxies (Cloudflare WARP identities by default). The client owns
all prompting; Lingling only routes.

All settings can be overridden with environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Shared upstream metadata
# ---------------------------------------------------------------------------
# models.dev provides freely-licensed capability metadata (modalities, pricing,
# context limits) used to enrich every provider's models.
MODELS_DEV_API = os.getenv("LINGLING_MODELS_DEV", "https://models.dev/api.json")

# ---------------------------------------------------------------------------
# Multi-model dispatcher
# ---------------------------------------------------------------------------
MULTIMODEL_ID = os.getenv("LINGLING_MULTIMODEL_ID", "lingling-auto")
MULTIMODEL_NAME = os.getenv("LINGLING_MULTIMODEL_NAME", "Lingling Auto")
MULTIMODEL_DESCRIPTION = (
    "Routes every request to the best free model across all providers "
    "automatically. Attaching an image restricts routing to vision-capable "
    "models, and load fails over across providers and keys on rate limits."
)
# The model that makes the routing decision. Must be free, fast, large-context
# and text-only. deepseek-v4-flash-free fits and is served by multiple providers.
DISPATCHER_MODEL = os.getenv("LINGLING_DISPATCHER_MODEL", "deepseek-v4-flash-free")

# ---------------------------------------------------------------------------
# Provider: OpenCode (primary)
# ---------------------------------------------------------------------------
OPENCODE_BASE_URL = os.getenv(
    "LINGLING_OPENCODE_BASE", "https://opencode.ai/zen/v1"
).rstrip("/")
# OpenCode account keys (the rotation pool / key router).
OPENCODE_ACCOUNTS_FILE = Path(
    os.getenv("LINGLING_ACCOUNTS_FILE", str(_BACKEND_DIR / "accounts.json"))
)

# ---------------------------------------------------------------------------
# Egress proxy pool (for OpenCode's IP-based free-tier limits)
# ---------------------------------------------------------------------------
# OpenCode rate-limits its free tier by the connecting IP, not by key. Routing
# OpenCode requests through a rotating pool of egress proxies defeats that limit:
# when one proxy's IP is burned (429), the pool cools it down and the next
# request uses a different IP. Proxies are loaded from a JSON file (same shape as
# accounts.json: a list of {"url": "..."} or bare strings) and/or a comma-separated
# env var. When the pool is empty, Lingling connects directly (backward compatible).
PROXIES_FILE = Path(
    os.getenv("LINGLING_PROXIES_FILE", str(_BACKEND_DIR / "proxies.json"))
)
# Quick inline setup: LINGLING_PROXIES="http://host:port,socks5://host:port"
PROXIES_ENV = os.getenv("LINGLING_PROXIES", "")
# Sticky sessions keep one conversation on one exit IP. Off by default, because
# it fights the reason the pool exists: OpenCode meters the free tier per IP, and
# a coding agent sends one stable session id for its whole run -- so pinning made
# a single identity absorb an entire session's quota while the other nine idled.
# Turn it on only if an upstream needs per-conversation IP affinity.
PROXY_STICKY_SESSIONS = os.getenv("LINGLING_PROXY_STICKY", "0") not in ("0", "false", "False", "")
PROXY_COOLDOWN_BASE_MS = int(os.getenv("LINGLING_PROXY_COOLDOWN_BASE_MS", "1000"))
PROXY_COOLDOWN_MAX_MS = int(os.getenv("LINGLING_PROXY_COOLDOWN_MAX_MS", "60000"))
# Interactive chat must not wait through an entire egress pool. A single failed
# proxy is recorded and cooled; subsequent requests can use another proxy.
PROXY_MAX_ATTEMPTS_PER_REQUEST = int(os.getenv("LINGLING_PROXY_MAX_ATTEMPTS", "5"))
# Whether the fast chat models may bypass the egress pool. Off by default: the
# dispatcher runs on every lingling-auto request and picks ling-3.0-flash-free
# for most casual chat, so exempting them meant the two hottest paths egressed
# from the real IP -- defeating the point of the pool. The executor cools a dead
# proxy and retries another, and PROXY_CONNECT_TIMEOUT bounds the stall.
FAST_MODELS_DIRECT = os.getenv("LINGLING_FAST_MODELS_DIRECT", "0") not in ("0", "false", "False", "")

# ---------------------------------------------------------------------------
# Cloudflare WARP rotation (free, unlimited -- defeats OpenCode's IP limit).
# How many free WARP identities to register. Each becomes one local SOCKS5
# proxy on 127.0.0.1:51001+. More identities = more IPs before cooldown bites.
# ---------------------------------------------------------------------------
WARP_IDENTITY_COUNT = int(os.getenv("LINGLING_WARP_COUNT", "10"))

# ---------------------------------------------------------------------------
COOLDOWN_BASE_MS = int(os.getenv("LINGLING_COOLDOWN_BASE_MS", "2000"))
COOLDOWN_MAX_MS = int(os.getenv("LINGLING_COOLDOWN_MAX_MS", "30000"))

# ---------------------------------------------------------------------------
# Catalog cache
# ---------------------------------------------------------------------------
CATALOG_TTL_SECONDS = int(os.getenv("LINGLING_CATALOG_TTL", "600"))
# How long to wait before retrying an upstream /models fetch that produced an
# empty catalog. Deliberately far below CATALOG_TTL_SECONDS: a recovering
# upstream should be picked up quickly, but not re-fetched on every request.
CATALOG_RETRY_SECONDS = int(os.getenv("LINGLING_CATALOG_RETRY", "30"))

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.getenv("LINGLING_DATA_DIR", str(_BACKEND_DIR / "data")))
USAGE_DB = DATA_DIR / "usage.db"

RETIRED_MODELS_FILE = Path(
    os.getenv("LINGLING_RETIRED_MODELS_FILE",
              str(DATA_DIR / "retired_models.json"))
)
# How long an advertised-`-free` model that refused to serve stays hidden before
# being offered and re-checked. OpenCode has occasionally restored a free tier,
# so retired is temporary, not permanent. Learned at runtime (see
# catalog.mark_unavailable); persisted so the gateway and the Codex catalog
# generator agree on which models are actually servable.
RETIRED_MODEL_TTL_DAYS = int(os.getenv("LINGLING_RETIRED_MODEL_TTL_DAYS", "7"))
# Models already known to have been dropped by the upstream, seeded so they are
# hidden from the very first startup instead of only after the first runtime
# 400. OpenCode keeps advertising retired models in /models for a long time,
# so "dynamic fetching" alone cannot know they are dead.
# Comma-separated; set to "" to clear every seed. Runtime-learned retirements
# (RETIRED_MODELS_FILE) are still merged on top of these at every startup.
RETIRED_MODELS_SEED = os.getenv("LINGLING_RETIRED_MODELS", "ling-3.0-flash-free")


def retired_seed_ids() -> frozenset:
    """The known-dead model ids hidden from the catalog even before first use.

    Unlike runtime-learned retirements (which cool down after
    ``RETIRED_MODEL_TTL_DAYS``), seeded ids are re-stamped ``now`` on every
    catalog load, so they stay hidden until the operator unseeds them.
    """
    return frozenset(m.strip() for m in RETIRED_MODELS_SEED.split(",") if m.strip())

# ---------------------------------------------------------------------------
# User API keys (for authenticating clients like Cline / Claude Code)
# ---------------------------------------------------------------------------
# Keys are stored in this JSON file under DATA_DIR. Each key is a random
# ``ll_<32 hex chars>`` token. Clients send ``Authorization: Bearer ll_...``
# (or ``x-api-key: ll_...``) to /v1/chat/completions.
API_KEYS_FILE = Path(
    os.getenv("LINGLING_API_KEYS_FILE", str(DATA_DIR / "api_keys.json"))
)
# When True (default), /v1/chat/completions requires a valid user API key.
# Set LINGLING_REQUIRE_KEY=0 to disable auth (open local gateway).
REQUIRE_API_KEY = os.getenv("LINGLING_REQUIRE_KEY", "1") not in ("0", "false", "False", "")

# ---------------------------------------------------------------------------
# Dashboard sessions + CORS
# ---------------------------------------------------------------------------
# The dashboard authenticates with a signed, HttpOnly session cookie issued when
# it loads `/`. The signing secret is per-process and never written to disk, so
# a restart invalidates outstanding sessions. This replaces the old
# `sec-fetch-site` header check, which any non-browser client could forge.
SESSION_TTL_SECONDS = int(os.getenv("LINGLING_SESSION_TTL", str(12 * 3600)))
# Ports the dashboard may legitimately be served from -- used to build the CORS
# allow-list. A wildcard would let any page on the internet call /api/* from a
# visitor's browser.
DASHBOARD_PORTS = [
    p.strip() for p in os.getenv("LINGLING_DASHBOARD_PORTS", "8000,8008,8080").split(",")
    if p.strip()
]
# Extra origins, comma-separated, for reverse-proxy or LAN setups.
ALLOWED_ORIGINS_ENV = os.getenv("LINGLING_ALLOWED_ORIGINS", "")

# ---------------------------------------------------------------------------
# Startup bootstrap
# ---------------------------------------------------------------------------
# When set, the server registers and starts the WARP identity pool itself during
# startup. `start.bat` used to do this by POSTing /api/warp/setup and
# /api/warp/start with curl, but those routes are authenticated now -- the
# launcher would have needed a credential to bootstrap the server it just
# started. Doing it in-process removes that entirely.
BOOTSTRAP_WARP = os.getenv("LINGLING_BOOTSTRAP_WARP", "0") not in ("0", "false", "False", "")

# ---------------------------------------------------------------------------
# Streaming recovery
# ---------------------------------------------------------------------------
# A stream that dies after the first chunk cannot be failed over at the HTTP
# level (the 200 is already sent). When enabled, Lingling keeps enough state to
# retry once on a fresh exit IP and tells the client to discard the partial
# answer via a `lingling_reset` frame. Clients that ignore the marker would see
# the answer twice, so this is opt-out per request with
# `{"lingling_recover": false}` and globally with LINGLING_STREAM_RECOVERY=0.
STREAM_RECOVERY = os.getenv("LINGLING_STREAM_RECOVERY", "1") not in ("0", "false", "False", "")

# Longest an open stream may go without a usable frame before it is treated as
# broken. This catches the case neither the first-token budget nor stream_guard
# covers: an upstream that stops speaking while the socket stays open. Measured in
# a real session -- one request sat for 885 seconds with zero tokens before being
# filed as broken, which to the user is a hang.
#
# Deliberately generous. A thinking free model streams `reasoning_content`
# continuously (1203 frames for one measured turn), so a long think keeps frames
# flowing; only a true stall goes quiet. 0 disables the watchdog.
STREAM_IDLE_TIMEOUT = float(os.getenv("LINGLING_STREAM_IDLE_TIMEOUT", "90"))

# ---------------------------------------------------------------------------
# Waiting out an exhausted egress pool
# ---------------------------------------------------------------------------
# When every WARP exit is in cooldown at once, the honest answer is "come back in
# a moment", not HTTP 503. A 503 makes Cline and Codex abandon the whole task,
# which costs the user far more than a slow turn -- so the request waits for the
# soonest exit to cool off and then goes out. This is the ceiling on that wait;
# if the next exit needs longer than this, the request fails as it used to.
# 0 disables parking entirely (straight back to the old 503 behaviour).
EGRESS_WAIT_BUDGET = float(os.getenv("LINGLING_EGRESS_WAIT_BUDGET", "120"))

# ---------------------------------------------------------------------------
# Usage retention
# ---------------------------------------------------------------------------
# The request log grows without bound otherwise. Rows older than this are pruned
# on startup. 0 disables pruning entirely.
USAGE_RETENTION_DAYS = int(os.getenv("LINGLING_USAGE_RETENTION_DAYS", "90"))

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
REQUEST_TIMEOUT = int(os.getenv("LINGLING_REQUEST_TIMEOUT", "120"))
DISPATCH_TIMEOUT = int(os.getenv("LINGLING_DISPATCH_TIMEOUT", "60"))
# Interactive clients such as Cline need visible progress quickly. This is a
# time-to-first-token budget for native SSE requests, not a cap on the complete
# response: after the first chunk arrives the stream continues normally.
STREAM_FIRST_TOKEN_TIMEOUT = float(os.getenv("LINGLING_STREAM_FIRST_TOKEN_TIMEOUT", "45"))
# Bound the time spent establishing a SOCKS/HTTP proxy connection. This is
# separate from response generation and prevents an unreachable local WARP
# endpoint from consuming the whole first-token budget. 3s was the historical
# default; 2s fails a dead proxy faster while still covering a healthy WARP
# tunnel (the connect is to a *localhost* wireproxy, so sub-50ms normally).
PROXY_CONNECT_TIMEOUT = float(os.getenv("LINGLING_PROXY_CONNECT_TIMEOUT", "2"))

# ---------------------------------------------------------------------------
# Concurrency + connection pooling (first-token latency tuning)
# ---------------------------------------------------------------------------
# Max threads the sync executor runs on. Every streamed request holds one
# thread from the pool for the life of the stream, so a small default
# (anyio's ~40) becomes the real concurrency ceiling under many users. The
# executor work is mostly I/O waiting on the upstream, so a generous thread
# count is cheap. Bump via LINGLING_THREADPOOL_MAX if you expect more users.
THREADPOOL_MAX = int(os.getenv("LINGLING_THREADPOOL_MAX", "128"))

# How many warm httpx clients to keep per egress proxy. Each pooled client
# holds a live SOCKS5 + TLS connection to the upstream, so reusing it skips
# the handshakes that dominate first-token latency. A coding agent firing
# many turns at one proxy benefits from several warm clients in parallel.
CONNECTION_POOL_MAX_PER_PROXY = int(os.getenv("LINGLING_CONNECTION_POOL_MAX", "8"))
# Seconds an idle pooled connection is kept before it is discarded.
CONNECTION_POOL_IDLE_S = float(os.getenv("LINGLING_CONNECTION_POOL_IDLE_S", "90"))


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
