"""Which shipped value is actually guarded? Break each one and watch.

    python tools/verify/sweep_guards.py

Why this exists. `verify_audit.py` runs its suites as subprocesses with captured
output, so a clean run shows no banner and says nothing about what ran. Asked
"is the 120s send window protected?", grepping the output suggested NO -- the
suite is in the tuple, its banner is simply swallowed. The only way to know a
guard is live is to break the value and watch it fail.

So this does that, for every env-overridable constant, against the suites that
could plausibly notice. The interesting rows are the ones where NOTHING fails:
a value with no guard, or a guard that passes both ways, which by this
project's own rule is not a guard at all.

Two mechanisms, because they answer differently:

  * a suite that reads the LIVE module value (`mitm._SEND_TIMEOUT`) sees the
    override and can fail on it -- a real guard on the shipped value;
  * a suite that PINS its own window (the convention, to stop hangs) cannot
    see the override at all, so it tests the mechanism while the shipped
    default goes unguarded.

Both are correct for the suite. Only the first protects the number.

Run each suite only where it can notice, and print as it goes: the first
version ran 9 suites per mutation and printed nothing until each row finished,
so one slow suite looked like a hang and told me nothing for eight minutes.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / "tools" / "verify"
#: 45s produced FALSE "HUNG" verdicts on `first_token_grace` and
#: `idle_ceiling`, which are slow by design (they let a simulated stream run
#: past a ceiling), and that silently turned three guarded values into
#: "UNGUARDED". The audit allows 240s; use the same here, or the instrument
#: reports its own timeout as a finding.
TIMEOUT = 240

#: shipped default -> a value that would be wrong -> the suites that could see it
MUTATIONS = (
    ({}, "control (shipped)",
     ("send_window", "reused_send", "pool_ttl", "first_token_grace",
      "cold_connect", "committed_stream", "client_stall", "idle_ceiling",
      "window_floors")),
    ({"LINGLING_SEND_S": "30"}, "SEND 120->30 (the reverted value)",
     ("send_window", "reused_send")),
    ({"LINGLING_KEEPALIVE_S": "30"}, "KEEPALIVE 600->30 (below his 70s lap)",
     ("pool_ttl",)),
    # `window_floors` is the only one of these that reads the LIVE value; the
    # pinning suites are kept as witnesses that the mechanism still passes
    ({"LINGLING_FIRST_BYTE_S": "2"}, "FIRST_BYTE 30->2",
     ("first_token_grace", "cold_connect", "committed_stream",
      "window_floors")),
    ({"LINGLING_STREAM_TIMEOUT": "2"}, "READ 1800->2",
     ("committed_stream", "client_stall", "window_floors")),
    ({"LINGLING_STREAM_IDLE_S": "2"}, "IDLE 1800->2",
     ("idle_ceiling", "committed_stream", "client_stall", "window_floors")),
)


def run(suite, env):
    """(ok|None, note) for one suite under one env."""
    e = dict(os.environ)
    e.update(env)
    t0 = time.time()
    try:
        res = subprocess.run([sys.executable, str(VERIFY / f"verify_{suite}.py")],
                             cwd=str(ROOT), env=e, capture_output=True,
                             text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, f"HUNG >{TIMEOUT}s"
    secs = time.time() - t0
    if res.returncode == 0:
        return True, f"{secs:5.1f}s ok"
    why = next((ln.strip()[2:] for ln in (res.stdout + res.stderr).splitlines()
                if ln.strip().startswith("- ")), "")
    return False, f"{secs:5.1f}s FAIL {why[:56]}"


def main():
    # a bare `python sweep_guards.py READ IDLE` re-runs only those rows, so a
    # timeout that was too tight can be corrected without paying for the whole
    # sweep again -- the full run is minutes, and the slow suites dominate it
    want = [a.upper() for a in sys.argv[1:]]
    results = {}
    for env, label, suites in MUTATIONS:
        if want and not any(w in label.upper() for w in want):
            continue
        print(f"\n=== {label} ===", flush=True)
        failed = []
        for s in suites:
            ok, note = run(s, env)
            print(f"  {s:<20} {note}", flush=True)
            if ok is None:
                failed.append((s, "HUNG"))
            elif not ok:
                failed.append((s, note))
        results[label] = failed

    print("\n=== coverage map ===", flush=True)
    base = {s for s, _ in results["control (shipped)"]}
    if base:
        print(f"  control already fails {sorted(base)} -- fix before reading")
    for env, label, _ in MUTATIONS:
        # a filtered run has no row for the envs it skipped
        if not env or label not in results:
            continue
        hit = sorted({s for s, _ in results[label]} - base)
        knob = label.split()[0]
        if hit:
            print(f"  {knob:<12} GUARDED  by {', '.join(hit)}")
        else:
            print(f"  {knob:<12} UNGUARDED -- no suite fails when it is wrong")
    return 0


if __name__ == "__main__":
    sys.exit(main())
