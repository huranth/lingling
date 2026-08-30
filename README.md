# Lingling

### What even is this?

OpenCode gives away genuinely good models for free. The catch: they throttle
you by IP, so right when the conversation gets good — bam, wall. Very rude.

Lingling fixes this the fun way. It spins up a little kitchen of Tor exit
lanes on your machine, each one a different country, and puts your requests
on whichever lane is hot. One exit gets throttled? Cute. Your traffic is
already on another one. You never see the limit. You just keep typing.

```
pip install lingling

lingling               # = opencode, but the requests ride Tor
lingling --help        # anything after `lingling` goes to opencode untouched
```

First run downloads the Tor Expert Bundle (~30 MB) into Lingling's per-user
data directory (`%LOCALAPPDATA%\lingling` on Windows, `~/.local/share/lingling`
on Linux, `~/Library/Application Support/lingling` on macOS; override with
`LINGLING_DATA_DIR`). After that, launching looks like this — a few seconds
of kitchen noises while the first lane cooks, then opencode opens:

```
 ⠋ glazing the tunnel
 served! lanes are hot -- proof is in the other window.
```

### "Sure, but how do I know it's not lying?"

Fair. That's what the proof window is for. A second console pops up next to
your editor and shows every single model call, live: which lane it rode,
which country, which exit IP, what came back, how big, how fast.

```
13:52:55  #3.1  lane 2 {de} 185.220.100.243  ->  muse-spark-1.2-contributor-free
13:53:00    | #3.1 200 81.4 KB in 2.7s
13:53:01  #4.1  lane 3 {nl} 192.42.116.113  ->  muse-spark-1.2-contributor-free
13:53:03    | #4.1 200 79.6 KB in 2.6s
13:53:07  * lane 4 hit a hidden limit -- your traffic moved to a fresh lane
```

No gaslighting. If the pane says bytes are flowing through a German exit,
your tokens are flowing through a German exit. `--no-proof` if you trust us.
(You shouldn't trust anyone. That's the point of the window.)

### How a limit dies without you noticing

A Tor tunnel is end-to-end TLS, so a 429 can't be spotted in flight — instead,
every 45 seconds each lane fires a real probe at the upstream and rats itself
out. The moment a lane comes back throttled:

1. it's yanked from rotation **before** your next request would have touched
   it — your traffic just... continues, elsewhere;
2. the throttled lane is taken out back and re-cooked from scratch: fresh
   tor process, fresh keys, fresh guards, fresh exit IP. We don't negotiate
   with rate limits;
3. if the same lane keeps getting burned, it gets a new country too
   (`us → de → nl → fr → ro → …`). A new personality, basically.

Lanes that just fall over get poked, restarted, re-cooked, and — if they're
truly hopeless — benched with a note in the proof window. You'll see the whole
drama live, which is honestly half the fun.

### "Wait, how can it see each request? Isn't TLS encrypted?"

Very observant. opencode holds one TLS connection open for your whole
session, so a normal proxy sees one opaque pipe, not individual calls. So
Lingling does a magic trick: it mints a throwaway certificate authority **on
your own machine**, hands it to opencode (and only opencode), and unwraps
`opencode.ai` traffic just long enough to read the model name off each
request before re-encrypting it through a lane to the real server.

Everything stays end-to-end TLS over Tor. The only new thing that can see
your traffic is... your own computer, which could already see your traffic.
`LINGLING_NO_MITM=1` turns the trick off if it makes you itchy (you'll get
per-tunnel proof instead of per-request).

### Fresh every time

Every launch wipes lane keys, guards, and circuit state — yesterday's exits
are dead to us. The only thing kept is tor's cached copy of the public relay
directory, because re-downloading that every launch is how you turn a
five-second boot into a two-minute boot. Orphaned `tor.exe` processes from a
crashed run are swept up before new lanes take their ports.

### Flags Lingling keeps for itself

| Flag | What it does |
|---|---|
| `--lanes N` | how many lanes to keep warm (default 5, env `LINGLING_TOR_COUNT`) |
| `--no-tor` | skip the lanes, run opencode on your own IP like a civilian |
| `--no-proof` | no proof window (coward's mode) |
| `--demo [question]` | fire one real Muse Spark request through a lane, show receipts |

Everything else is handed to opencode byte-for-byte. We don't touch it,
we don't parse it, we don't want to know.

### Under the hood

```
lingling/cli.py      entry point, loader, opencode launch
lingling/lanes.py    Tor lane lifecycle: boot, regenerate, country rotation
lingling/relay.py    local CONNECT relay, least-busy lane picking, MITM handoff
lingling/mitm.py     local CA + per-request interception for opencode.ai
lingling/health.py   probe daemon: burns, deaths, dramatic recoveries
lingling/proof.py    the proof window (event log + live tail)
lingling/netutil.py  raw SOCKS5 / HTTPS-over-SOCKS primitives
lingling/demo.py     lingling --demo
lingling/winjob.py   Windows Job Object so tor.exe dies with us
```

Runtime state (tor, lanes, proof log, local CA) lives in the per-user data
directory — never in the package, never in your project folder.

Requirements: Python 3.11+, Windows/macOS/Linux, and `opencode` on your PATH.
Runs entirely on your machine. Nothing calls home. The kitchen is local.
