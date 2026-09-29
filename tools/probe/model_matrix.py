"""Does the probe's model name change its verdict?"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import netutil  # noqa: E402
from lingling.cli import DATA_DIR, load_countries  # noqa: E402
from lingling.lanes import TorManager  # noqa: E402

HOST = "opencode.ai"
RESPONSES = "/zen/v1/responses"
CHAT = "/zen/v1/chat/completions"

#: the shipped probe default, the model the owner actually rides, a control
MODELS = [
    "muse-spark-1.3-contributor-free",
    "mimo-v2.6-flash-free",
    "definitely-not-a-real-model-xyz",
]

#: what the answer means
MEANING = {
    403: "exit is fine (the gate refused it) -- limit check passed",
    429: "this exit's quota is spent",
    401: "THE MODEL NAME IS REJECTED -- a probe that returns this is not a"
         " verdict about the lane at all",
    400: "request refused as malformed -- not a lane verdict",
    0: "nothing came back",
}


def responses_body(model: str) -> bytes:
    return (b'{"model":"' + model.encode() + b'","stream":false,'
            b'"max_output_tokens":1,'
            b'"input":[{"role":"user","content":'
            b'[{"type":"input_text","text":"x"}]}]}')


def chat_body(model: str) -> bytes:
    return (b'{"model":"' + model.encode() + b'","stream":false,'
            b'"max_tokens":1,'
            b'"messages":[{"role":"user","content":"x"}]}')


def send(port: int, path: str, body: bytes):
    try:
        code, raw = netutil.https_via_socks(
            port, HOST, "POST", path, "opencode/1.0", body=body, timeout=25.0)
        return code, raw.decode("utf-8", "replace")[:110].replace("\n", " ")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def main():
    countries, fallback, preferred = load_countries()
    mgr = TorManager(DATA_DIR, count=1, exit_countries=countries,
                     fallback_countries=fallback,
                     preferred_countries=preferred, log=lambda *a: None)
    codes = {}
    try:
        err = mgr.setup_lanes()
        if err:
            print(f"setup_lanes: {err}")
            return 1
        mgr.start_all()
        lane = mgr.lanes[0]
        for _ in range(60):
            if netutil.port_is_open("127.0.0.1", lane.socks_port, timeout=1.0):
                break
            time.sleep(1)
        if not netutil.port_is_open("127.0.0.1", lane.socks_port, timeout=1.0):
            print("the lane's socks port never opened")
            return 1
        print(f"lane 1: {lane.exit_country} {lane.exit_ip}\n")

        for path, build in ((RESPONSES, responses_body), (CHAT, chat_body)):
            print(f"=== POST {path} ===")
            for model in MODELS:
                code, text = send(lane.socks_port, path, build(model))
                codes[(path, model)] = code
                print(f"  {model:34} -> {code:3}  {MEANING.get(code, '?')}")
                print(f"       {text!r}")
                time.sleep(1.5)
            print()

        print("=== what this decides ===")
        shipped = codes.get((RESPONSES, MODELS[0]))
        live = codes.get((RESPONSES, MODELS[1]))
        bogus = codes.get((RESPONSES, MODELS[2]))
        print(f"  shipped probe model on {RESPONSES} : {shipped}")
        print(f"  the model actually in use          : {live}")
        print(f"  a deliberately bogus name          : {bogus}")
        print()
        if bogus == 401:
            print("  The bogus name returns 401, so a rejected name IS silent:")
            print("  `check_once` reads a truthy code as healthy, so the 429")
            print("  branch would never fire. Confirmed -- the guard matters.")
        else:
            print(f"  A bogus name returned {bogus}, NOT 401. The assumption in")
            print("  the docstring does not hold on this endpoint today.")
        print()
        if live == shipped:
            print(f"  Both real models answer {shipped}, so the NAME does not")
            print("  gate the verdict and this probe cannot see a per-model")
            print("  quota. Wiring it to the model in use would change nothing")
            print("  measurable -- keep the constant and the env override.")
        else:
            print(f"  The two real models DIFFER ({shipped} vs {live}), so the")
            print("  probe's name changes its verdict. Wire it to the model in")
            print("  use, or its 403 is evidence about the wrong model.")
    finally:
        mgr.stop_all()
        print("\n[stop] lane down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
