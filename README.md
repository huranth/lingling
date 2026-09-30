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

State lives under the OS temp dir (`%LOCALAPPDATA%\Temp\lingling-data` on
Windows; override with `LINGLING_DATA_DIR`) — the OS's own cleanup owns any
residue. The first run downloads the Tor Expert Bundle (~30 MB) there, once.
Launching shows exactly one pinned line until the lanes are hot:

```
 ⠙ first run -- pulling the relay directory, a few minutes, once  47s
 served! lanes are hot -- proof is in the other window.
```

One session at a time: launching a second `lingling` while one is running
prints `lingling is already running (pid N) -- one kitchen, one session.`
and exits. A crashed session leaves nothing behind — the lock self-clears.

### "Sure, but how do I know it's not lying?"

Fair. That's what the proof window is for. A second console pops up next to
your editor and shows every single model call, live: which lane it rode,
which country, which exit IP, what came back, how big, how fast.

```
13:52:55  #3.1  lane 2 {de} 185.220.100.243  ->  muse-spark-1.2-contributor-free
13:53:00    | #3.1 200 81.4 KB in 2.7s  first 0.7s  event 1.1s  reused tunnel
13:53:01  #4.1  lane 3 {nl} 192.42.116.113  ->  muse-spark-1.2-contributor-free
13:53:03    | #4.1 200 79.6 KB in 2.6s  first 0.6s  event 1.0s  reused tunnel
13:53:07    | #5.1 429 exit limited 146m -- moving lanes
13:53:07  * lane 3 keeps hitting the limit -- moving to a new country {se}
13:53:09  * 4 lanes busy at cap -- holding this request for a free lane
```

No gaslighting. If the pane says bytes are flowing through a German exit,
your tokens are flowing through a German exit. `--no-proof` if you trust us.
(You shouldn't trust anyone. That's the point of the window.)

### How a limit dies without you noticing

Two different things can go wrong, and they are kept strictly apart.

**The limit.** opencode's free tier is per exit IP, and a 429 means that IP
has spent its allowance for the window. opencode tells us exactly when it
comes back, via `retry-after` — often hours. So the lane's relay is written
off until then, the lane is pinned to a **different** relay in the same
country, and your request is already elsewhere. Keep hitting the limit and the
lane changes country too; keep going and it gets re-cooked from scratch. The
proof window names the reset it was told: `429 exit limited 146m`.

**Busy.** A lane carrying as many requests as we allow is *busy* — loaded,
healthy, still ours. Nothing is retired for it. Your request waits for a free
lane instead, and the pane says so once: `4 lanes busy at cap`, or
`no lane available` when the lanes are cooking rather than loaded. Busy is
never confused with the limit, because retiring a healthy exit over it would
be exactly the wrong move.

**The far end's bad moments.** A 502, 503 or 504 means opencode's edge is
unwell — not your client, and not that exit's quota. A different exit reaches
a different edge, so those are retried on another lane exactly like a 429. A
plain 500 is *not* retried: that may be a genuine error, and repeating it
would just fail twice.

A Tor tunnel is end-to-end TLS, so a 429 can't be spotted in flight. Each lane
therefore probes the upstream periodically and reports on itself, and a lane
that just carried real traffic is left alone rather than probed. A lane that
merely falls over gets poked, restarted, and — if it is truly hopeless —
benched with a note. You'll see the whole drama live, which is half the fun.

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

Every lane is pinned to its **own** exit relay, chosen from the live Tor
consensus, and no two lanes ever share one. Which relay each lane has used is
remembered, so a fresh start hands you exits you have not been through lately
instead of the same handful every launch — freshness is preferred over
bandwidth precisely because the busiest relays are the most heavily shared.
That memory expires on its own, so a relay always comes back.

The only thing carried over besides that is tor's cached copy of the public
relay directory, because re-downloading it every launch turns a five-second
boot into a two-minute one. Stale processes still holding a lane's port are
cleared before new lanes take it — scoped to our own ports, never a blanket
sweep that could catch an unrelated `tor.exe`.

### Pick your own countries

By default lanes rotate through a generic pool (us, de, nl, fr, ro, gb, ca,
se, pl, ch). Want different exits? Drop a `countries.txt` into Lingling's
data directory (`%LOCALAPPDATA%\Temp\lingling-data` on Windows):

```
# big pools first: the six boot lanes take the first six here
de,nl,se,at,lu,fr,us,no,ch,hu,it,fi
# fallback, used once the primary pool is exhausted
sg,ro,cz,pl,bg,is,dk,za,ca,gb
# optional preferred pool, blank to skip
```

Line 1 is the primary pool lanes boot and rotate through, line 2 is the
fallback pool used once every primary country is limited, and line 3 is an
optional preferred pool lanes stick to first. Two-letter country codes,
comma-separated, one pool per line. `#` starts a comment, and a comment-only
line is ignored entirely. Leave a line blank to skip that pool. No file, no
problem — the defaults are fine.

**Size your pools by relay count.** Each lane is pinned to its *own* exit
relay, so a country needs spare relays to rotate through when one gets
limited. From the live consensus: the Netherlands has 588 usable exits,
Germany 415, Sweden 339, the US 1166 — while Hong Kong has 7, Turkey 8 and
Ukraine 8. A ladder built only from small countries has almost no room to
move. If a country does run out, that lane falls back to letting Tor choose.

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
lingling/lanes.py    Tor lane lifecycle: boot, pin, rotate, re-cook
lingling/exits.py    exit relays from the consensus: fresh, distinct, country-filtered
lingling/relay.py    local CONNECT relay, least-loaded lane picking, MITM handoff
lingling/mitm.py     local CA + per-request interception for opencode.ai
lingling/health.py   probe daemon: limits, deaths, recoveries
lingling/proof.py    the proof window (event log + live tail)
lingling/netutil.py  raw SOCKS5 / HTTPS-over-SOCKS primitives
lingling/demo.py     lingling --demo
lingling/winjob.py   Windows Job Object so tor.exe dies with us

tools/verify/        the three offline suites, a boot check, and a repo audit
tools/soak/          live soak: drives the real opencode client
tools/probe/         tunnel latency probe (no quota)
```

Runtime state (tor, lanes, proof log, local CA) lives in the per-user data
directory — never in the package, never in your project folder.

Requirements: Python 3.11+, Windows/macOS/Linux, and `opencode` on your PATH.
Runs entirely on your machine. Nothing calls home. The kitchen is local.
