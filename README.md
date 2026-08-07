# Lingling

A local gateway to free AI models. It runs on `127.0.0.1`, everything stays on
your machine, and no part of it calls home or needs an account.

You point your editor at it (Cline, Codex, Claude Code, or any OpenAI-compatible
client) and it passes your requests through to OpenCode's free models, spreading
them across a pool of free egress IPs so throttling doesn't stop you mid-task.

Start it, point an editor at `localhost`, done.

## Getting started

Needs Python 3.11+.

```
cd backend
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:8000 for the dashboard. On Windows, double-click
`backend/start.bat` instead — it also sets up the WARP egress pool (downloading
two small tools on the first run; later runs are instant).

By default the gateway asks for an API key: open the **Keys** view, create one,
and paste the `ll_...` token into your editor. To run it wide open on your own
machine, start it with `LINGLING_REQUIRE_KEY=0`.

## Connecting your editor

### Cline / any OpenAI-compatible client

| Setting | Value |
|---------|-------|
| Provider | OpenAI Compatible |
| Base URL | `http://127.0.0.1:8000/v1` |
| API key | your `ll_...` token |
| Model | any free model, or `lingling-auto` to auto-route |

### Codex

Codex only speaks the newer Responses API now, so its provider block points at
the gateway and Lingling translates. The one-click path is `setup_codex.bat` at
the repo root: it refreshes the model catalog, wires `~/.codex/config.toml`,
and handles the API key (auto-fills one already issued, or mints one). That's
the whole setup.

Done by hand, `~/.codex/config.toml` looks like:

```toml
model_provider = "lingling"
model = "lingling-auto"

[model_providers.lingling]
name = "Lingling"
base_url = "http://127.0.0.1:8000/v1"   # codex appends /responses; keep /v1
wire_api = "responses"
env_key = "LINGLING_API_KEY"
```

Set the key in the terminal before `codex` (skip `env_key` if you run with
`LINGLING_REQUIRE_KEY=0`):

```
set LINGLING_API_KEY=ll_your_token
```

For the reasoning dial to reach the model, Codex needs the model declared in its
own catalog. `python tools\codex_catalog.py` generates that file from the live
model list each run (and auto-wires the `model_catalog_json` line into your
config). Re-run it after a Codex upgrade or when new free models appear.

### Claude Code

Claude Code speaks Anthropic's API, so it uses a separate endpoint. The easiest
way in is the setup window: double-click `setup_claude_code.py`. It reads the
model list from your running gateway, reuses or mints a key, and writes
`~/.claude/settings.json`; it also strips any leftover `provider` block from an
older gateway. Then just run `claude` — no environment variables.

By hand: the base URL here has **no `/v1`** (Claude Code appends it):

```
set ANTHROPIC_BASE_URL=http://127.0.0.1:8000
set ANTHROPIC_AUTH_TOKEN=ll_your_token
```

Model and thinking depth stay in the terminal: `/model` and `/effort` change
them per session. Lingling reads the depth off every request and clamps it to
whatever the routed model actually supports.

If you leave the model unset, Claude Code asks for its usual `claude-sonnet-…`
names, which map onto free models by size class (`haiku` a fast one, `sonnet`
the general default, `opus` the deepest thinker). Its background title/summary
calls go out the same way, so nothing 404s.

---

## Which models you get

Lingling fetches OpenCode's model list on a refresh timer and serves whatever is
free at that moment. New free models show up on their own; retired ones (OpenCode
dropped `ling-3.0-flash-free` while these docs were being written) fail over to
another free model with no action on your part. The dashboard **Catalog** view is
the live list.

No capability or effort level is hardcoded; model ids come from the live feed,
except that models already known to have been dropped are seeded as retired so
they never re-list. For context these are the free models as of mid-2026:

| Model | Good at | Context |
|-------|---------|---------|
| `longcat-2.0-free` | very long documents, in-depth coding (Meituan) | 1M |
| `nemotron-3-ultra-free` | maths, planning, multi-step reasoning | 1M |
| `north-mini-code-free` | code generation and refactoring | 250K |
| `laguna-s-2.1-free` | everyday questions and writing | 250K |
| `mimo-v2.5-free` | images — the only free model that sees | 200K |
| `big-pickle` | step-by-step reasoning and tool use | 200K |
| `deepseek-v4-flash-free` | coding, long documents, deep reasoning | 200K |

## Reasoning effort

Most editors let you set how hard a model thinks before answering. Two things
complicate it: each editor uses its own words, and only some models have the
control at all.

Lingling reads each model's real capability set from models.dev (the same data
OpenCode's CLI shows under `/variants`), so whatever word your editor sends gets
translated onto that model's actual levels:

You ask for | On `deepseek` you get | On `laguna` you get | On `mimo` you get
----------- | --------------------- | ------------------- | ----------------
`minimal`    | `low`                 | `low`             | *nothing sent*
`medium`     | `low`                 | `medium`          | *nothing sent*
`max`        | `max`                 | `high`            | *nothing sent*
`ultra` (Codex) | `max`              | `high`            | *nothing sent*
`ultracode` (Claude) | `high`          | `high`            | *nothing sent*

Two rules make that table: asking below a model's floor gets you its floor, and
when two levels are equally close the weaker one wins, so the gateway never
spends more thinking than you asked for. OpenCode silently accepts an effort
value a model doesn't implement, so for a model with no dial Lingling sends no
effort parameter at all instead of sending one for show.

A model that publishes no levels today (`mimo`, `nemotron`, `big-pickle`) runs on
its own default; the moment it publishes real levels, they appear on the next
refresh. Codex shows a single `default` rung for those models. Elsewhere (Cline,
direct API) the field just passes through as-is.

---

## The dashboard

Open http://127.0.0.1:8000 for five views down the left rail: **Catalog**,
**Console**, **Ledger**, **Keys**, and **Egress**.

The Ledger is a rearrangeable board of live blocks. Click **Arrange** to drag
the grip (or arrow-key) blocks into order, set a block's width with the
3/4/6/8/12 buttons, hide one to a tray, and pick a chart style where more than
one reading makes sense (activity: area/line/bars; latency: bars/curve/
cumulative; share and outcomes: bars/donut/stacked). Changes persist in your
browser; **Reset layout** restores the defaults.

## Configuration

Everything is set with environment variables; the defaults suit a local
single-user gateway. The important ones:

| Variable | Default | Meaning |
|----------|---------|---------|
| `LINGLING_HOST` / `LINGLING_PORT` | `127.0.0.1` / `8000` | Where `python app.py` binds |
| `LINGLING_REQUIRE_KEY` | `1` | Gate API clients behind a `ll_` key |
| `LINGLING_DATA_DIR` | `backend/data` | Where the keyring, usage DB and WARP identities live |
| `LINGLING_WARP_COUNT` | `10` | How many WARP identities to register |
| `LINGLING_REQUEST_TIMEOUT` | `120` | Upstream request timeout (s) |
| `LINGLING_CATALOG_TTL` | `600` | How often the model list refreshes (s) |
| `LINGLING_EGRESS_WAIT_BUDGET` | `120` | Max seconds to wait for a cooled egress pool |
| `LINGLING_STREAM_IDLE_TIMEOUT` | `90` | Treat a silent stream as broken after this (s) |
| `LINGLING_RETIRED_MODELS` | `ling-3.0-flash-free` | Models hidden from the catalog from startup (comma-separated; e.g. dropped free models OpenCode still advertises) |
| `LINGLING_ALLOWED_ORIGINS` | *(empty)* | Extra CORS origins, comma-separated |

The full list is in `backend/core/config.py`. All settings are read from the
environment at startup.

---

## How requests are routed

Name a model and it's used directly. Send `lingling-auto` and Lingling asks one
small free model to read the request and pick the best model from the live list.
If that router is unreachable or answers with garbage, a rule falls back by
reading the request itself — is it writing code, diagnosing a bug, looking at an
image, or small talk? — so a request is never dropped and never lands on the
wrong kind of model.

Failover is layered: providers, then keys, then egress IPs. When every attempt
fails because the whole egress pool is temporarily cooling, the request waits
for the next free IP instead of dying with a 503 (which would make Cline or Codex
abandon the whole task). The wait is capped by `LINGLING_EGRESS_WAIT_BUDGET` and
emits SSE keepalives so the client doesn't think the connection hung.

Streaming gets one retry if it dies mid-answer. The partial text is discarded,
the request reopens on a fresh IP (re-deciding the model too, for auto-routed
turns), and the client is told with an explicit `lingling_reset` marker so it
doesn't append the new answer onto the half-gone one. An auto-routed request can
change model mid-turn that way; one that named a model explicitly never does.
A stream that goes silent without dying is cut off after `LINGLING_STREAM_IDLE_TIMEOUT`.

## Current status

Everything in this README is implemented and exercised end-to-end:

- The full test suite — hermetic unit tests plus live integration against the
  real OpenCode free tier — passes (143 tests, no failures).
- Free models come from OpenCode's live list on a refresh timer. Models that are
  advertised but no longer served are retired the first time a request hits the
  400 and stay hidden across restarts; models already known to be dropped are
  pre-seeded so they never list at all (`LINGLING_RETIRED_MODELS`). Retirement is
  temporary by default (7 days) because OpenCode has restored free tiers before —
  a restored model is re-offered automatically, and one that is still dead is
  re-retired on the next 400.
- Streaming answers survive a tunnel drop with one automatic retry behind an
  explicit `lingling_reset` marker, and a stream that goes silent is cut off
  instead of hanging the session.
- WARP egress identities auto-register, get health-checked on a timer, and are
  recycled when rate-limited, so the pool stays warm without manual intervention.
- The WARP bootstrap and the Codex/Claude one-click setups download their tools
  over verified TLS only; a failed download never disables certificate checking
  process-wide.

---

## Under the hood

`backend/app.py` is the whole gateway. The pieces it wires together:

| Piece | Location | Job |
|-------|----------|-----|
| Providers | `backend/providers/` | OpenCode transport, key pool, egress proxy pool, pooled httpx clients |
| Catalog | `backend/models/` | Merges free models + live capabilities (models.dev) |
| Dispatcher | `backend/routing/dispatcher.py` | `lingling-auto` model choice + rule fallback |
| Executor | `backend/routing/executor.py` | Failover across providers/keys/IPs |
| Effort | `backend/routing/effort.py` | Rank-based effort translation |
| Codex bridge | `backend/routing/responses_bridge.py` | Responses ↔ chat completions |
| Claude bridge | `backend/claudecode/` | Anthropic Messages ↔ chat completions |
| WARP | `backend/warp/` | Identity manager, auto-healing health daemon, kill-on-close job |
| Usage | `backend/usage/store.py` | SQLite request ledger |
| Dashboard | `frontend/` | Single-page app |

### Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/` | open | Dashboard + session cookie |
| GET | `/api/health` | open | Liveness + provider/catalog/pool summary |
| GET | `/v1/models` | open | OpenAI-compatible model list |
| POST | `/v1/chat/completions` | gated | Main router |
| POST | `/v1/responses` | gated | Codex (Responses wire format) |
| POST | `/v1/messages` | gated | Claude Code (Anthropic wire format) |
| GET | `/api/models` | gated | Catalog + router entry |
| GET | `/api/usage` · `/api/usage/since/{id}` | gated | Ledger feed |
| GET/POST/DELETE | `/api/keys` | gated | Issue / list / revoke `ll_` keys |
| GET/POST | `/api/proxies` | gated | Egress pool status / add |
| GET | `/api/warp` | gated | WARP status |
| POST | `/api/warp/setup|start|refresh|health` | gated | WARP lifecycle |

### Auth

The dashboard authenticates with a signed `HttpOnly` session cookie issued when
it loads `/`. API clients send `Authorization: Bearer ll_...` (or
`x-api-key`). CORS is an explicit loopback allow-list, not `*`. Sessions don't
survive a restart (the signing secret is per-process), which is fine: the page
just re-fetches `/` and gets a new one.

### Response handling

Lingling forwards upstream bytes untouched — no re-spacing, no rewriting. The
two things it adds at the edge: token counts are read off each SSE line so a
streamed request still lands in the ledger, and the Codex/Claude bridges
translate request and response shapes for those two clients.

---

## License

See [LICENSE](LICENSE). Free to use and run; may not be sold, or redistributed
in modified form. Copyright (c) 2026 huranth.