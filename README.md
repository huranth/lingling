# Lingling

### What even is this?

You know how the good AI models cost money, and the free ones are either useless
or throttle you after three messages? OpenCode quietly gives away a handful of
genuinely good models for free — no signup, no card, nothing. The only catch is
they slow you down after a few requests, counting by your IP address.

Lingling is the little piece in the middle that fixes that. You point your editor
at it — Cline, Codex, Claude Code, whatever you already use — and it passes your requests
through to those free models, quietly spreading them across a pool of free
network exits so you stop hitting the wall. That's the whole idea. Free models,
in the editor you already have, without the throttling getting in your way.

It runs on your own computer. Nothing is uploaded anywhere, nothing calls home,
there's no account. You start it, you point your editor at `localhost`, you're
done.

If you just want to use it, jump to [Getting started](#getting-started). The rest
of this page gets more technical as you scroll — that's on purpose. Read as far
as you care to.

---

## "Wait, is this even allowed?"

Fair question, and the honest answer is yes. Here's the reasoning, not just the
reassurance:

**These models are already free and already keyless.** You can call them right
now with a single `curl` command and no credentials — that's how OpenCode
published them, not a crack or a workaround. Lingling makes the exact same
request you could type by hand.

**It literally cannot touch the paid models.** There's a check in the code that
fails *closed* — a model is served only if OpenCode itself marks it free.
Everything else is refused before a request is even built. Right now that's 7
served out of 60, and there's a test that breaks the build if that guard ever
slips. No borrowed keys, no cracked accounts, no pretending to be a paying
customer.

**The IP rotation, said plainly.** OpenCode limits the free tier by IP address.
Lingling rotates through Tor exit lanes — each a distinct exit IP, no account
needed — so one throttled IP doesn't stall you. I won't dress it up:
that's a rate-limit-softener. It doesn't unlock anything paid and there's no
login to bypass, because the free tier has none. Whether that sits right with you
is your call. Don't want it? Leave the pool empty and Lingling connects directly.
That's the default.

**It's yours alone.** Runs on `127.0.0.1`, everything stays local, nothing is
hosted. The [LICENSE](LICENSE) forbids selling it.

If you want the paid models, buy them from OpenCode — that's what their key is
for. Lingling is for the free ones.

---

## Getting started

You need Python 3.11+ and about two minutes.

```bash
cd backend
pip install -r requirements.txt
python app.py
```

That's it. Open **http://127.0.0.1:8000** in a browser and you'll get a
dashboard showing which models are available and what your requests are doing.

On Windows you can double-click `backend/start.bat` instead — it does the same
thing and also sets up the Tor egress lanes for you (the first run downloads
the Tor bundle and takes a minute or two; later runs are instant).

No API key. Lingling is open on your own machine — you just point your editor at
the base URL and go.

---

## How it works

```
Your editor  ──►  Lingling  ──►  OpenCode's free models
                     │
                     ├─ picks a model (you name one, or let it choose)
                     └─ rotates which IP the request goes out from
```

You either name a model directly, or send the special id `lingling-auto` and let
Lingling pick. Auto-pick asks one small free model to read your request and
choose the best model for it from the live list. When that model is unreachable
or answers with nonsense, a rule reads the request itself — is this writing code,
diagnosing something, looking at an image, or just saying hello — and picks on the
same basis, so a request is never dropped and never lands on the wrong kind of
model because the chooser had an off moment.

Auto-routed requests can also change model **mid-answer**. If the stream dies
partway through, the retry re-decides instead of reopening on the model that just
stalled; the client gets a `lingling_reset` marker first, and the ledger records
the model that actually finished the turn. A request that named a model
explicitly always stays on it.

### Which models you get

Lingling asks OpenCode for its model list on every refresh and serves whatever
is free at that moment — if OpenCode publishes a new free model tomorrow, it
shows up on its own, no update needed. These are the seven available as of
August 2026; the **Catalog** tab in the dashboard is always the live answer:

| Model | Good at | Context |
|-------|---------|---------|
| `muse-spark-1.2-contributor-free` | coding, complex debugging, tool use | 1M |
| `nemotron-3-ultra-free` | maths, planning, multi-step reasoning | 1M |
| `laguna-s-2.1-free` | everyday questions and writing | 256K |
| `nemotron-3.5-lightning-free` | fast general chat and light coding | 256K |
| `mimo-v2.5-free` | **images** — the only free model that can see | 200K |
| `big-pickle` | careful step-by-step reasoning and tool use | 200K |
| `hy3-free` | reasoning | 190K |

---

## Connecting your editor

### Cline, or anything OpenAI-compatible

| Setting | Value |
|---------|-------|
| Provider type | OpenAI Compatible |
| Base URL | `http://127.0.0.1:8000/v1` |
| API key | anything — the gateway is open (leave blank if your client allows) |
| Model | any from the table above, or `lingling-auto` |

### Codex

Codex needs a little more, because recent versions dropped support for the older
API style. Put this in `~/.codex/config.toml` (on Windows:
`C:\Users\YOU\.codex\config.toml`):

```toml
model_provider = "lingling"
model = "lingling-auto"

[model_providers.lingling]
name = "Lingling"
base_url = "http://127.0.0.1:8000/v1"
wire_api = "responses"
```

The `wire_api = "responses"` line matters — Codex 0.144+ only speaks the newer
Responses format, and Lingling translates between the two for you. No key is
needed; the gateway is open.

### Claude Code

Claude Code speaks Anthropic's own API rather than OpenAI's, so Lingling has a
separate endpoint for it. The easiest way in is the setup window — double-click
`scripts/setup_claude_code.py`:

```
Point Claude Code at Lingling
  Gateway         http://127.0.0.1:8000
  Starting model  [ muse-spark-1.2-contributor-free  v ]
                                     [Refresh] [Apply] [Close]
```

It reads the model list from your running gateway and writes
`~/.claude/settings.json`. It also strips any leftover `provider` block from a
previous gateway — that key is not in Anthropic's settings reference, and it
silently outranks everything documented, which is the usual reason "I set the
variables and nothing changed".

Then just run `claude`. No environment variables.

If you would rather not use a window:

```cmd
python scripts/setup_claude_code.py                                   :: lists models
python scripts/setup_claude_code.py --model muse-spark-1.2-contributor-free
```

Or configure it by hand. Note the base URL has **no `/v1`** — Claude Code appends
that itself:

```cmd
set ANTHROPIC_BASE_URL=http://127.0.0.1:8000
```

**Model and thinking depth stay in the terminal.** `/model` and `/effort` change
them per session, and Lingling reads the depth off every single request. That is
why the setup window has no depth selector and never writes `ANTHROPIC_MODEL`:
Anthropic's precedence rule is that the environment variable *overrides* the
`model` setting, so writing it would make `/model` a no-op — the picker would show
your choice while every request carried the pinned one.

Leave the starting model unset and Claude Code asks for its usual
`claude-sonnet-...` names, which Lingling maps onto free models by size class:
`haiku` gets a fast one, `sonnet` the general-purpose default, `opus` the deepest
thinker available. Naming a real free model pins it instead.

Claude Code also makes small background calls for session titles and conversation
summaries, on whatever model its `haiku` alias resolves to. Those are served the
same way, so nothing 404s while you work.

Thinking depth translates. Whether Claude Code sends the modern
`output_config.effort` (`low`/`medium`/`high`/`xhigh`/`max`) or the older
`thinking.budget_tokens`, Lingling ranks it and clamps it to what your chosen
model actually publishes — see the effort section below for what that means per
model.

One thing to expect from the free tier: on a hard prompt at `max` effort, a model
can spend its entire token budget thinking and never answer. Lingling reports that
plainly rather than showing you its scratch work, with `stop_reason: max_tokens`
so the agent can retry. If you see it, `/effort medium` usually fixes it.

---

## Reasoning effort — "how hard should it think?"

Most editors let you dial how much the model thinks before answering. Two things
make this messy: every editor uses different words for it, and **only some
OpenCode models actually have the control at all.**

Lingling reads each model's real capabilities from models.dev — the same data
OpenCode's own CLI shows under `/variants` — and translates your editor's word
into something that model honours.

### Which models can be dialled

| Model | Levels it actually supports |
|-------|-----------------------------|
| `muse-spark-1.2-contributor-free` | `minimal`, `low`, `medium`, `high`, `xhigh` |
| `laguna-s-2.1-free` | `low`, `medium`, `high` |
| `mimo-v2.5-free` | **none — always runs on its default** |
| `big-pickle` | **none — always runs on its default** |
| `hy3-free` | **none — always runs on its default** |
| `nemotron-3-ultra-free` | **none — always runs on its default** |
| `nemotron-3.5-lightning-free` | **none — always runs on its default** |

This list is read live, not hardcoded. If OpenCode gives a model new levels
tomorrow, Lingling picks them up on its next refresh.

---

## The dashboard

Open `http://127.0.0.1:8000` and you get four views down the left rail:
**Catalog** (which models you can reach), **Console** (chat with them right
there), **Ledger** (every request), and **Egress** (the Tor lanes).

The Ledger is the one worth playing with. Click **Arrange** and every block
becomes adjustable:

- **Drag the grip** (or use arrow keys) to reorder blocks
- **The 3 / 4 / 6 / 8 / 12 buttons** set how many of the 12 columns a block takes
- **hide** tucks a block away; it comes back from the tray at the bottom
- **Charts with more than one useful shape get a style picker:**

| Block | Styles |
|-------|--------|
| Activity · last hour | `area` · `line` · `bars` |
| Latency spread | `bars` · `curve` · `cumulative` |
| Share of traffic | `bars` · `donut` · `stacked` |
| Outcomes | `bars` · `donut` · `stacked` |

These are different *readings* of the data, not skins. `cumulative` on latency
answers "what share of responses finish within this budget", which the histogram
can't tell you. Totals and Request log have no style picker — a number grid and a
table have no honest second form, and inventing one would just be decoration.

Everything you change is saved in your browser and comes back on reload. **Reset
layout** puts it all back.

---

### How your setting gets translated

Editors speak different vocabularies:

```
Codex        none  minimal  low  medium  high  xhigh  max  ultra
Claude Code                 low  medium  high  xhigh  max  ultracode
```

A word on its own means nothing across a boundary — `ultra` is Codex's ceiling,
`ultracode` is Claude Code's, and neither exists in OpenCode's vocabulary. So
each label is placed on a shared 0–1 scale and the **nearest level the model
really supports** wins:

| You ask for | On `deepseek` you get | On `ling` you get | On `mimo` you get |
|-------------|----------------------|-------------------|-------------------|
| `minimal` | `high` | `low` | *nothing sent* |
| `medium` | `high` | `medium` | *nothing sent* |
| `max` | `max` | `high` | *nothing sent* |
| `ultra` (Codex) | `max` | `high` | *nothing sent* |
| `ultracode` (Claude Code) | `high` | `high` | *nothing sent* |

Two rules behind that table. When you ask for less than a model's floor, you get
its floor — `deepseek` genuinely cannot think less than `high`, so that is not a
fudge, it is the weakest setting that exists. When two levels are equally close,
the weaker one wins, so a translation never quietly spends more thinking than you
asked for.

### The three models with no dial

`mimo`, `big-pickle`, `hy3` and the `nemotron` pair expose no effort control.
Whatever you set, they run on their own default — and Lingling **sends no
effort parameter at all** rather than sending one for show. That matters
because OpenCode returns a normal success response for a value a model ignores,
so a parameter sent to these models would look like it worked while changing
nothing. If you set `max` and get a reply in five seconds, this is why.

### If your editor sends nothing

Cline usually sends no effort setting. In that case Lingling sends none
either — the model's own default applies. There is deliberately no
`LINGLING_DEFAULT_EFFORT`: guessing on your behalf would be editorialising on a
request the gateway is supposed to forward.

### Codex: one setup step, then the dial works

Effort works in Codex too, but it needs one thing done first. Codex only sends the
effort field for models listed in its own catalog, and a third-party model isn't
in there — so until you give it a catalog, `model_reasoning_effort` is read,
displayed in Codex's UI, and then dropped before the request leaves.

Lingling generates that catalog for you:

```cmd
cd backend
python tools\codex_catalog.py
```

That writes `~/.codex/lingling_models.json` and prints the line to add to
`~/.codex/config.toml`. It goes at the **top**, above the first `[section]` header
— TOML puts anything after a header inside that section, and Codex reads this key
at top level only:

```toml
model_catalog_json = "C:/Users/YOU/.codex/lingling_models.json"
```

Check it took with `codex debug models` — it should list Lingling's models
instead of OpenAI's. After that, `/model` in Codex offers each model's real
levels (`high`/`max` on deepseek, `low`/`medium`/`high` on ling and laguna), and
whatever you pick arrives and gets translated exactly like any other editor's.

Re-run the generator after upgrading Codex or when OpenCode publishes new free
models — it reads both live, so it can't go stale on its own. Models with no
effort control are deliberately left out; nothing about them changes.

Cline and direct API calls need none of this — they send the field as-is.

### Claude Code: no setup step needed

Claude Code sends its depth on every request, so there is nothing to configure.
It uses two different fields depending on version, and both are read:

```
output_config.effort            low  medium  high  xhigh  max
thinking.budget_tokens          a token count, bucketed onto the same ladder
thinking.type = "disabled"      an explicit "don't think"
```

Where both appear, `output_config.effort` wins — that is the field Anthropic's own
migration guide moves to, and `thinking: {"type": "adaptive"}` alongside it is a
mode rather than a depth.

A token budget becomes a rung by size, with the boundaries sitting between Claude
Code's own presets (roughly 4k for *think*, 10k for *think harder*, 32k for
*ultrathink*) so a preset lands unambiguously. And `thinking: disabled` maps to
`none`, which is different from sending nothing: a model publishing an off rung
gets told to stop, instead of being left at its default.

After that it goes through the same translation as every other editor — ranked,
clamped to what the routed model publishes, dropped entirely for models with no
dial.

---

## Under the hood

Everything below is for people who want to read or change the code. You don't
need any of it to use Lingling.

### Key Components

| Component | File | Purpose |
|-----------|------|---------|
| **FastAPI app** | `backend/app.py` | Server: routes, auth middleware, streaming pipeline |
| **Auth** | `backend/core/auth.py` | Session cookies for the dashboard, API keys for clients |
| **Stream guard** | `backend/routing/stream_guard.py` | One retry when a stream dies mid-flight |
| **Stream watchdog** | `backend/routing/stream_idle.py` | Cuts off a stream that stops speaking without closing |
| **Egress parking** | `backend/routing/parking.py` | Waits for a cooling exit instead of answering 503 |
| **Router** | `backend/routing/dispatcher.py` | Picks a model for `lingling-auto` requests, with a deterministic fallback |
| **Effort** | `backend/routing/effort.py` | Translates each harness's thinking-depth label onto OpenCode's ladder |
| **Codex catalog** | `backend/models/codex_catalog.py` | Builds the catalog file Codex needs before it will send an effort field |
| **Claude Code** | `backend/claudecode/` | Anthropic Messages translation, kept apart from the Codex bridge on purpose |
| **Executor** | `backend/routing/executor.py` | Calls providers with failover logic |
| **Tor lanes** | `backend/tor/manager.py` | Manages `tor.exe` lanes pinned to distinct exit countries |
| **Proxy Pool** | `backend/providers/proxy_pool.py` | Rotating egress proxy pool |
| **Provider** | `backend/providers/opencode.py` | OpenCode free-tier provider implementation |
| **Usage** | `backend/usage/store.py` | SQLite usage ledger (two-phase for streams) |
| **Frontend** | `frontend/index.html` | Single-page dashboard |
| **Tokens** | `frontend/tokens.css` | The design system: every colour/font/space value |

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Dashboard |
| GET | `/api/health` | Liveness + provider/catalog summary |
| GET | `/v1/models` | OpenAI-compatible model list (for client pickers) |
| POST | `/v1/chat/completions` | **Main router** -- runs a prompt through the selected model |
| POST | `/v1/responses` | Responses bridge for Codex 0.144+ (`wire_api = "responses"`) |
| POST | `/v1/messages` | Anthropic Messages bridge for Claude Code |
| GET | `/api/models` | Multi-model entry + free model list |
| GET | `/api/usage` | Summary + recent feed + daily + live buckets |
| GET | `/api/usage/since/{id}` | Rows newer than `{id}` + refreshed aggregates |
| DELETE | `/api/usage` | Wipe the ledger (irreversible) |
| GET/POST | `/api/proxies` | Proxy pool status / add a proxy |
| GET | `/api/tor` | Tor lane status |
| POST | `/api/tor/setup` | Download the Tor Expert Bundle |
| POST | `/api/tor/start` | Start Tor lanes |
| POST | `/api/tor/renew` | NEWNYM refresh across the lanes |
| POST | `/api/tor/refresh` | Rebuild circuits / regenerate lanes |
| GET | `/api/tor/health` | Health check Tor lanes |

### Auth

There is none — the gateway is open on your own machine. Point any client at the
base URL and it works. CORS is an explicit allow-list (loopback dashboard ports
plus `LINGLING_ALLOWED_ORIGINS`), not `*`, so a random website cannot drive the
dashboard or fire destructive routes like `DELETE /api/usage` or
`POST /api/tor/refresh` from your browser.

---

## Response Handling

Lingling does **not** rewrite, re-space, or restructure upstream output.
OpenCode already handles BPE decoding, reasoning fields, and SSE streaming.
We forward the raw response body (JSON or SSE) and only append routing metadata
in the `lingling` envelope.

Two deliberate exceptions sit at the Lingling edge:

- `_harvest_stream_usage()` reads each SSE line in passing to capture token
  counts, because a streamed request otherwise records zero usage. The bytes
  forwarded to chat-completions clients are untouched.
- `/v1/responses` translates Responses request/stream shapes to and from chat
  completions for Codex. It is stateless: send complete context; Lingling does
  not store or resolve `previous_response_id`.

---

## Reasoning effort — implementation notes

The user-facing table is [above](#reasoning-effort--how-hard-should-it-think).
The mechanics:

Effort values come from models.dev's per-model `reasoning_options`, read via
`metadata.reasoning_effort_values()`. They are **not** hardcoded and **cannot be
probed**: OpenCode returns HTTP 200 for a value a model does not implement,
silently ignoring it, so a probe cannot tell "honoured" from "swallowed". An
earlier version of this module built its own table by probing and was wrong for
six of the seven free models — it also claimed 1M context for `deepseek` (really
200K) and missed that `nemotron` has 1M (it was recorded as 128K). Context limits
now come from the same source.

Translation is by **rank**, not by name. `routing/effort.py` maps every harness
word onto a 0–1 scale and picks the nearest value the target model publishes.
Rank is necessary because the published sets are sparse and not slices of one
common ladder — `["high", "max"]` and `["none", "high"]` have no shared ordering
to clamp along. Ties resolve to the weaker value.

Empty published list means the model has no effort control, and the parameter is
dropped. `clamp` also skips any published value this module has no rank for, so
one unfamiliar entry from a third-party feed cannot disable effort for a whole
model, and matching is case/whitespace-insensitive while the string sent back is
the provider's own spelling.

Codex nests it as `reasoning: {"effort": …}`; the chat path reads a flat
`reasoning_effort`. Both land in `_resolve_effort()`, which runs *after* routing —
with `lingling-auto` the target model is not known until the dispatcher has run,
and the legal set depends on the target. It is idempotent, so the streaming
retry path can re-enter it safely, and it re-resolves correctly if failover moves
a request to a different model.

`reasoning_toggle` (deepseek's on/off "Default") is parsed and exposed on the
model's capabilities but not yet driven by any request field.

### Getting Codex to send the field at all

Codex decides whether to put `reasoning` on the wire by looking the model up in
its own catalog; an unknown model gets `"reasoning": null` regardless of config.
Confirmed by pointing Codex at a capture proxy: same request, same
`model_reasoning_effort=high`, `null` with the stock catalog and
`{"effort": "high", "summary": "auto"}` once the model is declared.

`models/codex_catalog.py` builds that declaration and `tools/codex_catalog.py`
writes it. Three things about the schema are worth knowing before touching it:

- **Entries are clones of a real Codex model.** One of the 35 fields is
  `base_instructions` — the entire Codex agent prompt, 11–21 KB. A hand-written
  entry with a short prompt parses fine and quietly turns Codex into a much dumber
  agent, because that string *is* the harness. The generator therefore dumps a
  real model via `codex debug models` (UTF-8 BOM, read with `utf-8-sig`) and
  overrides only identity, levels and limits. Unknown fields ride along untouched,
  so a Codex upgrade that adds one doesn't invalidate the file.
- **The template must be a classic-Responses model.** Codex's newest models set
  `use_responses_lite` / `tool_mode = "code_mode_only"`, and cloning one changes
  what Codex *sends*: no `instructions`, no `tools` array, tools moved into an
  `additional_tools` input item wrapping a JavaScript `exec` tool. The bridge
  reads `instructions` and `tools`, so that variant arrives with no system prompt
  and no tools. `pick_template()` skips them.
- **Overrides never write null.** The parser dumps `null` for absent optional
  fields but rejects it on input for some of them — `web_search_tool_type: null`
  fails with "expected value", reproducibly.

Also note `supports_reasoning_summaries: false` suppresses `reasoning` entirely
even for a declared model, so entries force it true; and Codex sends whatever
effort its config holds even when the value isn't in the declared list, which is
why `_resolve_effort()` still clamps. `model_catalog_json` only accepts a
filesystem path — a URL is read as one and fails — so this is a generated file
rather than a Lingling endpoint.

End-to-end, `muse-spark-1.2-contributor-free` answering the same prompt through
Codex at two of its levels: median 1056 reasoning tokens at `high` against 1667
at `max` (5 runs each). Per-run counts overlap, which is why medians and not a
single pair.

---

## Usage ledger

Streaming is logged in two phases:

1. `UsageStore.log()` inserts at first-chunk time and returns the row id, so a
   stream that dies mid-flight still leaves a record.
2. `UsageStore.finalize()` fills in tokens and the true total duration once the
   upstream emits its terminal `usage` chunk.

`buckets()` gives minute-resolution buckets over the last hour -- this is what the
dashboard's live chart reads. `daily()` can't show live traffic (one day-wide
bucket hides everything until tomorrow). Rows older than
`LINGLING_USAGE_RETENTION_DAYS` (default 90) are pruned on startup.

---

## Tests

```bash
# Backend: 122 tests. Two files -- routing/Tor/parking, and Claude Code.
cd backend && python -m pytest tests/ -q

# Or run the routing suite directly, which prints a per-test summary:
cd backend && python tests/test_routing.py

# Frontend: 52 assertions driven through headless Chrome over CDP.
# Needs the gateway running plus Chrome with a debug port:
chrome --headless=new --remote-debugging-port=9222 --user-data-dir=%TEMP%\cdp
node frontend/tests/smoke.mjs http://127.0.0.1:8000 http://127.0.0.1:9222
```

Most backend tests are hermetic — they stand a fake provider in front of the real
FastAPI app, so a request travels the whole path (handler, bridge, routing, effort
resolution, executor) and the assertions are about what actually reached the model.
A handful are live and hit the real free tier; they skip themselves when it is
unavailable.

The frontend suite asserts every view renders against live data, the board's
drag/resize/hide/persist behaviour and keyboard reordering, chart survival across
repaints, the Stop control and vision guard, and that no console error, exception
or 4xx/5xx occurred during the run.

**The frontend suite needs a ledger with history in it.** Three chart assertions
fail against an empty database — `index.html` deliberately draws no canvas when
there is nothing to plot, so a fresh install reports 3 false failures. Send a few
requests first, or point it at a gateway that has been used.

---

## Config (`backend/core/config.py`)

| Variable | Default | Description |
|----------|---------|-------------|
| `LINGLING_HOST` / `LINGLING_PORT` | `127.0.0.1` / `8000` | Bind address for `python app.py` |
| `LINGLING_ALLOWED_ORIGINS` | *(empty)* | Extra CORS origins, comma-separated |
| `LINGLING_OPENCODE_KEYS` | *(empty)* | OpenCode keys via env instead of `accounts.json` |
| `LINGLING_MULTIMODEL_ID` | `lingling-auto` | Virtual model id that triggers auto-selection |
| `LINGLING_DISPATCHER_MODEL` | *(empty — auto-promoted from the live catalog)* | Free model asked to choose when auto-selecting |
| `LINGLING_TOR_COUNT` | `10` | Number of Tor lanes |
| `LINGLING_USAGE_RETENTION_DAYS` | `90` | Ledger pruning window; 0 disables |
| `LINGLING_STREAM_RECOVERY` | `1` | Retry once when a stream dies mid-flight |
| `LINGLING_STREAM_IDLE_TIMEOUT` | `90` | Treat an open-but-silent stream as broken after this many seconds; 0 disables |
| `LINGLING_EGRESS_WAIT_BUDGET` | `120` | Longest a request waits for a cooling exit before failing (s); 0 restores the old 503 |
| `LINGLING_DATA_DIR` | `./data` | Data directory |
| `LINGLING_STREAM_FIRST_TOKEN_TIMEOUT` | `30` | Time-to-first-token budget for SSE streams (s) |
| `LINGLING_DISPATCH_TIMEOUT` | `60` | Timeout for the auto-select model (s) |
| `LINGLING_REQUEST_TIMEOUT` | `90` | Per-request upstream timeout (s) |
| `LINGLING_CATALOG_TTL` | `600` | Model-list cache lifetime (s) |
| `LINGLING_CATALOG_RETRY` | `30` | Backoff before retrying an empty model list (s) |
| `LINGLING_RETIRED_MODELS` | *(empty)* | Comma-separated model ids hard-hidden from `/v1/models` listings on every load; re-stamped on every refresh so the TTL can never age them out while the seed is set |
| `LINGLING_FAST_MODELS_DIRECT` | `0` | `1` lets the fast models bypass the egress pool |
| `LINGLING_PROXY_STICKY` | `0` | `1` pins a conversation to one exit IP (see below) |

Egress selection is **least-loaded-first**: every request goes out through whichever Tor lane has
done the least work in the last few minutes, so the exit IPs burn their
per-IP quotas evenly.

`LINGLING_PROXY_STICKY=1` instead keeps one conversation on one exit IP. Leave it
off unless an upstream genuinely needs that affinity — a coding agent sends a
single session id for its whole run, so pinning makes one lane absorb the
entire session's quota while the others idle.

---

## Secrets

`.gitignore` covers `backend/accounts.json`, `backend/proxies.json` and
`backend/data/` (issued keys, usage DB, Tor lane state). Provide your
OpenCode key via `LINGLING_OPENCODE_KEYS` (comma-separated) so no secret ever
touches the working tree, or copy `backend/accounts.example.json` to
`backend/accounts.json` and fill it in locally. The free tier is keyless, so a
key is optional.

---

## Known Edge Cases / Limitations
1. **Proxy first-token timeout** -- when a local SOCKS proxy is unreachable,
   the first-chunk wait may consume the configured timeout before failing over.
2. **A recovered answer differs from the partial one** -- mid-stream recovery
   re-requests rather than resuming, because models are not deterministic. The
   client is told to discard the partial via a `lingling_reset` frame and the
   dashboard labels the turn *recovered*; a third-party client that ignores the
   marker would render the answer twice, so recovery is opt-out per request
   (`{"lingling_recover": false}`) and globally (`LINGLING_STREAM_RECOVERY=0`).
3. **A silent stream is cut off, not waited on** -- an upstream that stops
   speaking while the socket stays open is treated as broken after
   `LINGLING_STREAM_IDLE_TIMEOUT` (90 s) and handed to the normal retry. Before
   this, one measured request sat 885 seconds with zero tokens before giving up,
   which is indistinguishable from a hang. A thinking model streams reasoning
   continuously, so a long think keeps the clock reset; only a true stall trips it.
4. **Only one retry** -- two consecutive failures end the stream and log
   `stream_broken`.
5. **A parked request looks slow, not broken** -- when every exit is cooling, the
   request waits for the next one instead of returning 503 (see below). Clients
   with their own short read timeout may give up before Lingling answers; lower
   `LINGLING_EGRESS_WAIT_BUDGET` to fail faster.

---

## Waiting out a burned pool

OpenCode meters its free tier per IP, so when all the Tor lanes are in cooldown
at once the pool is not broken — it is busy, and the first lane is usually back
within seconds. Returning HTTP 503 there was the worst possible answer: Cline and
Codex treat it as fatal and abandon the whole task, so the human loses the
accumulated context of a long run and restarts from scratch, while the pool
recovers a minute later and sits idle.

Lingling now holds the request instead. `ProxyPool.time_until_available()` reports
how long the soonest exit still needs; the request waits that long and goes out
again. A turn that takes seventy seconds is an annoyance, a turn that fails is a
dead session, and nobody trades those the other way round.

The wait is deliberately narrow. It only happens when *every* exit is cooling,
which is the one case where trying again immediately cannot help. A healthy pool,
an empty pool, and any non-retryable status all fail exactly as they did before —
including the fall-back-to-another-model path. And the wait is awaited on the
event loop, never slept inside a worker thread, so a parked request cannot starve
the threadpool that the retry itself needs.

The same wait guards mid-stream recovery. When a stream dies after bytes are
already on the wire, `stream_guard` gets exactly one retry; reopening it while
every exit was cooling spent that attempt on an IP that had to refuse, and the
user lost the rest of the answer. Now the retry waits for capacity first. Since
HTTP 200 was sent long ago, that wait cannot be silent — it emits SSE comment
frames (`: lingling is holding…`) about once a second, which clients ignore and
which never reach usage harvesting. The wait is sliced rather than taken in one
sleep so the worker thread driving the response is handed back between frames.

`LINGLING_EGRESS_WAIT_BUDGET` caps it (120 s by default). If the soonest exit
needs longer than the budget, the request fails immediately rather than stranding
the client; `0` disables parking entirely.

---

## Streaming recovery

`backend/routing/stream_guard.py` wraps the upstream generator. If a stream ends without
the model reporting `finish_reason`, a replacement stream is opened through the
executor -- landing on a fresh exit IP -- and the client receives:

```
data: {"lingling_reset": {"reason": "...", "attempt": 2}, "choices": []}
```

before the new answer. Ledger status becomes `ok_recovered`; a doubly-failed
stream becomes `stream_broken`. A client hanging up raises `GeneratorExit`, which
is *not* caught, so a cancelled request never burns a retry.

---

## Contributing / roadmap

Lingling is actively developed and **future updates land here as commits** — the
early history is sparse because the first few commits were large drops, but
changes from here on are committed as they're made, so the git log is the real
changelog.

Recent work:

- **Tor orphan fix** — closing the backend terminal now kills every
  `tor.exe` child via a Windows Job Object; no manual `taskkill` needed
- **Health daemon warmup** — the first health-cycle skips HTTP probes and pool
  removal so the bootstrap thread has time to launch the lanes without the
  daemon undoing its work
- **Egress panel consistency** — the identity rack always uses real-time
  `_is_running()` state (no longer loses it to stale pool data), and the
  startup poll rate is 1 s for the first 30 s so you can watch proxies come up
- **Codex support** — a `/v1/responses` bridge, since Codex 0.144+ dropped the
  older chat-completions wire format
- **Reasoning effort** — translates each editor's thinking-depth vocabulary onto
  what OpenCode actually accepts
- **Codex-native effort** — `tools/codex_catalog.py` generates the catalog file
  Codex needs before it will send the effort field for a third-party model
- **Claude Code** — a `/v1/messages` endpoint speaking Anthropic's format, so it
  points at Lingling directly, with its thinking depth translated the same way
- **Request-aware fallback, mid-stream reroute** — when the chooser is
  unreachable or says nonsense, a rule reads the request itself (code, reasoning,
  image, small talk) instead of defaulting to the router's own model; and an
  auto-routed stream whose model stalls re-decides on the retry rather than
  reopening on the one that broke, with the ledger crediting the model that
  actually finished the turn

Planned, roughly in order:

- **More free providers** — the provider layer is already an interface
  (`providers/base.py`); a second free gateway is one subclass, and the catalog
  merges models that appear in more than one so they can fail over between them

Issues and pull requests are welcome. If you're adding a provider, start at
`providers/base.py` and copy the shape of `providers/opencode.py`. Run
`python tests/test_routing.py` before opening a PR — it hits the live free tier,
so it catches upstream changes a mocked suite would miss.

---

## License

See [LICENSE](LICENSE). Free to use and run; may not be sold or redistributed
in modified form. Copyright (c) 2026 huranth.


