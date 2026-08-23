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

# The public exit IP is assigned when the tunnel is established: sticky for the
# tunnel's life, sampled from the local Cloudflare colo's small pool, and NOT
# tied to the identity (measured live: 9 of 10 identities shared one exit).
# Re-establishing the tunnel re-rolls the exit; rotating the endpoint IP across
# attempts adds entropy. 162.159.193.x is deliberately absent — those edges
# refused handshakes on the test network, and a refused handshake is a wasted
# re-roll. Override for your network via LINGLING_WARP_ENDPOINTS.
WARP_ENDPOINTS = [
    e.strip() for e in os.getenv(
        "LINGLING_WARP_ENDPOINTS",
        "162.159.192.1,162.159.192.50,162.159.192.100,162.159.192.150,"
        "162.159.192.200,162.159.192.250,162.159.195.1,188.114.96.1",
    ).split(",") if e.strip()
]
# How many tunnel re-establishments a rate-limit heal may spend hunting for an
# unburned exit IP, and how long to let a fresh tunnel settle before reading
# its exit IP (~3s measured for handshake + first SOCKS5 accept).
WARP_REROLL_MAX_ATTEMPTS = int(os.getenv("LINGLING_WARP_REROLL_MAX_ATTEMPTS", "6"))
WARP_REROLL_SETTLE_S = float(os.getenv("LINGLING_WARP_REROLL_SETTLE_S", "3"))
# Exit-lane formation (assembling distinct exits on purpose at launch):
# outer rounds of duplicate->empty-exit moves, and rolls per move. Placement
# checks use Cloudflare's trace endpoint only, so rolls cost no OpenCode
# quota; the budget just bounds wall-clock. Formation runs in the background
# (startup thread or the formation endpoint), and convergence continues
# across runs -- the periodic pass keeps improving whatever this run leaves.
WARP_FORMATION_MAX_ROUNDS = int(os.getenv("LINGLING_WARP_FORMATION_MAX_ROUNDS", "4"))
WARP_FORMATION_MAX_ROLLS = int(os.getenv("LINGLING_WARP_FORMATION_MAX_ROLLS", "5"))
# Form the distinct-exit lanes automatically after the startup probe.
WARP_FORM_ON_STARTUP = os.getenv("LINGLING_WARP_FORM_ON_STARTUP", "1") not in ("0", "false", "False", "")
# Verbose per-element WARP chatter: every exit's probe result, every reroll,
# every intermediate spread roll. Off by default so the terminal shows summaries
# (probe done, formation X->Y, sampler verdicts, per-request chat) instead of a
# wall of "warp-N healthy (Ms)" / "restart_instance #N ok" lines. Flip to 1 only
# when debugging the pool.
WARP_VERBOSE = os.getenv("LINGLING_WARP_VERBOSE", "0") not in ("0", "false", "False", "")

# --- Tor egress lanes ---
# Zero-account exit IPs beside WARP: OpenCode answers Tor exits (measured),
# each tor.exe instance is one SOCKS5 lane with a random exit, and restarting
# the instance rotates the exit. First run downloads the ~25 MB expert bundle;
# later instances clone its directory cache and boot in seconds.
TOR_ENABLED = os.getenv("LINGLING_TOR_ENABLED", "1") not in ("0", "false", "False", "")
TOR_LANE_COUNT = int(os.getenv("LINGLING_TOR_COUNT", "3"))

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

# Cooldown on the runtime retirement attempt itself (see _flag_model_retired
# in app.py). A model 400ing "unavailable" under heavy upstream load fires
# that attempt on many concurrent requests at once; this bounds it to one
# retirement decision per model per window so a burst of transient
# "unavailable" 400s doesn't hammer the lock or the persisted retired set.
RETIRE_RECHECK_COOLDOWN_S = float(os.getenv("LINGLING_RETIRE_RECHECK_COOLDOWN_S", "30"))

# Defer re-rolling/restarting an egress (WARP wireproxy / Tor lane) while an
# HTTP stream is riding it, so the health/formation/probe daemons don't
# terminate the tunnel under a live request (the graceful FIN reads as
# "upstream closed before completing" and breaks long reasoning-model streams
# such as MuseSpark; see providers/active_streams.py). 0 reverts to the old
# re-roll-anyway behaviour if a regression appears.
DEFER_REROLL_WHEN_BUSY = os.getenv(
    "LINGLING_DEFER_REROLL_WHEN_BUSY", "1",
) not in ("0", "false", "False", "")


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
# Per-lane probe read timeout extended for a *reasoning* probe model. OpenCode's
# free tier is currently all reasoning models, so the probe often lands on one,
# and a model that thinks before its first token can stretch the trivial "hi"
# probe past PROBE_TIMEOUT -- which false-positives as "dead" and churns the
# healers (regenerating identities) for lanes that are merely thinking. 0 or <=
# PROBE_TIMEOUT disables the extension (reasoning probe models fall back to the
# tight budget). The watchdog cap scales with it, so a wedged lane is only
# slower-to-cut while the probe is on a reasoning model.
PROBE_REASONING_TIMEOUT = float(os.getenv("LINGLING_PROBE_REASONING_TIMEOUT", "45"))
# Probe max_tokens: how many completion tokens the liveness probe asks for. A
# fast model accepts a tiny budget (a cheap is-this-exit-alive ping), but a
# reasoning / long-thinking model rejects it -- muse-spark returns HTTP 400 at
# max_tokens=5 because its reasoning cannot fit, which the sampler misreads as a
# model-side refusal and false-cooks (every green exit probe-errors, so a
# healthy model gets pinned cooked). Reasoning probes get a real budget; fast
# models stay cheap. See routing/sampler._sampler_probe_budget + warp/probe._probe_single.
PROBE_MAX_TOKENS = int(os.getenv("LINGLING_PROBE_MAX_TOKENS", "5"))
PROBE_REASONING_MAX_TOKENS = int(os.getenv("LINGLING_PROBE_REASONING_MAX_TOKENS", "64"))
# Lanes probed at once. Parallel probing keeps one slow lane from stretching
# the whole pass by its full timeout (a sequential pass once measured 6.6
# minutes while every individual request took ~2s); the count stays small
# because several lanes routinely share one exit IP's rate-limit budget.
PROBE_CONCURRENCY = int(os.getenv("LINGLING_PROBE_CONCURRENCY", "6"))
# Budgets for the raw SOCKS5 liveness check that precedes every probe request
# (httpcore's own SOCKS5 handshake reads carry no timeout, so without this a
# lane that accepts TCP but never answers would park the probing thread for
# minutes or forever): the handshake itself, the exit-IP trace fetch through
# an already-verified lane, and the slack on top of a lane's worst case that
# forms the per-lane watchdog deadline. All generous against the ~3s a freshly
# re-rolled tunnel needs for its first SOCKS CONNECT, but firm enough that a
# wedged lane costs seconds, not minutes.
PROBE_SOCKS_TIMEOUT = float(os.getenv("LINGLING_PROBE_SOCKS_TIMEOUT", "8"))
PROBE_TRACE_TIMEOUT = float(os.getenv("LINGLING_PROBE_TRACE_TIMEOUT", "8"))
PROBE_CAP_SLACK = float(os.getenv("LINGLING_PROBE_CAP_SLACK", "12"))
# The health daemon's SOCKS5 probe only proves the tunnel CONNECTs — an exit
# IP OpenCode has 429'd passes it happily. Every PROBE_INTERVAL_S seconds the
# daemon re-runs the real-model probe and both healers, catching rate limits
# that appear mid-session, then verifies the regenerated exits are clean.
# 0 disables the periodic probe (startup probe still runs).
PROBE_INTERVAL_S = float(os.getenv("LINGLING_PROBE_INTERVAL_S", "300"))

# --- Post-heal multi-model sampler ---
# After the startup heal (and, when enough time has passed, after the daemon's
# heal cycle) the pool is freshly verified against a *canary* free model. The
# sampler then runs a tiny non-stream completion per configured model across the
# canary-green exits and attributes the outcome:
#   * a model that errors (model-side 4xx) on every canary-green exit while those
#     same exits serve the canary -> OpenCode-side outage, so the request path
#     fails fast instead of churning the whole pool, and the existing per-model
#     fallback fires.
#   * a model that 429s/timeouts on some green exits but serves others -> per-IP
#     burns, so requests for that model route onto its green subset.
# State is in-memory and short-lived (SAMPLER_TTL_S), refreshed each pass, so a
# model that recovers is retried without a restart. The request path treats a
# model with no fresh sampler data exactly as before, so this is strictly
# additive and reverts fully when LINGLING_SAMPLER_ENABLED=0. See routing/sampler.py.
SAMPLER_ENABLED = os.getenv(
    "LINGLING_SAMPLER_ENABLED", "1",
) not in ("0", "false", "False", "")
# Models swept across the green pool, in order. DeepSeek first (the primary most
# users route through Lingling to reach), then the hidden-reasoning MuseSpark the
# dynamic pacing already protects, then the other curated OpenCode free models.
SAMPLER_MODELS = [
    m.strip()
    for m in os.getenv(
        "LINGLING_SAMPLER_MODELS",
        "deepseek-v4-flash-free,muse-spark-1.2-contributor-free,"
        "nemotron-3-ultra-free,north-mini-code-free,big-pickle,"
        "longcat-2.0-free,laguna-s-2.1-free",
    ).split(",")
    if m.strip()
]
# Minimum time between sampler passes. The sampler piggybacks on the heal cycle,
# so this is a *skip-if-too-recent* gate rather than its own timer; a value >= the
# heal cadence means at most one sampler pass per heal cycle. 0 means every heal.
# Default 600s (10 min), not 300s: each pass probes every model across the green
# pool -- the live log showed one pass at ~68s -- so a 5-min cadence had the sampler
# eating a large slice of OpenCode's free tier on its own and feeding the same 429
# exhaustion it was built to detect. 10 min roughly halves that self-load;
# real-time per-request cooldowns (mark_failure / extend_cooldown) still catch a
# freshly-burned exit between passes, so model-level detection lagging up to one
# interval costs steering freshness, not availability. Tune via the env if the
# tier is generous enough to want fresher green-sets.
SAMPLER_INTERVAL_S = float(os.getenv("LINGLING_SAMPLER_INTERVAL_S", "600"))
# How long the request path trusts a sampler result. Kept at 2x INTERVAL_S so the
# data stays fresh across a whole skipped pass (a sampler that misses one heal
# cycle still has the previous verdict) while a sampler that stops (disabled at
# runtime, or a long OpenCode outage that empties the green pool) does not pin a
# stale "cooked" flag on a model forever. 0 means trust indefinitely (not advised).
SAMPLER_TTL_S = float(os.getenv("LINGLING_SAMPLER_TTL_S", "1200"))
# When the sampler proves a model is OpenCode-side cooked, cap the per-request
# per-egress attempt budget to this many exits so the request fails (and falls
# back) instead of burning the whole pool retrying IPs that cannot fix a
# model-side refusal. 1 = try exactly one exit, then give up to the fallback.
SAMPLER_FAIL_FAST_ATTEMPTS = int(os.getenv("LINGLING_SAMPLER_FAIL_FAST_ATTEMPTS", "1"))

# --- Streaming recovery ---
# A stream that dies after the first chunk cannot fail over at the HTTP level
# (the 200 is already sent). When enabled, Lingling retries once on a fresh exit
# IP and tells the client to discard the partial answer via a `lingling_reset`
# frame. Opt-out per request with `{"lingling_recover": false}`.
STREAM_RECOVERY = os.getenv("LINGLING_STREAM_RECOVERY", "1") not in ("0", "false", "False", "")

# Longest an open stream may go without a usable frame before it is treated as
# broken -- catches an upstream that stops speaking while the socket stays open
# (neither the first-token budget nor stream_guard covers this). A model that
# streams its reasoning continuously never goes quiet, so a true stall is the
# only thing that trips this. 0 disables the watchdog.
STREAM_IDLE_TIMEOUT = float(os.getenv("LINGLING_STREAM_IDLE_TIMEOUT", "90"))

# A *hidden-reasoning* model thinks with server-side tokens it never streams
# and sends no keepalives while it does, so the wire is silent for the whole
# thinking pause. The watchdog above would read that silence as a broken stream
# and stream_guard would retry it to death -- while the model was merely
# thinking. Reasoning models therefore get a longer "thinking patience": a
# silent pause up to this many seconds is tolerated (per-gap, the budget resets
# on every frame, so streaming-then-thinking still only bounds each pause).
# The httpx read timeout for these streams is set above this so the watchdog's
# informative StreamStalled (and its one retry) governs a pause rather than a
# bare httpx ReadTimeout. 0 disables the extension (reasoning models fall back
# to the budgets above) -- it is *not* the watchdog-disable sentinel.
STREAM_THINKING_TIMEOUT = float(os.getenv("LINGLING_STREAM_THINKING_TIMEOUT", "300"))
# Model ids known to think with hidden reasoning tokens even when the catalog
# does not flag them reasoning (a model whose reasoning is server-side and
# unadvertised). Comma-separated; they get the thinking patience regardless of
# metadata. MuseSpark 1.2 Contributor free is the canonical case -- it thinks
# silently (no streamed reasoning tokens, no keepalives) and models.dev does
# not reliably flag it reasoning -- so it ships in the default set rather than
# requiring an operator to discover the break the hard way. Set
# LINGLING_LONG_THINKING_MODELS="" to clear, or list your own ids to override.
LONG_THINKING_MODELS = frozenset(
    m.strip() for m in os.getenv(
        "LINGLING_LONG_THINKING_MODELS", "muse-spark-1.2-contributor-free",
    ).split(",") if m.strip()
)

# --- Runtime-learned "needs thinking patience" registry ---
# A hidden-reasoning model whose listing does NOT advertise reasoning and whose
# client never sends a reasoning param is invisible to LONG_THINKING_MODELS, the
# catalog flag, and the per-request body check -- the only signal that it thinks
# is how it behaves on the wire (a chunk carrying reasoning tokens, or a stream
# that trips the idle watchdog before emitting any visible content). That signal
# is timestamped and persisted here so the patience it needs survives a restart
# and is shared across the chat/responses/messages entrypoints: a future model
# of the same kind self-adapts after its first turn instead of stalling on every
# request until an operator edits LONG_THINKING_MODELS by hand. See
# routing/pacing_memory.py.
REASONING_LEARNED_FILE = Path(
    os.getenv("LINGLING_REASONING_MODELS_FILE",
              str(DATA_DIR / "reasoning_models.json"))
)
# How long a learned entry stays trusted. Long, because reasoning is a stable
# property of a model -- but finite, so a one-time transient stall (a model
# that stalled for an unrelated reason) does not pin it patient forever. 0
# disables the learning extension entirely (falls back to override/catalog/body).
REASONING_LEARNED_TTL_DAYS = int(os.getenv("LINGLING_REASONING_TTL_DAYS", "30"))

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
