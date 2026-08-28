"""Central configuration for the Lingling routing proxy.

Lingling is a dumb routing layer: it takes whatever an OpenAI-compatible
client (Cline, Claude Code, any harness) sends and routes it to free models
on OpenCode, defeating OpenCode's per-IP free-tier limit via a rotating pool
of egress proxies (Tor lanes by default). The client owns
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
# and text-only. Empty by default: the dispatcher then picks the best live
# model from the catalog (``routing.dispatcher.dispatcher_model``) instead of
# pinning a specific id that upstream may have rotated out or burned. Set
# LINGLING_DISPATCHER_MODEL to pin one explicitly.
DISPATCHER_MODEL = os.getenv("LINGLING_DISPATCHER_MODEL", "")

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
# Authoritative set of upstream statuses that both failover AND cooldown.
# A single source of truth kept here so the executor retry loop, the egress
# ProxyPool and the per-provider KeyPool stop drifting: 426/409/410/428 were
# retried by the executor but never recorded a proxy cooldown (so a dead exit
# stayed hot for the next request), and 404 was cooled on the proxy pool but
# never retried -- mis-attributing a model-class "not found" to a healthy
# egress IP. 404 stays OUT: it's the model itself that's gone, not the exit.
RECONCILED_FAILURE_STATUSES: tuple[int, ...] = (
    401, 403,               # auth: rotate the proxy/key another host answers
    426, 409, 410, 428,     # upstream "can't proceed" -- try another exit
    429,                    # rate limit (per-IP at OpenCode): cool this exit
    500, 502, 503, 504,     # server outage: try a different host
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
# Whether the fast chat models may bypass the egress pool. Off by default:
# the dispatcher runs on every lingling-auto request, and exempting its pick
# would mean the hottest path egresses from the real IP -- defeating the point
# of the pool. The executor cools a dead proxy and retries another, and
# PROXY_CONNECT_TIMEOUT bounds the stall.
FAST_MODELS_DIRECT = os.getenv("LINGLING_FAST_MODELS_DIRECT", "0") not in ("0", "false", "False", "")
# Which model ids may bypass the egress pool when FAST_MODELS_DIRECT is on.
# Comma-separated, empty by default: fast-path membership is the operator's
# call, not a hardcoded model roster that upstream rotations silently stale.
FAST_MODELS_DIRECT_IDS: set = {
    m.strip() for m in os.getenv("LINGLING_FAST_MODELS_DIRECT_IDS", "").split(",") if m.strip()
}

# -- Model-burn recycler -----------------------------------------------------
# When a model's upstream call returns an "unavailable" status (400 / 404 / 422)
# -- upstream confirmed the model id itself is unserviceable -- the executor
# bumps the catalog's recycler. Once it crosses MAX_MODEL_FAILURES the model
# is burned and dropped from ``catalog.free()`` -- i.e. from every pool, every
# listing, every picker end-to-end without further code. After
# MODEL_BURN_COOLDOWN_SECONDS it gets one trial request back in; success heals
# it, failure re-burns.
#
# Default is 1: a single exhausted-egress model-class failure is conclusive
# evidence that the model is gone upstream-side (Zen model id evaporated, not
# an egress or credential blip; the latter classes are status in {401,403,409,
# 410,426,428,429,500,502,503,504} and those never bump the recycler). A user
# who prompted ``deepseek-v4-flash-free`` once and saw a 400 was seeing the
# burn recycler stall -- the threshold demanded three consecutive errors
# before eviction. Resilience against transient blips is the cooldown, not
# the counter: even at MAX=1, success at the next trial (after the cooldown
# lapses) heals the model. Set LINGLING_MAX_MODEL_FAILURES=3 if you prefer
# the old three-strikes semantics.
MAX_MODEL_FAILURES = int(os.getenv("LINGLING_MAX_MODEL_FAILURES", "1"))
MODEL_BURN_COOLDOWN_SECONDS = float(os.getenv("LINGLING_MODEL_BURN_COOLDOWN_S", "3600"))

# ---------------------------------------------------------------------------
# Tor lanes (genuine exit-IP diversity -- the single egress family)
# ---------------------------------------------------------------------------
# Each lane is a separate ``tor.exe`` pinned to a *different* exit country via
# ``StrictNodes 1`` + disjoint ``ExitNodes {cc}``, so their exit IPs are
# guaranteed distinct by construction. Lanes register into the shared
# ProxyPool, so least-loaded-first rotates across the distinct lanes.
# 10 by default: free-tier quotas are per exit IP, and concurrent CLI sessions
# need enough distinct IPs to spread across (measured: 5 lanes stacked three
# sessions on one IP and that IP's quota died mid-stream).
TOR_LANE_COUNT = int(os.getenv("LINGLING_TOR_COUNT", "10"))
# First SOCKS5 listener; +1 per lane.
TOR_SOCKS_BASE_PORT = int(os.getenv("LINGLING_TOR_SOCKS_BASE_PORT", "52001"))
# First Control listener; +1 per lane. Default deliberately does NOT sit at
# 52101: Windows administratively excludes that block (Hyper-V/WSL reserves
# 52093-52192 on many machines), tor then fails every lane's launch with
# "Failed to bind one of the listener ports" (WSAEACCES on the ControlPort)
# even though the port is free. 52301 clears that range.
TOR_CONTROL_BASE_PORT = int(os.getenv("LINGLING_TOR_CONTROL_BASE_PORT", "52301"))
# One lowercase ISO-3166 country code per lane, comma-separated. Lanes cycle
# through this list when count > len(countries); StrictNodes + disjoint
# ExitNodes still enforce a distinct country per lane even when cycled.
TOR_EXIT_COUNTRIES = [
    c.strip().lower() for c in os.getenv(
        "LINGLING_TOR_EXIT_COUNTRIES", "us,de,nl,fr,ro,gb,ca,se,pl,ch"
    ).split(",") if c.strip()
]
# Override the Tor Expert Bundle location. Empty -> auto-download into
# DATA_DIR/tor/tools (first run is ~1-2 min for the 10-15 MB bundle).
TOR_EXE = os.getenv("LINGLING_TOR_EXE", "")
# Seconds to wait for a freshly launched tor.exe to publish ``Bootstrap 100``.
# Tor's first circuit build is slow.
TOR_BOOT_TIMEOUT = int(os.getenv("LINGLING_TOR_BOOT_TIMEOUT", "120"))
TOR_HEALTH_INTERVAL = int(os.getenv("LINGLING_TOR_HEALTH_INTERVAL", "60"))
# Floor on healthy Tor lanes. Capped at the configured lane count by the daemon.
TOR_MIN_HEALTHY = int(os.getenv("LINGLING_TOR_MIN_HEALTHY", "3"))
# When set, the server launches the Tor lanes during startup.
# start.bat sets this so the default experience is Tor auto-bootstrap.
BOOTSTRAP_TOR = os.getenv("LINGLING_BOOTSTRAP_TOR", "0") not in ("0", "false", "False", "")

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
# Burn-state reconcile against OpenCode Zen
# ---------------------------------------------------------------------------
# On every startup and then periodically, the gateway walks the burn manifest
# on disk, asks OpenCode's live /models endpoint which of those currently-
# burned ids are still listed, and probes the ones that are back. A model
# Zen has dropped from its catalog stays burned (Zen's roster is the source
# of truth for "this model exists"). A model Zen lists again but still 4xxs
# on a probe is *re-burned* and, after this many consecutive probe failures
# in a row, becomes hard-blacklisted: from then on the reconciler stops
# paying upstream bandwidth on it (it stays hidden in /v1/models until an
# operator manually clears the persisted entry or deletes the file). Set
# LINGLING_BURN_BLACKLIST_HITS=0 to disable blacklisting entirely.
#
# Default lowered from 3 to 2 because the probe-pool fan-out (every egress
# agrees on the failure) is now a strong-enough signal that two cycles is
# sufficient for a conclusive verdict: a model that 5xxs across every proxy
# twice in a row is uniformly broken upstream, not a coincidental Cloudflare
# blip -- so the third cycle was routinely just a wasted reconcile window
# during which the dispatcher was still free to pick a model that streams bad.
BURN_BLACKLIST_HITS = int(os.getenv("LINGLING_BURN_BLACKLIST_HITS", "2"))
# Idle gap between reconcile ticks. The first tick runs synchronously inside
# lifespan(), so the operator's first request sees the recovered manifest.
RECONCILE_INTERVAL_S = float(os.getenv("LINGLING_RECONCILE_INTERVAL_S", "1800"))
# Per-probe timeout. Probes are sent keyless against OpenCode's free tier;
# generously under REQUEST_TIMEOUT so a slow probe does not eat the user
# request budget. Per-probe only.
BURN_PROBE_TIMEOUT_S = float(os.getenv("LINGLING_BURN_PROBE_TIMEOUT_S", "10"))

# ---------------------------------------------------------------------------
# Operator-seeded retired models (hard-hidden from listings on every load)
# ---------------------------------------------------------------------------
# Models the operator knows are uniformly broken upstream -- so cleanly broken
# that the auto-burn / auto-blacklist path would still let them into ``/v1/models``
# listings during the gap between first probe and the threshold tripping. With
# the probe-pool fan-out a model that 5xxs across every egress (e.g.
# ``muse-spark-1.2-contributor-free`` in late Aug 2026) escalates ``blacklist_hits``
# and only trips hard-blacklisted AFTER ``BURN_BLACKLIST_HITS`` reconcile cycles --
# during which the dispatcher may still pick it for a Codex turn, watch the stream
# die mid-flight, and leave the client with an empty answer. The seed list below
# stamps these ids as unavailable on every catalog load so the TTL never ages
# them out while the seed is set: the model is hidden before any picker sees it.
# Comma-separated; whitespace tolerated per the project's standard list env-var
# convention (matches ``LINGLING_TOR_EXIT_COUNTRIES`` / ``LINGLING_DASHBOARD_PORTS``).
LINGLING_RETIRED_MODELS = [
    mid.strip() for mid in os.getenv(
        "LINGLING_RETIRED_MODELS",
        # Empty by default: the reconciler's probe-pool blacklisting handles
        # uniformly-broken models dynamically. Pre-seeding muse-spark
        # permanently hid it from every picker and blocked explicit picks
        # that could be healed by the recycler/reconciler. Operators who
        # still need a hard-hide can set LINGLING_RETIRED_MODELS explicitly.
        "",
    ).split(",") if mid.strip()
]

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.getenv("LINGLING_DATA_DIR", str(_BACKEND_DIR / "data")))
USAGE_DB = DATA_DIR / "usage.db"
# Persistent record of which models the recycler has burned, when, and how
# many times reconciliation has probed them without success. Survives
# restarts so the recycler's view of the world is not wiped on every reboot.
BURN_STATE_FILE = Path(
    os.getenv("LINGLING_BURN_STATE_FILE", str(DATA_DIR / "catalog_burn_state.json"))
)

# ---------------------------------------------------------------------------
# Dashboard CORS
# ---------------------------------------------------------------------------
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
#
# 200, not 90: a deep think phase can go fully silent for well over a minute
# (measured 27s+ gaps mid-think; a real session stalled 90s+ on a long think and
# the 90s watchdog killed it as "broken"). The watchdog must outlast the model's
# longest natural silence, not the average one.
STREAM_IDLE_TIMEOUT = float(os.getenv("LINGLING_STREAM_IDLE_TIMEOUT", "200"))

# Soft cap on concurrent streams per egress proxy. Above this, the executor's
# pick filter passes through to the next lane in the picker, and the saturated lane
# waits for an in-flight stream to finish before being chosen again. Sized at 2:
# OpenCode's free tier meters per-IP, and a single lane rarely has the headroom
# for two parallel full-payload streams without burning mid-``firstchunk`` (the
# 40 s stall + 90 s silence + retry-also-dies pattern under 3-CLI stress).
# Tunable via env (LINGLING_PROXY_MAX_PARALLEL_STREAMS) for providers with a
# different per-IP grant (some communities report double the headroom; some
# report half).
PROXY_MAX_PARALLEL_STREAMS = int(os.getenv("LINGLING_PROXY_MAX_PARALLEL_STREAMS", "2"))

# ---------------------------------------------------------------------------
# Waiting out an exhausted egress pool
# ---------------------------------------------------------------------------
# When every Tor lane is in cooldown at once, the honest answer is "come back in
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
# Per-request upstream timeout in seconds. 90 is enough for any free-tier
# model to finish a non-stream request (deepseek-v4-flash-free's slow lane
# completes well within 60s in measurement) while shedding a stuck read
# before interactive clients give up.
REQUEST_TIMEOUT = int(os.getenv("LINGLING_REQUEST_TIMEOUT", "90"))
DISPATCH_TIMEOUT = int(os.getenv("LINGLING_DISPATCH_TIMEOUT", "60"))
# Interactive clients such as Cline need visible progress quickly. This is a
# time-to-first-token budget for native SSE requests, not a cap on the complete
# response: after the first chunk arrives the stream continues normally.
# Lower than REQUEST_TIMEOUT so a slow proxy gives up and Lingling rolls to
# the next egress instead of stretching a single bad tunnel across the whole
# user-facing wait. 30 matches the user-perceived "this is too slow" boundary
# reported in the live logs (~45s timed-out attempts chained into 90+s waits).
STREAM_FIRST_TOKEN_TIMEOUT = float(os.getenv("LINGLING_STREAM_FIRST_TOKEN_TIMEOUT", "30"))
# Bound the time spent establishing a SOCKS/HTTP proxy connection. This is
# separate from response generation and prevents an unreachable local proxy
# endpoint from consuming the whole first-token budget.
PROXY_CONNECT_TIMEOUT = float(os.getenv("LINGLING_PROXY_CONNECT_TIMEOUT", "3"))
# Longest a single socket read on a *live* streaming connection may sit quiet
# before httpx calls it a transport failure. Historically the streaming client
# was built with the single-scalar ``STREAM_FIRST_TOKEN_TIMEOUT`` applied as
# httpx's timeout, which meant every inter-chunk gap -- every legitimate
# thinking pause on a reasoning model -- was capped at 30s and httpx raised
# ``ReadTimeout`` -> ``UpstreamError(504, "read operation timed out")`` -> the
# mid-flight break + ``lingling_reset`` you can see in a real log. That fired
# *before* the ``STREAM_IDLE_TIMEOUT`` watchdog could say anything, so the
# watchdog never got the chance to be the source of truth.
#
# The first-chunk budget is now enforced independently in
# ``executor.execute_stream`` (a threadpool future wrapping only the very
# first ``next(stream)``), so the socket read ceiling can safely exceed the
# idle watchdog and let ``stream_idle.with_idle_timeout`` be the arbiter of
# "gone quiet". 260 > 200 (idle) so the watchdog fires first on a real stall;
# 260 < REQUEST_TIMEOUT+60 so a truly dead socket is still reclaimed
# promptly and the reader thread in ``stream_idle`` exits on its own.
STREAM_READ_TIMEOUT = float(os.getenv("LINGLING_STREAM_READ_TIMEOUT", "260"))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# When false (default) the gateway emits one line per routed request and one
# per lifecycle event. Health-daemon chatter, catalog refresh, and per-proxy
# heal noise move to DEBUG so the terminal stays readable. Set
# LINGLING_VERBOSE=1 for the full firehose.
VERBOSE = os.getenv("LINGLING_VERBOSE", "0") not in ("0", "false", "False", "")


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
