# Lingling

**Official OpenCode, riding rotating Tor lanes.** The free models on OpenCode
throttle you by IP. Lingling stands between you and the API with a handful of
Tor exits, so when one exit cools off your traffic quietly moves to a fresh
one — you never see the limit, you just keep working.

```
pip install .          # this repo
lingling               # = opencode, but through the lanes
lingling --help        # anything after `lingling` goes to opencode untouched
```

First run downloads the Tor Expert Bundle (~30 MB) into `data/tools/`. After
that, a launch looks like this: a few seconds of kitchen noises while the
first lane cooks, then opencode opens and the remaining lanes register in the
background while you work.

```
 ⠋ glazing the tunnel
 served! lanes are hot -- proof is in the other window.
```

## The proof window

A second console opens next to your editor and shows, live, which lane every
single model call rode — model name, exit country, exit IP, status, size,
duration:

```
13:52:55  #3.1  lane 2 {de} 185.220.100.243  ->  muse-spark-1.2-contributor-free
13:53:00    | #3.1 200 81.4 KB in 2.7s
13:53:01  #4.1  lane 3 {nl} 192.42.116.113  ->  muse-spark-1.2-contributor-free
13:53:03    | #4.1 200 79.6 KB in 2.6s
13:53:07  * lane 4 hit a hidden limit -- your traffic moved to a fresh lane
```

No gaslighting: if the pane shows bytes flowing, the model is answering
through that exact exit. `--no-proof` closes the pane.

## How a limit disappears

A CONNECT tunnel is end-to-end TLS, so a 429 can't be seen in flight. Instead,
a health daemon fires a real `GET /zen/v1/models` through each lane every 45
seconds (with the `opencode/1.0` User-Agent the free tier demands). The moment
a probe comes back 429:

1. the lane leaves rotation **before** your next request would have used it —
   your traffic instantly continues on the other lanes;
2. the burned lane is dropped and re-cooked from scratch: fresh tor process,
   fresh keys, fresh guards, fresh exit IP. No half-measures;
3. if the same lane keeps burning, its exit country rotates too
   (`us → de → nl → fr → ro → …`).

A lane that simply dies gets poked, restarted, then re-cooked; one that can't
be revived sits out and is retried later.

## How the per-call proof works

opencode holds one TLS connection open for a whole session, so a blind proxy
can only see tunnels, not requests. To show each call, Lingling mints a local
throwaway CA (`data/mitm/`) and tells opencode to trust it via
`NODE_EXTRA_CA_CERTS`. TLS to `opencode.ai` is then terminated **on your own
machine**, the model name is read from each request, and the request is
re-encrypted through a fresh lane tunnel to the real server. Everything stays
end-to-end TLS over Tor — the only new trust point is your own computer.
`LINGLING_NO_MITM=1` falls back to blind tunnels (per-tunnel proof only).

Every launch starts fresh: lane keys, guards and circuit state are wiped, only
tor's cached network directory survives (it's public data, and it's why a
relaunch takes seconds instead of minutes). Orphaned `tor.exe` processes from
a crashed run are cleaned up before new lanes bind their ports.

## Flags Lingling keeps for itself

| Flag | What it does |
|---|---|
| `--lanes N` | how many lanes to keep warm (default 5, env `LINGLING_TOR_COUNT`) |
| `--no-tor` | skip the lanes entirely, run opencode on your own IP |
| `--no-proof` | don't open the proof window |
| `--demo [question]` | fire one real Muse Spark request through a lane and show the receipts |

Everything else is passed to opencode byte-for-byte.

## Layout

```
lingling/cli.py      entry point, loader, opencode launch
lingling/lanes.py    Tor lane lifecycle: boot, regenerate, country rotation
lingling/relay.py    local CONNECT relay, least-busy lane picking, MITM handoff
lingling/mitm.py     local CA + per-request interception for opencode.ai
lingling/health.py   probe daemon: burns, deaths, rotation
lingling/proof.py    the proof window (event log + live tail)
lingling/netutil.py  raw SOCKS5 / HTTPS-over-SOCKS primitives
lingling/demo.py     lingling --demo
lingling/winjob.py   Windows Job Object so tor.exe dies with us
data/                runtime state (tor, lanes, proof log) — gitignored
```

Requirements: Python 3.11+, Windows/macOS/Linux, and `opencode` on your PATH.
Runs entirely on your machine; nothing calls home.
