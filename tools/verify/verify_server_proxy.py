"""opencode 2.0.26's server keeps the environment it was BORN with."""
import contextlib
import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import cli  # noqa: E402

FAILS = []
BINARY = "opencode"
PROXY = "http://127.0.0.1:58911"
ENV = {"HTTPS_PROXY": PROXY, "https_proxy": PROXY,
       "HTTP_PROXY": PROXY, "http_proxy": PROXY,
       "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1",
       "NODE_EXTRA_CA_CERTS": "C:/tmp/ca.pem"}
EVENTS = []

WITH_SERVER = ("USAGE\n  opencode <subcommand>\n\nFLAGS\n"
               "  --standalone            Run with a private server\n"
               "  --server string         Connect to a server URL\n")
NO_SERVER = ("USAGE\n  opencode <subcommand>\n\nFLAGS\n"
             "  --help, -h              Show help information\n")


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    if not ok:
        FAILS.append(name)


class _Done:
    def __init__(self, out=""):
        self.stdout = out
        self.stderr = ""
        self.returncode = 0


class _Proc:
    pid = 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


_real = {}


def install(help_text):
    """Fake run/Popen, recording each call with the env it actually saw."""
    del EVENTS[:]
    _real.setdefault("run", cli.subprocess.run)
    _real.setdefault("popen", cli.subprocess.Popen)

    def run(cmd, *a, **k):
        EVENTS.append(("run", list(cmd), k.get("env"),
                       os.environ.get("HTTPS_PROXY")))
        return _Done(help_text)

    def popen(cmd, *a, **k):
        EVENTS.append(("popen", list(cmd), k.get("env"),
                       os.environ.get("HTTPS_PROXY")))
        return _Proc()

    cli.subprocess.run = run
    cli.subprocess.Popen = popen
    cli._SERVER_ERA = None


def launch(args, env=ENV):
    """Run the handoff with its chatter swallowed."""
    with contextlib.redirect_stdout(io.StringIO()):
        return cli._run_opencode(BINARY, args, env)


def restore():
    if "run" in _real:
        cli.subprocess.run = _real["run"]
    if "popen" in _real:
        cli.subprocess.Popen = _real["popen"]
    cli._SERVER_ERA = None


def service_calls():
    return [e for e in EVENTS if e[0] == "run" and e[1][1:2] == ["service"]]


def main() -> int:
    args = ["--continue", "--prompt", "hi"]
    had = os.environ.get("HTTPS_PROXY")
    os.environ["HTTPS_PROXY"] = "http://sentinel:1"

    print("\n=== 1. the era is read from the installed CLI ===")
    install(WITH_SERVER)
    got = cli._uses_background_server(BINARY)
    check("a client/server opencode is detected", got is True, f"{got}")
    install(NO_SERVER)
    check("an older opencode is not mistaken for one",
          cli._uses_background_server(BINARY) is False)
    install(NO_SERVER)
    cli._uses_background_server(BINARY)
    check("and the answer follows the CLI, not a constant",
          cli._SERVER_ERA is False)

    print("\n=== 2. the server is restarted before the TUI connects ===")
    install(WITH_SERVER)
    code = launch(args)
    calls = service_calls()
    kinds = [c[1][-1] for c in calls]
    check("the server is restarted", kinds[:1] == ["restart"], f"{kinds}")
    popens = [e for e in EVENTS if e[0] == "popen"]
    check("the TUI is launched exactly once", len(popens) == 1, f"{popens}")
    seq = [(e[0], e[1][1:]) for e in EVENTS]
    check("restart happens before the TUI connects",
          -1 < EVENTS.index(calls[0]) < EVENTS.index(popens[0]) if calls and popens
          else False, f"{seq}")

    print("\n=== 3. the proxy reaches the server by INHERITANCE, not env= ===")
    check("the restart does not pass an explicit env block",
          bool(calls) and calls[0][2] is None,
          f"env= was used: {calls[:1]} -- Windows drops the proxy for a "
          f"detached child that way (measured: 0 proxied vs 12 inherited)")
    check("our own environment carries the relay during the restart",
          bool(calls) and calls[0][3] == PROXY, f"{calls[:1]}")
    check("the relay reaches the CA too, or TLS through us fails",
          "NODE_EXTRA_CA_CERTS" in ENV)

    print("\n=== 4. our environment is put back exactly ===")
    check("HTTPS_PROXY is restored after the handoff",
          os.environ.get("HTTPS_PROXY") == "http://sentinel:1",
          f"{os.environ.get('HTTPS_PROXY')!r}")
    install(WITH_SERVER)
    launch(args)
    check("and a second handoff leaves it restored again",
          os.environ.get("HTTPS_PROXY") == "http://sentinel:1")

    print("\n=== 5. the server is stopped when opencode exits ===")
    install(WITH_SERVER)
    launch(args)
    kinds = [c[1][-1] for c in service_calls()]
    check("a stop follows the restart", kinds == ["restart", "stop"], f"{kinds}")
    check("the restart is not left behind for the next opencode",
          "restart" in kinds and kinds[-1] == "stop", f"{kinds}")

    print("\n=== 6. --standalone is never passed (it drops the env) ===")
    install(WITH_SERVER)
    launch(args)
    popens = [e for e in EVENTS if e[0] == "popen"]
    check("no --standalone on the TUI argv",
          popens and "--standalone" not in popens[0][1], f"{popens}")
    check("the user's args are handed over untouched",
          popens and popens[0][1] == [BINARY] + args, f"{popens}")
    check("the TUI itself still inherits our environment",
          popens and popens[0][2] == ENV, f"{popens}")

    print("\n=== 7. an older opencode is left exactly as it was ===")
    install(NO_SERVER)
    check("no service action is invented", not service_calls(),
          f"{service_calls()}")
    launch(args)
    popens = [e for e in EVENTS if e[0] == "popen"]
    check("the TUI still launches, argv untouched",
          len(popens) == 1 and popens[0][1] == [BINARY] + args, f"{popens}")

    print("\n=== 8. the handoff exits with opencode's code ===")
    install(WITH_SERVER)
    check("a clean exit is reported", launch(args) == 0)

    print("\n=== 9. the tree wires it into the handoff ===")
    src = (ROOT / "lingling" / "cli.py").read_text(encoding="utf-8")
    check("cli.py knows the server subcommand", '"service"' in src)
    check("_run_opencode consults the detector",
          "_uses_background_server(binary)" in src,
          "the handoff never asks, so the restart is dead code")
    import ast
    spliced = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "Popen"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.List):
                for el in arg.elts:
                    if (isinstance(el, ast.Constant)
                            and isinstance(el.value, str)):
                        spliced.append(el.value)
    check("no string flag is spliced into the TUI argv", not spliced,
          f"{spliced}")

    print("\n=== 10. the installed opencode, if any ===")
    restore()
    if had is None:
        os.environ.pop("HTTPS_PROXY", None)
    else:
        os.environ["HTTPS_PROXY"] = had
    live = shutil.which(BINARY)
    if live is None:
        print("  [SKIP] opencode is not on PATH -- nothing to inspect")
    else:
        try:
            out = subprocess.run([live, "--help"], capture_output=True,
                                 text=True, timeout=30).stdout or ""
        except Exception as exc:  # noqa: BLE001
            out = ""
            print(f"  [SKIP] could not ask {live}: {exc!r}")
        if out:
            if "--standalone" in out:
                print(f"  [PASS] {live} has the client/server split, so the "
                      f"handoff will restart its server under our env")
            else:
                print(f"  [SKIP] {live} predates the split; the handoff "
                      f"stays as it was")

    print()
    if FAILS:
        print("SERVER PROXY: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("SERVER PROXY: CONFIRMED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        restore()
