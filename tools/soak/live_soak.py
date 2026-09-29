"""Live soak: boot the real stack, then drive the REAL opencode client through
the relay until ~N model calls have gone upstream. AppData-only.

    python tools/soak/live_soak.py [target_model_calls]

Why it drives opencode instead of raw HTTP: the free tier gates on the
client. A hand-rolled request to /zen/v1/responses returns

    403 FreeTierError "OpenCode's free tier can only be used from within OpenCode"

no matter which session id or headers you send. The real binary rides fine, so
this is also the only honest way to reproduce your actual usage.

**SOAK_CONCURRENCY=1 IS THE OWNER'S PATTERN. THE DEFAULT 5 IS NOT.**

His requests arrive strictly sequentially, one at a time, round-robin across
his lanes. This harness defaults to five parallel opencode processes, and five
parallel clients re-hit a lane within seconds -- so it reported 78-94% tunnel
reuse while his own sessions sat at **0%**, and the pool TTL that caused it
(30s against a 98.8s minimum same-lane lap) was invisible for as long as the
harness ran at the default. A soak that cannot reproduce the traffic cannot
find the bug in it: **run this at concurrency 1 to study his latency.**

Also: never start the lingling CLI while this is running. `setup_lanes` prunes
any `tor-<digits>` dir the current pool does not own and `_reap_orphans` sweeps
the whole lane port range, so a CLI boot mid-soak takes the soak's lanes down.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

sys.path.insert(0, r"C:\Users\W\AppData\Local\Programs\Python\Python312\Lib\site-packages")

from lingling import mitm, proof
from lingling.cli import DATA_DIR, load_countries
from lingling.health import HealthDaemon
from lingling.lanes import TorManager
from lingling.relay import Relay

MODEL = "muse-spark-1.3-contributor-free"
PROOF_LOG = DATA_DIR / "proof.log"
#: A hung run must not outlive this.
RUN_TIMEOUT = 180.0

#: Two prompt pools, because the stall needs LONG reasoning answers.
#:
#: The first pool is the original one: one-word replies, ~1 KB of response.
#: It cannot reproduce the owner's stall, because that arrives on answers of
#: 48-62 KB. A soak that only ever asks for "PROOF_OK" will run for a week and
#: never once generate the failure it is meant to study. So `--hard` swaps in
#: prompts that force sustained reasoning, which is the only way the effort
#: field logged by `_model_of` gets anything to correlate against.
PROMPTS = [
    "Reply with exactly: PROOF_OK",
    "Reply with exactly: LANE_1",
    "Reply with exactly: ALIVE",
    "Reply with exactly: OK_2",
    "Reply with exactly: PING",
    "Reply with exactly: TEST_A",
    "Reply with exactly: TEST_B",
    "Reply with exactly: GOOD",
    "Reply with exactly: READY",
    "Reply with exactly: FINE",
]

#: Long-reasoning prompts: each wants a real derivation, not a token.
HARD_PROMPTS = [
    "Derive the time complexity of a red-black tree insertion, proving each "
    "step, then compare it against a B-tree with the same key count.",
    "Design a rate limiter for 10 million requests per second across three "
    "regions. Give the algorithm, its failure modes, and the tradeoffs.",
    "Prove that every finite integral domain is a field, then explain where "
    "the argument breaks for infinite domains.",
    "Write a correct LRU cache in Python with O(1) operations, then prove "
    "the amortised bound of each method.",
    "Explain why TCP congestion control converges, deriving the AIMD "
    "equilibrium from first principles and noting the assumptions.",
    "Given a stream of integers, design a structure returning the median in "
    "O(log n) with O(n) memory. Prove both bounds.",
]


def boot(wait_healthy=4, timeout=420):
    countries, fallback, preferred = load_countries()
    mgr = TorManager(DATA_DIR, count=6, exit_countries=countries,
                     fallback_countries=fallback,
                     preferred_countries=preferred, log=lambda *a: None)
    err = mgr.setup_lanes()
    assert not err, f"setup_lanes: {err}"
    emit = proof.make_emitter(PROOF_LOG)
    #: session marker, as the CLI emits -- without it a soak run cannot be
    #: told apart from the run before it
    emit({"type": "start", "t": time.time(), "session": os.urandom(6).hex(),
          "lanes": len(mgr.lanes), "countries": list(mgr.countries),
          "version": "soak"})
    daemon = HealthDaemon(mgr, event=emit, log=lambda *a: None)

    def _report_boot(lane, status) -> None:
        """Put the launch outcome in the LOG, not just on stdout.

        The soak printed `[boot] healthy lanes: N` and that was the only place
        a failed lane showed up -- so a run that had lost five of six lanes
        looked identical to a healthy one in `proof.log`."""
        if status in ("started", "already_running"):
            return
        emit({"type": "lane", "kind": "fail", "t": time.time(),
              "lane": lane.index, "cc": lane.exit_country, "ip": "",
              "msg": f"lane {lane.index} {{{lane.exit_country}}} did not come "
                     f"up ({status})"})

    mgr.start_all(on_lane=_report_boot)
    daemon.start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(mgr.healthy_lanes()) >= wait_healthy:
            break
        time.sleep(3)
    print(f"[boot] healthy lanes: {len(mgr.healthy_lanes())}", flush=True)
    relay = Relay(mgr, event=emit)
    port = relay.start()
    relay.cert_shop = mitm.CertShop(DATA_DIR / "mitm")
    relay.tunnels = mitm.TunnelPool()
    print(f"[boot] relay port: {port}", flush=True)
    return mgr, daemon, relay, port


class LogTail:
    """Incremental reader so we can watch model calls land without re-parsing
    a multi-megabyte log on every poll."""

    def __init__(self, path):
        self.path = path
        self.off = path.stat().st_size if path.exists() else 0
        self.buf = b""
        self.calls = 0
        self.call_lanes = []
        self.ends = []
        self.lanes = []

    def poll(self):
        try:
            with open(self.path, "rb") as f:
                f.seek(self.off)
                data = f.read()
                self.off = f.tell()
        except OSError:
            return
        if not data:
            return
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                r = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(r, dict):
                continue
            t = r.get("type")
            if t == "call":
                self.calls += 1
                self.call_lanes.append(r.get("lane"))
            elif t == "callend":
                self.ends.append(r)
            elif t == "lane":
                self.lanes.append(r)


def kill_tree(pid):
    """Kill children too. Killing only the launcher leaves node holding the
    stdout pipe open, and the reap then blocks until those exit on their own."""
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001
        pass


def run_once(oc, env, i, pool=None):
    t0 = time.monotonic()
    prompts = pool or PROMPTS
    prompt = prompts[i % len(prompts)]
    hard = prompts is HARD_PROMPTS
    try:
        cmd = [oc, "run", "-m", "opencode/" + MODEL]
        if hard:
            # Ask for the effort the owner's failing calls carry. opencode
            # passes this through to the model path, so the relay's new
            # `effort=` field has something real to report.
            #
            # The variant is settable because it CHANGES the failure rate and
            # a soak pinned to one value cannot see that: the owner's failing
            # calls are `xhigh`, and every soak here ran `high` or lower, which
            # is why the time-to-first-token that breaks his traffic was never
            # reproduced. SOAK_VARIANT=xhigh to match him.
            cmd += ["--variant", os.environ.get("SOAK_VARIANT", "high")]
        cmd.append(prompt)
        proc = subprocess.Popen(
            cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=r"C:\Users\W")
    except Exception as exc:  # noqa: BLE001
        return i, -2, time.monotonic() - t0, type(exc).__name__
    try:
        out, _ = proc.communicate(timeout=RUN_TIMEOUT)
        return i, proc.returncode, time.monotonic() - t0, (out or "").strip()
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        return i, -1, time.monotonic() - t0, "timeout"


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    target = int(argv[0]) if argv else 400
    conc = int(os.environ.get("SOAK_CONCURRENCY", "5"))
    budget = float(os.environ.get("SOAK_BUDGET_S", "2700"))
    # `--hard` is what makes the soak able to reproduce the stall at all:
    # short prompts produce ~1 KB answers, and the owner's failures arrive on
    # 48-62 KB ones.
    pool = HARD_PROMPTS if "--hard" in flags else PROMPTS
    print(f"[soak] prompt pool: "
          f"{'HARD (long reasoning)' if pool is HARD_PROMPTS else 'short'}"
          f"  target={target}", flush=True)

    mgr, daemon, relay, port = boot()
    ca = str(DATA_DIR / "mitm" / "ca.pem")
    oc = shutil.which("opencode") or \
        r"C:\Users\W\AppData\Roaming\npm\node_modules\opencode-ai\bin\opencode.exe"
    env = dict(os.environ)
    env["HTTPS_PROXY"] = f"http://127.0.0.1:{port}"
    env["HTTP_PROXY"] = f"http://127.0.0.1:{port}"
    env["NO_PROXY"] = "localhost,127.0.0.1"
    env["NODE_EXTRA_CA_CERTS"] = ca

    tail = LogTail(PROOF_LOG)
    print(f"[soak] target={target} model calls  concurrency={conc}  "
          f"budget={budget:.0f}s", flush=True)
    t0 = time.monotonic()
    results = []
    runs = 0
    with ThreadPoolExecutor(max_workers=conc) as ex:
        pending = set()
        while True:
            elapsed = time.monotonic() - t0
            tail.poll()
            if tail.calls >= target:
                print(f"[soak] target reached: {tail.calls} calls", flush=True)
                break
            if elapsed > budget:
                print(f"[soak] budget hit at {tail.calls} calls", flush=True)
                break
            while len(pending) < conc:
                pending.add(ex.submit(run_once, oc, env, runs, pool))
                runs += 1
            done, pending = wait(pending, timeout=1.0,
                                 return_when=FIRST_COMPLETED)
            for f in done:
                results.append(f.result())
            if runs % 10 == 0 and done:
                print(f"  runs={runs} calls={tail.calls} "
                      f"elapsed={elapsed:.0f}s ({tail.calls / max(elapsed, 1):.2f} calls/s)",
                      flush=True)
        for f in pending:
            f.cancel()
        # Short drain: a long one only waits on runs we already gave up on.
        done, _ = wait(pending, timeout=30)
        for f in done:
            if not f.cancelled():
                try:
                    results.append(f.result())
                except Exception:  # noqa: BLE001
                    pass
    elapsed = time.monotonic() - t0
    tail.poll()

    codes = Counter(str(e.get("status")) for e in tail.ends)
    secs = sorted(e.get("secs") or 0 for e in tail.ends)
    kbs = [e.get("kb") or 0 for e in tail.ends]
    rcs = Counter(str(r[1]) for r in results)
    oks = sum(1 for r in results if "PROOF_OK" in r[3] or r[1] == 0)

    print("\n" + "=" * 60, flush=True)
    print(f"LIVE SOAK   runs={runs}  model calls={tail.calls}  wall={elapsed:.0f}s")
    print("=" * 60, flush=True)
    print(f"opencode rc  : {dict(rcs)}   (rc=0 = {oks}/{len(results)})")
    print(f"call statuses: {dict(codes)}")
    # A 200 that the far end cut mid-body is NOT a success: the client got a
    # truncated answer. `status` now reports what the client actually saw, so
    # it reads 200 for a committed stream even when the stream then died --
    # which is correct, and exactly why this metric has to check `err` too.
    ok200 = sum(1 for e in tail.ends
                if e.get("status") == 200 and e.get("err") == "")
    tot = max(1, len(tail.ends))
    print(f"success rate : {100 * ok200 / tot:.1f}%  ({ok200}/{tot})")

    def pct(vals, p):
        return vals[min(len(vals) - 1, int(len(vals) * p))]

    if secs:
        print(f"latency      : p50={pct(secs, 0.5):.1f}s "
              f"p90={pct(secs, 0.9):.1f}s max={secs[-1]:.1f}s")
    # 0 means the attempt never got that far, so those drop out of the sample.
    fbs = sorted(e.get("first_byte_s") for e in tail.ends
                 if e.get("first_byte_s"))
    fes = sorted(e.get("first_event_s") for e in tail.ends
                 if e.get("first_event_s"))
    if fbs:
        print(f"first byte   : p50={pct(fbs, 0.5):.2f}s "
              f"p90={pct(fbs, 0.9):.2f}s max={fbs[-1]:.2f}s (n={len(fbs)})")
    if fes:
        print(f"first event  : p50={pct(fes, 0.5):.2f}s "
              f"p90={pct(fes, 0.9):.2f}s max={fes[-1]:.2f}s (n={len(fes)})")
    # A reused tunnel skips the SOCKS5 CONNECT and TLS handshake (~1.27s).
    reuse = [e for e in tail.ends if e.get("reused")]
    if reuse:
        print(f"tunnel reuse : {len(reuse)} of {len(tail.ends)} calls "
              f"({100 * len(reuse) / len(tail.ends):.0f}%)")
        rb = sorted(e["first_byte_s"] for e in reuse if e.get("first_byte_s"))
        if rb:
            print(f"  reused first byte p50={pct(rb, 0.5):.2f}s  "
                  f"vs {pct(fbs, 0.5):.2f}s overall")
    if kbs:
        small = sum(1 for k in kbs if k < 2)
        print(f"payload      : median={sorted(kbs)[len(kbs) // 2]}KB  "
              f"under 2KB={100 * small / len(kbs):.0f}%  max={max(kbs)}KB")
    #: dispatched, not finished -- a callend can be lost if the tail starts
    #: late, and the first calls of a soak are exactly the interesting ones
    lanes_used = Counter(tail.call_lanes)
    print(f"lane spread  : {dict(sorted(lanes_used.items()))}")
    print(f"throughput   : {elapsed / max(1, tail.calls):.2f}s per model call")

    print("\n--- health daemon activity during the soak ---", flush=True)
    kinds = Counter(e.get("kind") for e in tail.lanes)
    print(f"lane events: {dict(kinds) if kinds else 'none'}")
    for e in tail.lanes:
        # `probe` is included deliberately: it means the health probe got a
        # code that is NOT a verdict (401/500/nothing), so the lane was left
        # unasked and its quota was never checked. It is an early warning --
        # in the first run that produced one, lane 2 answered nothing and then
        # failed all three of its next real calls with `timed out`.
        if e.get("kind") in ("limited", "rotate", "up", "fail", "probe"):
            print(f"  [{e.get('kind')}] {e.get('msg')}", flush=True)

    daemon.stop()
    relay.stop()
    mgr.stop_all()

    # Did anything survive? A straggler holds its lane's DataDirectory, and the
    # next run then cannot boot that lane -- measured as one per run, five
    # accumulated, and a pool that quietly ran on a single exit. Checking here
    # is the only way to see it: the pane never mentions a process that has
    # already outlived the run that made it.
    import subprocess as _sp
    _tl = _sp.run(["tasklist", "/FO", "CSV", "/NH"],
                  capture_output=True, text=True).stdout
    _tor = [r for r in _tl.splitlines() if "tor.exe" in r.lower()]
    print(f"\n[teardown] tor.exe still alive: {len(_tor)}", flush=True)
    if _tor:
        print("           ^ a straggler survived stop_all -- it will block a "
              "lane next run", flush=True)


if __name__ == "__main__":
    main()
