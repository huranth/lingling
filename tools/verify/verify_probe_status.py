"""The health probe must get 403 or 429 -- never 401."""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import health, netutil  # noqa: E402
from lingling.cli import DATA_DIR, load_countries  # noqa: E402
from lingling.health import PROBE_MODEL, PROBE_PATH, _scan_body  # noqa: E402
from lingling.lanes import TorManager  # noqa: E402

FAILS = []
PLACEHOLDER = (b'{"model":"x","stream":false,"max_output_tokens":1,'
               b'"input":[{"role":"user","content":'
               b'[{"type":"input_text","text":"x"}]}]}')
BODY = _scan_body(PROBE_MODEL, PROBE_PATH)

#: the (model, path) pairs measured to answer a verdict, and one that does not
GOOD_PAIRS = [
    ("muse-spark-1.3-contributor-free", "/zen/v1/responses"),
    ("mimo-v2.6-flash-free", "/zen/v1/chat/completions"),
]


def check(name, ok, detail=""):
    """Detail is the failure reason, so only show it when it failed."""
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    if not ok:
        FAILS.append(name)


def send(port, body, path=PROBE_PATH):
    try:
        code, raw = netutil.https_via_socks(
            port, "opencode.ai", "POST", path, "opencode/1.0",
            body=body, timeout=25.0)
        return code, raw.decode("utf-8", "replace")[:160].replace("\n", " ")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def live_session() -> bool:
    """True when a lingling or opencode process is already running.

    The live half boots a lane on the real data dir, and a boot reaps the
    orphan tor processes holding lane ports. Against a live session that
    is not a reap, it is a kill.
    """
    import subprocess
    try:
        out = subprocess.check_output(["tasklist"], text=True, timeout=15)
    except Exception:  # noqa: BLE001
        return False
    low = out.lower()
    return "lingling.exe" in low or "opencode.exe" in low


def main():
    print("=== offline: the shipped probe body names a real model ===")
    model = json.loads(BODY.decode())["model"]
    print(f"  PROBE_MODEL = {model!r}")
    check("the probe body does not use a placeholder model",
          len(model) > 8 and "-" in model,
          f"model={model!r} looks like a placeholder")
    check("the probe body is not the known-bad placeholder",
          BODY != PLACEHOLDER, "shipped body is the placeholder")

    print("\n=== live: boot a lane and ask it both ways ===")
    if live_session():
        print("  [SKIP] a lingling/opencode session is running -- refusing "
              "to touch the lane ports.")
        return finish()
    countries, fallback, preferred = load_countries()
    mgr = TorManager(DATA_DIR, count=1, exit_countries=countries,
                     fallback_countries=fallback,
                     preferred_countries=preferred, log=lambda *a: None)
    try:
        err = mgr.setup_lanes()
        if err:
            check("a lane can be set up", False, err)
            return finish()
        mgr.start_all()
        lane = mgr.lanes[0]
        for _ in range(60):
            if netutil.port_is_open("127.0.0.1", lane.socks_port, timeout=1.0):
                break
            time.sleep(1)
        if not netutil.port_is_open("127.0.0.1", lane.socks_port, timeout=1.0):
            check("a lane comes up", False, "socks port never opened")
            return finish()
        print(f"  lane 1: {lane.exit_country} {lane.exit_ip}")

        code, text = send(lane.socks_port, PLACEHOLDER)
        print(f"\n  placeholder model -> {code}  {text!r}")
        check("the placeholder reproduces the bug (so this test can catch it)",
              code == 401, f"got {code}")

        time.sleep(1.5)
        code2, text2 = send(lane.socks_port, BODY)
        print(f"  shipped body      -> {code2}  {text2!r}")
        check("the shipped probe gets 403 or 429, never 401",
              code2 in (403, 429), f"got {code2}")
        check("the probe did not spend quota (403 = gate refused it)",
              code2 in (403, 429), f"got {code2}")

        print("\n=== live: the (model, path) pairs, so the override is safe ===")
        # The model and the path are a PAIR.
        for model, path in GOOD_PAIRS:
            time.sleep(1.5)
            code3, text3 = send(lane.socks_port, _scan_body(model, path), path)
            print(f"  {model:32} on {path:26} -> {code3}")
            check(f"{model} on {path} answers a verdict",
                  code3 in health.PROBE_VERDICTS,
                  f"got {code3} -- {text3!r}. A non-verdict means this pair "
                  f"cannot be shipped as PROBE_MODEL/PROBE_PATH")
    finally:
        mgr.stop_all()
        print("\n  [stop] lanes down")
    return finish()


def finish():
    print()
    if FAILS:
        print("PROBE STATUS: FAILED")
        return 1
    print("PROBE STATUS: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
