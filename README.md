# Lingling

Lingling puts an OpenAI-compatible API in front of opencode.ai's free tier and rotates the exit IP of every single request through a pool of Cloudflare WARP and Tor SOCKS5 lanes, so opencode's per-IP free-tier meter never quite catches on to you. It's a sassy local gateway that mooches free compute, and it's mildly annoyed about it.

> Unofficial. Not affiliated with opencode, Cloudflare, the Tor Project, or anyone who'd approve of that sentence. Lingling routes your requests through a dozen-odd rotating exit IPs to dodge a per-IP rate limit on a free product you didn't pay for — which is clever, mildly sketchy, and entirely your problem. Don't run a real product off someone else's free compute, don't hammer their free tier from a hundred rotating IPs and expect them to love you, and don't come crying when they swap the User-Agent gate and your favourite free model 429s every IP you throw at it.

For the full, diagram-heavy, no-jokes version of all of this, see [ARCHITECTURE.md](ARCHITECTURE.md) — it's all in there.

## What it does

You know free AI tiers. They work beautifully for twenty minutes, then a per-IP rate limit mid-task kills the whole session. Lingling sits on `127.0.0.1:8000`, your editor talks to it instead of the internet, and it spreads your requests across a rotating pool of exit IPs so throttling stops being the thing that interrupts you. It speaks OpenAI (`/v1/chat/completions`), Codex's Responses API (`/v1/responses`), and Anthropic (`/v1/messages`) — one gateway, three dialects — and forwards upstream bytes **verbatim**. No reformatting, no "improving" the answer.

It also:
- auto-discovers whatever free models opencode is serving *right now* — no frozen model table, because any list would be wrong within a month;
- retires models opencode quietly stopped serving at chat, even when `/models` keeps advertising them (the deepseek/musespark chronic case), persists across restarts, and self-heals under a probation clock when opencode actually restores the chat;
- sends a real probe request through every WARP lane at startup and every few minutes, so dead/burned IPs get re-rolled before your request finds them;
- runs a tiny non-stream sampler after each heal that pokes every free model through every green exit and records which ones came back **cooked** — yes, that's the technical term — so a model-side refusal fails fast instead of churning the whole pool, and the per-model fallback fires.

Honestly, it's a lot. You don't have to care about most of it.

## Installation

You need, in roughly the order of how much you'll regret not having them:

- **Python 3.11+.** No `pyproject.toml`, no venv lockfile — just `pip install -r backend/requirements.txt`. On Windows, `start.bat` deliberately uses the `py -3` launcher instead of `python`, because PATH's `python` resolving to 3.12 on this box has no fastapi and the backend would crash on import. If your `python` imports fastapi, you're fine.
- **Nothing else you have to install.** First run downloads the WARP tooling (`wgcf` + `wireproxy`, tiny) and a ~25 MB Tor expert bundle on its own, over verified TLS. A failed download never quietly disables certificate checking process-wide. Give the first launch a minute or two; every launch after is fast.

### Windows — just double-click the bat
`backend\start.bat`. It wakes the backend on `http://localhost:8000`, sets `LINGLING_BOOTSTRAP_WARP=1` so the WARP pool registers in-process (no curl gymnastics against an authenticated route), pokes `/api/health` until Lingling answers, and tells you when it's done. Closing the minimized "Lingling Backend" window kills the WARP proxies with it — no orphan `wireproxy.exe` processes haunting your machine.

Honestly, just double-click the bat.

### Any OS — by hand
```
cd backend
pip install -r requirements.txt
python app.py          # = uvicorn app:app on 127.0.0.1:8000
```
(Without the bat, bring the egress pool up yourself: `LINGLING_BOOTSTRAP_WARP=1` does it in-process, or hit the Egress view in the dashboard.)

## Pointing OpenAI at it

Open the dashboard at `http://localhost:8000`, go to **Keys**, and create one — yes, even you. You get an `ll_…` token. The dashboard itself authenticates with a signed HttpOnly session cookie (per-process secret, so a restart logs you out — harmless, just reload the page). `LINGLING_REQUIRE_KEY=0` turns the whole thing into an open local gateway with no key, if the key feels like ceremony.

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer ll_yourtoken" \
  -H "Content-Type: application/json" \
  -d '{"model":"lingling-auto","messages":[{"role":"user","content":"say hi"}]}'
```

- `lingling-auto` is the router: a small free model reads the request and picks the best free model for it, with a rule-based fallback in case the router hallucinates. Or name a specific free model from `GET /v1/models` and pick yourself.
- Three clients, one gateway. Cline / any OpenAI client → `http://127.0.0.1:8000/v1`. Codex → same base (`/v1`, it appends `/responses`), `wire_api = "responses"`. Claude Code → `http://127.0.0.1:8000` **with no `/v1`** (it adds its own).
- One-click setups so you don't have to remember any of that: `setup_codex.bat` writes `~/.codex/lingling_models.json` from the live catalog, wires `~/.codex/config.toml`, and sorts out the key; `setup_claude_code.py` writes `~/.claude/settings.json` and cleans out any stale gateway `provider` block. Both reuse a key you've minted on the dashboard, or mint a fresh one. There's no `set LINGLING_API_KEY` step unless you insist on doing it by hand.

## Which models

Whatever's free right now — the catalog is fetched live from the upstream at runtime, so there's deliberately no model table here. The defaults currently in play (the sampler's sweep order) are `deepseek-v4-flash-free`, `muse-spark-1.2-contributor-free`, `nemotron-3-ultra-free`, `north-mini-code-free`, `big-pickle`, `longcat-2.0-free`, `laguna-s-2.1-free`; the router's own brain is `deepseek-v4-flash-free`, because it's free, fast, large-context, and text-only. If one of these vanishes, the catalog drops it on the next refresh and the request path fails over to another free model.

A quirk worth knowing: opencode **still advertises some pulled free models in `/v1/models`** even after the CLI drops them — the list lies. Lingling learns the truth at chat-time, retires a refused model regardless of the listing (see the recycler below), and routes the request onto a live one transparently. So "this model works" and "this model is listed" are not the same sentence, and that's by design.

### The recycler (retirement cycle)
First request that comes back with the "unavailable for free" 400 retires the model — hidden from the dashboard picker, `/v1/models`, `/api/models`, and the Codex/Claude Code setup catalogs — **regardless of whether `/models` still advertises it** (the chronic case: list says free, chat says no). The model stays hidden for `LINGLING_RETIRED_MODEL_PROBATION_S` (5 minutes by default); after that the catalog self-heal can resurrect it on a live re-list, so a transient upstream blip self-heals in minutes, not the seven-day lockdown the old "trust `/models`" design used to lock out working models for. Chronic offenders — `/models` keeps advertising but chat keeps refusing — cycle parked → retried → re-parked once per probation window until opencode actually restores the chat. Operator-seeded dead ids (`LINGLING_RETIRED_MODELS`, default `ling-3.0-flash-free`) are hidden from the very first startup. The recycler trusts the 400, not the listing.

## The egress pool, a.k.a. the part that sounds illegal

opencode meters the free tier by your connecting IP, not by your key. So Lingling routes every request through a rotating pool of SOCKS5 egress proxies — a burned IP gets cooled and the next request uses a different one. Two free, no-account exit sources do the lifting:

| source | default | distinct exits | rotation |
|---|---|---|---|
| **Cloudflare WARP** | 10 identities | whatever your local Cloudflare PoP offers right now (2–5, measured) | tunnel re-roll (~4 s), free, no re-registration |
| **Tor lanes** | 3 | one per lane, always distinct | restart the lane, ~seconds, fresh route |
| **any SOCKS proxy you add** | — | one per proxy, guaranteed | yours |

One fact that's easy to get wrong: **a WARP identity is not an exit IP.** Your local Cloudflare PoP owns a small set of public exits; ten identities can share one address. So Lingling counts **lanes** (what actually carries traffic), not identities, and the Egress view shows each lane's real exit IP and a live "exit lanes" count — "5 slots on 1 address" can never masquerade as "5 exits" again.

The part that actually *is* locked behind something other than the IP: opencode gates its **best** free models behind the official client's `User-Agent`. Send anything other than `opencode/1.0` and you eat an instant `FreeUsageLimitError` 429 on the very first call, from any IP, with quota to spare. No amount of egress rotation fixes that — the wall was never the IP, it was the header. Lingling identifies as `opencode/1.0` upstream (`LINGLING_UPSTREAM_USER_AGENT`). Touch that only if you know why.

Mildly impressive that this whole rotating-IP launderette exists to dodge a per-IP limit, and the good models were never behind the IP in the first place — but here we are. The lighter free models don't check the header, which is precisely why they kept working while the good ones stayed shut, and the rotation keeps *those* alive.

## The dashboard

`http://127.0.0.1:8000`, five views down the left rail: **Catalog** (what's free now), **Console** (try a prompt in-browser), **Ledger** (every request, charted — the fun one, a board of draggable live blocks), **Keys** (mint / revoke `ll_` tokens), **Egress** (lane health, real exit IPs). It's a single-page app in `frontend/`. Lingling logs sassy `INFO` lines at it — "awake and mildly annoyed", "opencode won't know what hit it", "create one — yes, even you" — because being a little roasting is the house style and the logs are no exception.

## Configuration

All `LINGLING_*` environment variables, all read once at startup, all with defaults tuned for a local single-user gateway. Skip this section unless something specific needs changing. The complete list (sixty-odd knobs, every default included) lives in `backend/core/config.py`; here are the ones you'd actually touch:

**Listening + auth**

| variable | default | what it does |
|---|---|---|
| `LINGLING_HOST` / `LINGLING_PORT` | `127.0.0.1` / `8000` | where `python app.py` binds (loopback only by default) |
| `LINGLING_REQUIRE_KEY` | `1` | set `0` for an open local gateway, no `ll_` key needed |
| `LINGLING_ALLOWED_ORIGINS` | *(empty)* | extra CORS origins, comma-separated (default allow-list is explicit loopback ports, never `*`) |
| `LINGLING_DATA_DIR` | `backend/data` | where the keyring, usage DB, WARP identities, Tor state, and logs live |

**Upstream**

| variable | default | what it does |
|---|---|---|
| `LINGLING_OPENCODE_BASE` | `https://opencode.ai/zen/v1` | the free tier you're mooching off |
| `LINGLING_UPSTREAM_USER_AGENT` | `opencode/1.0` | the header the best free models are gated behind; touch only if you know why |
| `LINGLING_CATALOG_TTL` | `600` | how often the model list refreshes (s) |

**Egress pool**

| variable | default | what it does |
|---|---|---|
| `LINGLING_WARP_COUNT` | `10` | how many WARP identities to register |
| `LINGLING_TOR_ENABLED` / `LINGLING_TOR_COUNT` | `1` / `3` | Tor exit lanes beside WARP — zero-account exit IPs; first run downloads Tor itself |
| `LINGLING_PROBE_ON_STARTUP` | `1` | send a real model request through each lane at startup to catch dead/burned IPs early |
| `LINGLING_PROBE_INTERVAL_S` | `300` | the daemon re-probes + heals on this cadence |
| `LINGLING_BOOTSTRAP_WARP` | `0` | `start.bat` sets this `1` to register + start WARP in-process, sidestepping the authenticated `/api` routes |

**Model availability (the recycler)**

| variable | default | what it does |
|---|---|---|
| `LINGLING_RETIRED_MODELS` | `ling-3.0-flash-free` | known-dead ids hidden from the catalog from the very first startup (opencode keeps advertising them) |
| `LINGLING_RETIRED_MODEL_PROBATION_S` | `300` | how long a runtime-retired model stays hidden before the catalog self-heal can resurrect it on a live `/models` re-list; a chronic offender (still listed but chat refuses) cycles parked → retried → re-parked every window |
| `LINGLING_RETIRED_MODEL_TTL_DAYS` | `7` | upper-bound age cap — a retired entry older than this is dropped on a fresh gateway start; probation re-parks reset the timer, so a chronic offender cycling on probation never ages out |

**Sampler + streaming**

| variable | default | what it does |
|---|---|---|
| `LINGLING_SAMPLER_ENABLED` | `1` | after each heal, sample every free model across the green exits and flag the cooked ones so a refusal fails fast |
| `LINGLING_SAMPLER_FAIL_FAST_ATTEMPTS` | `1` | cap per-request egress attempts on a cooked model so the request fails (and falls back) instead of burning the pool |
| `LINGLING_STREAM_RECOVERY` | `1` | a stream that dies after the first chunk retries once on a fresh IP and tells the client to drop the partial answer via a `lingling_reset` frame |
| `LINGLING_LONG_THINKING_MODELS` | `muse-spark-1.2-contributor-free` | models that think with hidden server-side tokens and need extra stream patience, even when the catalog doesn't flag them reasoning |

The only one that's actually a secret is your `ll_` API key (you'll never guess what it does). Everything else just tunes a gateway that already works.

## Local test data stays local

`backend/data/` — your issued `ll_` API keys (`api_keys.json`, plaintext), the usage SQLite DB (`usage.db`), retired-model state (`retired_models.json`), the backend logs (`backend.log*`), ten WARP identity dirs each holding a real WireGuard **private key** (`wgcf-account.toml`), Tor state, and the downloaded `wgcf.exe` / `wireproxy.exe` / `tor.exe` binaries — is gitignored in its entirety and never committed. Same for `backend/accounts.json` and `backend/proxies.json` (which can embed proxy credentials) and `.env*`. A fresh checkout starts with an empty `data/` dir and mints everything on first run. Don't copy someone else's `data/` — their `ll_` keys and WARP private keys travel with it.

## Developing

The repeatable test suite (hermetic unit tests — the opt-in live integration checks need network and an available free tier):

```
cd backend
python -m pytest tests/ -q
```

Run it from `backend/`, not the repo root, or the package imports won't resolve. `backend/requirements-dev.txt` has the test deps.

## License

See [LICENSE](LICENSE). Free to use and run; may not be sold or redistributed in modified form. Copyright (c) 2026 huranth.
