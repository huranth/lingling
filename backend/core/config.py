"""Central configuration for the Lingling routing proxy.

All settings can be overridden with environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent

# models.dev: freely-licensed capability metadata used to enrich provider models.
MODELS_DEV_API = os.getenv("LINGLING_MODELS_DEV", "https://models.dev/api.json")

# --- Multi-model dispatcher ---
MULTIMODEL_ID = os.getenv("LINGLING_MULTIMODEL_ID", "lingling-auto")
MULTIMODEL_NAME = os.getenv("LINGLING_MULTIMODEL_NAME", "Lingling Auto")
MULTIMODEL_DESCRIPTION = (
    "Routes every request to the best free model across all providers "
    "automatically. Attaching an image restricts routing to vision-capable "
    "models, and load fails over across providers and keys on rate limits."
)
# The model that makes the routing decision: must be free, fast, large-context
# and text-only.
DISPATCHER_MODEL = os.getenv("LINGLING_DISPATCHER_MODEL", "deepseek-v4-flash-free")

# --- Provider: OpenCode (primary) ---
OPENCODE_BASE_URL = os.getenv(
    "LINGLING_OPENCODE_BASE", "https://opencode.ai/zen/v1"
).rstrip("/")
# Gotcha: OpenCode gates its best free models (deepseek-v4-flash-free,
# mimo-v2.5-free, big-pickle) behind the official client's User-Agent. Any other
# UA gets an instant FreeUsageLimitError 429 from any IP with quota to spare, so
# rotating egress IPs cannot help those models -- the block is on this header.
UPSTREAM_USER_AGENT = os.getenv("LINGLING_UPSTREAM_USER_AGENT", "opencode/1.0")
# OpenCode account keys (the rotation pool / key router).
OPENCODE_ACCOUNTS_FILE = Path(
    os.getenv("LINGLING_ACCOUNTS_FILE", str(_BACKEND_DIR / "accounts.json"))
)

# --- Egress proxy pool (defeats OpenCode's per-IP free-tier limit) ---
# OpenCode meters the free tier by connecting IP, not by key, so requests are
# routed through a rotating pool of egress proxies: a burned IP (429) is cooled
# and the next request uses a different one. Loaded from a JSON file and/or a
# comma-separated env var; an empty pool means direct connection.
PROXIES_FILE = Path(
    os.getenv("LINGLING_PROXIES_FILE", str(_BACKEND_DIR / "proxies.json"))
)
# Quick inline setup: LINGLING_PROXIES="http://host:port,socks5://host:port"
PROXIES_ENV = os.getenv("LINGLING_PROXIES", "")
# Sticky sessions keep one conversation on one exit IP. Off by default: it fights
# the pool's purpose, since a coding agent sends one stable session id for its
# whole run and would pin an entire session's quota to a single IP.
PROXY_STICKY_SESSIONS = os.getenv("LINGLING_PROXY_STICKY", "0") not in ("0", "false", "False", "")
PROXY_COOLDOWN_BASE_MS = int(os.getenv("LINGLING_PROXY_COOLDOWN_BASE_MS", "1000"))
PROXY_COOLDOWN_MAX_MS = int(os.getenv("LINGLING_PROXY_COOLDOWN_MAX_MS", "60000"))
# A single request must not wait through the whole pool; cool one failed proxy
# and let later requests use another.
PROXY_MAX_ATTEMPTS_PER_REQUEST = int(os.getenv("LINGLING_PROXY_MAX_ATTEMPTS", "5"))
# Whether fast chat models may bypass the pool. Off by default: the dispatcher
# and the model it picks for casual chat are the two hottest paths, and exempting
# them would egress from the real IP -- defeating the pool.
FAST_MODELS_DIRECT = os.getenv("LINGLING_FAST_MODELS_DIRECT", "0") not in ("0", "false", "False", "")

# --- Cloudflare WARP rotation (free, unlimited exit IPs) ---
# Each identity becomes one local SOCKS5 proxy on 127.0.0.1:51001+.
WARP_IDENTITY_COUNT = int(os.getenv("LINGLING_WARP_COUNT", "10"))

COOLDOWN_BASE_MS = int(os.getenv("LINGLING_COOLDOWN_BASE_MS", "2000"))
COOLDOWN_MAX_MS = int(os.getenv("LINGLING_COOLDOWN_MAX_MS", "30000"))

# --- Catalog cache ---
CATALOG_TTL_SECONDS = int(os.getenv("LINGLING_CATALOG_TTL", "600"))
# Retry window after an empty /models fetch. Far below the TTL so a recovering
# upstream is picked up quickly without re-fetching on every request.
CATALOG_RETRY_SECONDS = int(os.getenv("LINGLING_CATALOG_RETRY", "30"))

# --- Storage ---
DATA_DIR = Path(os.getenv("LINGLING_DATA_DIR", str(_BACKEND_DIR / "data")))
USAGE_DB = DATA_DIR / "usage.db"

RETIRED_MODELS_FILE = Path(
    os.getenv("LINGLING_RETIRED_MODELS_FILE",
              str(DATA_DIR / "retired_models.json"))
)
# How long a model that refused to serve free stays hidden before being
# re-checked. Retirement is temporary (OpenCode has restored free tiers before),
# learned at runtime and persisted so the gateway and Codex catalog agree.
RETIRED_MODEL_TTL_DAYS = int(os.getenv("LINGLING_RETIRED_MODEL_TTL_DAYS", "7"))
# Known-dead models seeded so they are hidden from first startup, not only after
# the first runtime 400 (OpenCode keeps advertising retired models in /models).
# Comma-separated; "" clears the seed. Runtime retirements merge on top.
RETIRED_MODELS_SEED = os.getenv("LINGLING_RETIRED_MODELS", "ling-3.0-flash-free")


def retired_seed_ids() -> frozenset:
    """Known-dead model ids hidden from the catalog even before first use.

    Unlike runtime-learned retirements, seeded ids are re-stamped ``now`` on
    every catalog load, so they stay hidden until the operator unseeds them.
    """
    return frozenset(m.strip() for m in RETIRED_MODELS_SEED.split(",") if m.strip())

# --- User API keys ---
# Random ``ll_<32 hex>`` tokens in this file; clients send them as
# ``Authorization: Bearer ll_...`` or ``x-api-key: ll_...``.
API_KEYS_FILE = Path(
    os.getenv("LINGLING_API_KEYS_FILE", str(DATA_DIR / "api_keys.json"))
)
# When True (default), /v1/chat/completions requires a valid key. Set
# LINGLING_REQUIRE_KEY=0 for an open local gateway.
REQUIRE_API_KEY = os.getenv("LINGLING_REQUIRE_KEY", "1") not in ("0", "false", "False", "")

# --- Dashboard sessions + CORS ---
# The dashboard authenticates with a signed, HttpOnly session cookie. The signing
# secret is per-process and never written to disk, so a restart invalidates
# outstanding sessions. Replaces the old forgeable sec-fetch-site header check.
SESSION_TTL_SECONDS = int(os.getenv("LINGLING_SESSION_TTL", str(12 * 3600)))
# Ports the dashboard may be served from -- used to build the CORS allow-list
# (a wildcard would let any page call /api/* from a visitor's browser).
DASHBOARD_PORTS = [
    p.strip() for p in os.getenv("LINGLING_DASHBOARD_PORTS", "8000,8008,8080").split(",")
    if p.strip()
]
# Extra origins, comma-separated, for reverse-proxy or LAN setups.
ALLOWED_ORIGINS_ENV = os.getenv("LINGLING_ALLOWED_ORIGINS", "")

# --- Startup bootstrap ---
# When set, the server registers and starts the WARP pool in-process at startup,
# so start.bat needn't authenticate against the routes it would otherwise POST.
BOOTSTRAP_WARP = os.getenv("LINGLING_BOOTSTRAP_WARP", "0") not in ("0", "false", "False", "")

# --- Startup probe ---
# After WARP starts, send a real model request through each proxy to detect
# rate-limited or dead exit IPs immediately rather than on the first user request.
PROBE_ON_STARTUP = os.getenv("LINGLING_PROBE_ON_STARTUP", "1") not in ("0", "false", "False", "")
PROBE_MODEL = os.getenv("LINGLING_PROBE_MODEL", "deepseek-v4-flash-free")
PROBE_TIMEOUT = float(os.getenv("LINGLING_PROBE_TIMEOUT", "15"))

# --- Streaming recovery ---
# A stream that dies after the first chunk cannot fail over at the HTTP level
# (the 200 is already sent). When enabled, Lingling retries once on a fresh exit
# IP and tells the client to discard the partial answer via a `lingling_reset`
# frame. Opt-out per request with `{"lingling_recover": false}`.
STREAM_RECOVERY = os.getenv("LINGLING_STREAM_RECOVERY", "1") not in ("0", "false", "False", "")

# Longest an open stream may go without a usable frame before it is treated as
# broken -- catches an upstream that stops speaking while the socket stays open
# (neither the first-token budget nor stream_guard covers this). Generous,
# because a thinking model streams reasoning continuously; only a true stall goes
# quiet. 0 disables the watchdog.
STREAM_IDLE_TIMEOUT = float(os.getenv("LINGLING_STREAM_IDLE_TIMEOUT", "90"))

# --- Waiting out an exhausted egress pool ---
# When every WARP exit is cooling at once, the request waits for the soonest one
# rather than returning 503 (which makes Cline/Codex abandon the whole task).
# This is the ceiling on that wait; 0 disables parking (back to the old 503).
EGRESS_WAIT_BUDGET = float(os.getenv("LINGLING_EGRESS_WAIT_BUDGET", "120"))

# --- Usage retention ---
# Rows older than this are pruned on startup so the log doesn't grow unbounded.
# 0 disables pruning.
USAGE_RETENTION_DAYS = int(os.getenv("LINGLING_USAGE_RETENTION_DAYS", "90"))

# --- HTTP ---
REQUEST_TIMEOUT = int(os.getenv("LINGLING_REQUEST_TIMEOUT", "120"))
DISPATCH_TIMEOUT = int(os.getenv("LINGLING_DISPATCH_TIMEOUT", "60"))
# Time-to-first-token budget for native SSE requests, not a cap on the full
# response: after the first chunk the stream continues normally.
STREAM_FIRST_TOKEN_TIMEOUT = float(os.getenv("LINGLING_STREAM_FIRST_TOKEN_TIMEOUT", "45"))
# Bounds proxy connection setup so a dead local WARP endpoint can't eat the whole
# first-token budget (the connect is to a localhost wireproxy, sub-50ms normally).
PROXY_CONNECT_TIMEOUT = float(os.getenv("LINGLING_PROXY_CONNECT_TIMEOUT", "2"))

# --- Concurrency + connection pooling (first-token latency) ---
# Max sync-executor threads. Every streamed request holds one for the life of the
# stream, so anyio's ~40 default becomes the concurrency ceiling under load;
# the work is I/O-bound, so a generous count is cheap.
THREADPOOL_MAX = int(os.getenv("LINGLING_THREADPOOL_MAX", "128"))

# Warm httpx clients kept per egress proxy. Each holds a live SOCKS5 + TLS
# connection, so reuse skips the handshakes that dominate first-token latency.
CONNECTION_POOL_MAX_PER_PROXY = int(os.getenv("LINGLING_CONNECTION_POOL_MAX", "10"))
# Seconds an idle pooled connection is kept before discard; keeps warm tunnels
# alive across short gaps between turns.
CONNECTION_POOL_IDLE_S = float(os.getenv("LINGLING_CONNECTION_POOL_IDLE_S", "180"))


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
