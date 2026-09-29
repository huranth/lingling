"""Repo hygiene: does the tree still meet every standard we set?

    python tools/verify/verify_audit.py

Run this before any push. It checks the things that are easy to regress and
hard to notice: comment length, dead code, duplicated logic, stale vocabulary,
files nothing accounts for, and whether the local-only paths are actually
excluded from git. Every check names the standard it enforces, so a failure
says what to fix rather than just that something is wrong.
"""
import ast
import io
import pathlib
import re
import subprocess
import sys
import tokenize

sys.path.insert(0, r"C:\Users\W\AppData\Local\Programs\Python\Python312\Lib\site-packages")

ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG = sorted((ROOT / "lingling").glob("*.py"))
TOOLS = sorted((ROOT / "tools").rglob("*.py"))
ALL_PY = PKG + TOOLS

#: every file the tree is allowed to contain, and why
EXPECTED = {
    ".gitignore": "excludes local state from git",
    "LICENSE": "licence",
    "README.md": "user docs",
    "pyproject.toml": "packaging",
    "requirements.txt": "pinned deps",
}
EXPECTED_DIRS = {"lingling": "the package", "tools": "dev tooling"}

#: vocabulary that no longer describes anything in this codebase
STALE = ("identity", "walled", "burned_cycles", "report_burn", "_heal_burn",
         "avoid_countries", "_IDLE_GUARD", "spent-exits")

FAIL = []


def check(name, cond, detail=""):
    """Print a verdict, and label the detail as a note or a reason.

    Some checks pass an informational detail ("found 6 callend emits") and
    some pass the reason they would fail ("the ceiling behaved like a total
    budget"). This used to print both identically, so a PASSING line read as a
    contradiction:

        PASS  a slow stream outlives the ceiling   the ceiling behaved like a
                                                    total budget

    That is the same failure this whole file exists to prevent -- output that
    says one thing and means another. A parenthesised note is a fact; a bare
    reason after FAIL is a reason."""
    if not cond:
        print(f"  FAIL  {name}" + (f"   {detail}" if detail else ""))
        FAIL.append(name)
    else:
        print(f"  PASS  {name}" + (f"   ({detail})" if detail else ""))


def blob(files):
    return "\n".join(p.read_text(encoding="utf-8") for p in files)


def main():
    src_all = blob(ALL_PY)

    print("=== comment length: at most two words, package ===")
    counts, bad = {}, []
    for p in PKG:
        for t in tokenize.generate_tokens(
                io.StringIO(p.read_text(encoding="utf-8")).readline):
            if t.type == tokenize.COMMENT:
                txt = t.string.lstrip("#").strip().lstrip(":").strip()
                counts[len(txt.split())] = counts.get(len(txt.split()), 0) + 1
                if len(txt.split()) > 2:
                    bad.append(f"{p.name}:{t.start[0]} {txt!r}")
    check("no comment over two words", not bad, "; ".join(bad[:4]))
    print(f"        {sum(counts.values())} comments, lengths {sorted(counts)}")

    print("\n=== dead code ===")
    unused = []
    for p in ALL_PY:
        src = p.read_text(encoding="utf-8")
        imported = {}
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imported[(a.asname or a.name).split(".")[0]] = node.lineno
            elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
                for a in node.names:
                    imported[a.asname or a.name] = node.lineno
        body = re.sub(r"^\s*(import|from)\s+.*$", "", src, flags=re.M)
        for nm, ln in imported.items():
            if not re.search(r"\b" + re.escape(nm) + r"\b", body):
                unused.append(f"{p.name}:{ln} {nm}")
    check("no unused imports", not unused, "; ".join(unused))

    defs = {}
    for p in PKG:
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                defs.setdefault(node.name, []).append(p.name)
    #: Walked, not just module level -- the old check read `ast.parse(...).body`,
    #: so no METHOD was ever examined and dead methods were invisible. And the
    #: search is the PACKAGE only: a function called solely by its own test
    #: suite is dead in the product, and searching src_all hid exactly that.
    #: A mention counts, not just a call, because callbacks are passed by name
    #: (`target=self._loop`) and classes are used as annotations (`lane: Lane`).
    pkg_only = blob(PKG)
    dead = []
    for name in sorted(defs):
        if name.startswith("__"):
            continue
        if len(re.findall(r"\b" + re.escape(name) + r"\b", pkg_only)) <= 1:
            dead.append(name)
    check("no unreferenced definitions", not dead, ", ".join(dead))

    print("\n=== duplicated logic ===")
    consts = {}
    for p in PKG:
        for node in ast.parse(p.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id.isupper():
                        consts.setdefault(t.id, []).append(p.name)
    dupes = {k: v for k, v in consts.items() if len(v) > 1}
    check("no constant defined in two modules", not dupes, str(dupes))

    tables = len(re.findall(r"SOCKS_REPLY_CODES = \{", src_all))
    check("the SOCKS reply table exists once", tables == 1, f"{tables} copies")

    print("\n=== the limit decision is single-sourced ===")
    pkg_src = blob(PKG)
    #: the invariant, not a count: the recorder sets it, and the one path that
    #: hands the lane a fresh relay clears it. A count has to be edited on every
    #: refactor and then it stops meaning anything -- this names the owners.
    owners = set()
    for p in PKG:
        lines = p.read_text(encoding="utf-8").splitlines()
        fn = "?"
        for ln in lines:
            m = re.match(r"\s*(?:async )?def (\w+)", ln)
            if m:
                fn = m.group(1)
            if "limited_until = " in ln:
                owners.add(fn)
    check("only the recorder and the re-pinner touch the deadline",
          owners == {"note_limited", "restart_lane"}, str(sorted(owners)))
    check("one recorder owns it", pkg_src.count("def note_limited") == 1)

    print("\n=== a loaded lane is never benched ===")
    # A lane that is healthy carries its request however loaded it is. Two
    # designs were rejected for breaking that: a `_LANE_CAP` that held a
    # request when N lanes looked busy, and a first-strike pull that retired a
    # lane after one timeout. Both blamed our own arithmetic on a live lane and
    # idled paid-for capacity, and both spiralled: pull one, load the rest,
    # pull them too. A name blacklist would not catch them -- the cap was a
    # brand-new constant and a brand-new emit, so nothing was unreferenced and
    # no removed word appeared.
    #
    # So this is structural, and read from the AST rather than the text --
    # a text scan cannot tell `pick_lane`'s own return from the `key()` helper's.
    # `pick_lane` may *rank* a lane by how much it is carrying; it may never
    # *drop* one for it. Narrow on purpose: `busy` and `wait` are honest words
    # about a live socket, so they are not evidence of anything.
    tree = ast.parse(pkg_src)
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent
    picks = [n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "pick_lane"]
    check("pick_lane appears once", len(picks) == 1, f"{len(picks)} found")
    fn = picks[0] if picks else None
    #: only statements that directly belong to pick_lane, not to a nested def
    own = [n for n in (fn.body if fn else []) if not isinstance(n, ast.FunctionDef)]
    own_src = "\n".join(ast.unparse(n) for n in own)

    # `l.active` is how we *rank*. The rejected designs read it to *decide*: a
    # comprehension that keeps only busy lanes, an `if` that consults a load
    # count, a threshold. The two are distinguishable by the frame that holds
    # the read. Clean: `Tuple -> Return -> key()`, i.e. the value goes back to
    # `min(..., key=...)` to be compared against other lanes. Rejected:
    # `Compare -> comprehension`, i.e. the count is tested and lanes are
    # dropped. So the rule is on the enclosing statement, not the comparison.
    def judges(node):
        up = getattr(node, "parent", None)
        while up is not None:
            if isinstance(up, (ast.Lambda, ast.IfExp)):
                return True
            if isinstance(up, ast.comprehension):
                return bool(up.ifs)      # a filter, not a bare iteration
            if isinstance(up, ast.If):
                return True
            if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return False             # the nested `key()` -- ranking
            up = getattr(up, "parent", None)
        return False

    load_verdicts = []
    for n in ast.walk(fn) if fn else []:
        if isinstance(n, ast.Attribute) and n.attr == "active" and judges(n):
            load_verdicts.append(ast.unparse(n.parent))
    check("pick_lane only ranks lanes by load, never judges them",
          not load_verdicts, " | ".join(load_verdicts[:3]))
    #: a cap is a number compared against load; `capacity` is a word
    check("pick_lane does not cap concurrency",
          not re.search(r"(max_active|_LANE_CAP|_CAP\b|>= *cap|busy *at *)",
                        own_src), "a cap is back")
    #: the verdict, not the prose about it. A docstring may explain that the
    #: cap was removed -- that is the one honest place an "at cap" can live.
    #: Everything else that is a string and carries those words is an emit.
    prose = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                prose.add(id(doc))
                for stmt in node.body:
                    if isinstance(stmt, ast.Expr) and \
                            isinstance(stmt.value, ast.Constant):
                        prose.add(id(stmt.value.value))
    spoken = [n.value.strip().splitlines()[0][:60]
              for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)
              and "at cap" in n.value and id(n.value) not in prose]
    check("no 'at cap' verdict anywhere", not spoken, " | ".join(spoken[:2]))
    check("no 'holding this request' path anywhere",
          "holding this request" not in own_src)

    print("\n=== the timeout tally is a counter, not a verdict ===")
    # `note_timeout` is the one piece of per-lane state we keep, and it is a
    # reintroduction of exactly the machinery the section above removed. So it
    # is pinned to the shape that made it defensible:
    #
    #   * it counts ONE measured event -- a TimeoutError -- and nothing else
    #   * a 200 clears it, so a lane that recovers is never held to its past
    #   * it lies dormant below the threshold; it may not emit, delay or drop
    #     anything until the counter is full
    #
    # What must never come back: a strike on any other event, a tally a success
    # does not clear, or an action taken below the threshold. Without this the
    # feature can regrow into the sidelining ladder by one small edit.
    lsrc = (ROOT / "lingling" / "lanes.py").read_text(encoding="utf-8")
    lfns = {n.name: n for n in ast.walk(ast.parse(lsrc))
            if isinstance(n, ast.FunctionDef)}
    nt = lfns.get("note_timeout")
    check("note_timeout exists", nt is not None)
    nt_src = ast.unparse(nt) if nt else ""
    nok = lfns.get("note_ok")
    check("a 200 clears the tally",
          nok is not None and "timeout_run = 0" in ast.unparse(nok))
    #: nothing may act while the tally is short of the threshold
    check("nothing happens below the threshold",
          "return None" in nt_src, "no early return")
    #: the threshold is one named constant, not a scattered magic number
    check("the threshold is single-sourced",
          lsrc.count("_TIMEOUT_RUN") >= 3, f"{lsrc.count('_TIMEOUT_RUN')}")
    #: only a TimeoutError may charge it. A strike on 403/503/SSLEOF is the
    #: revoked design: 403 is a client gate and cannot be fixed by moving. The
    #: check reads the AST, because "does the file contain these two words"
    #: passes even after the gate is deleted -- the call site and the word both
    #: survive; only the `if` around the call is gone. The charge lives in the
    #: transport, where the timeout actually happens, so the search is over
    #: every function rather than a named one.
    msrc = (ROOT / "lingling" / "mitm.py").read_text(encoding="utf-8")
    gated = False
    for fn in ast.walk(ast.parse(msrc)):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            calls = [x for x in ast.walk(node)
                     if isinstance(x, ast.Call)
                     and getattr(x.func, "id", "") == "_note_timeout"]
            if not calls:
                continue
            #: the call must sit behind a test that names TimeoutError
            if re.search(r"TimeoutError", ast.unparse(node.test)):
                gated = True
    check("only a TimeoutError charges the tally", gated,
          "ungated _note_timeout call")

    print("\n=== ctypes structs match the Windows ABI ===")
    # A field declared one width too wide makes the whole struct too big, and
    # the API call then fails with ERROR_BAD_LENGTH -- quietly, because the
    # return value was discarded. That is how the kill job was dead for the
    # life of the project: tor children orphaned on a hard kill, held their
    # ports and lane data dirs, and broke the next run.
    import ctypes
    from lingling import winjob
    basic = ctypes.sizeof(winjob._JOBOBJECT_BASIC_LIMIT_INFORMATION)
    ext = ctypes.sizeof(winjob._JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
    wide = ctypes.sizeof(ctypes.c_void_p) == 8
    check("JOBOBJECT_BASIC_LIMIT_INFORMATION is 64 bytes",
          basic == 64 or not wide, f"{basic}")
    check("JOBOBJECT_EXTENDED_LIMIT_INFORMATION is 144 bytes",
          ext == 144 or not wide, f"{ext}")

    print("\n=== stale vocabulary ===")
    hits = []
    for word in STALE:
        for p in ALL_PY:
            if p.name == "verify_audit.py":
                continue          # this list is the check itself

            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if word in line and "previous design" not in line \
                        and "no account" not in line:
                    hits.append(f"{p.name}:{i} {word}")
    check("no removed concept survives", not hits, "; ".join(hits[:5]))

    print("\n=== every file has a reason ===")
    unknown = []
    for entry in sorted(ROOT.iterdir()):
        if entry.name.startswith("."):
            allowed = set(EXPECTED) | {".git", ".workbuddy-ai"}
            if entry.name not in allowed:
                unknown.append(entry.name)
            continue
        if entry.name == "__pycache__":
            continue
        if entry.is_dir():
            if entry.name not in EXPECTED_DIRS:
                unknown.append(entry.name + "/")
        elif entry.name not in EXPECTED:
            unknown.append(entry.name)
    check("no unexplained file at the root", not unknown, ", ".join(unknown))

    stray = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.egg-info")]
    check("no build metadata in the tree", not stray, f"{len(stray)} dirs")

    print("\n=== git will not carry local state ===")
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in (".workbuddy-ai/", "__pycache__/", "*.pyc", "*.egg-info/",
                    "data/", ".env", ".venv/"):
        check(f".gitignore excludes {pattern}", pattern in ignore)
    #: bytecode regenerates whenever anything runs, so the guarantee is that
    #: git ignores it, not that it is absent from disk
    present = len(list(ROOT.rglob("*.pyc")))
    check("bytecode is ignored, not tracked", "*.pyc" in ignore,
          f"{present} on disk")

    print("\n=== runtime deps are importable ===")
    try:
        from lingling import mitm
        check("crypto stack imports", mitm.crypto_available())
    except Exception as exc:  # noqa: BLE001
        check("crypto stack imports", False, repr(exc))
    try:
        import stem  # noqa: F401
        check("stem imports", True)
    except Exception as exc:  # noqa: BLE001
        check("stem imports", False, repr(exc))

    print("\n=== the install points at this tree ===")
    import lingling
    here = pathlib.Path(lingling.__file__).resolve().parents[1]
    check("live package is this workspace", here == ROOT, str(here))

    print("\n=== the 20s ceiling is an idle ceiling, not a total budget ===")
    # The one claim the owner asked to see measured rather than asserted: a
    # call whose first token lands at 19s and then thinks for another 60s must
    # run to completion. This drives the real `_roundtrip` against a fake
    # upstream that keeps that exact schedule, so it is a measurement of the
    # shipped code -- ~101s of real wall clock for the three cases.
    proof = ROOT / "tools" / "verify" / "verify_idle_ceiling.py"
    # This one drives a 79s wall-clock schedule for real (122s for all three
    # cases), so a loaded machine can cut it short before it prints a verdict.
    # A missing verdict is not a code failure and is retried once; a real
    # [FAIL] line is never retried, because that IS a code failure.
    out = ""
    for _attempt in (1, 2):
        try:
            res = subprocess.run([sys.executable, str(proof)], cwd=str(ROOT),
                                 capture_output=True, text=True, timeout=240)
            out = res.stdout + res.stderr
        except subprocess.TimeoutExpired:
            out = ""
        if "[PASS]" in out or "[FAIL]" in out:
            break
    if "[PASS]" not in out and "[FAIL]" not in out:
        check("the idle ceiling proof produces a verdict", False,
              "no verdict line -- the run was cut short, not a code failure")
    else:
        check("19s first token + 60s think survives",
              "[PASS] survived all" in out
              and "[FAIL] survived all" not in out,
              "a long think was cut short")
        check("silence past the ceiling is still caught",
              "[PASS] abandoned at the ceiling" in out
              and "[FAIL] abandoned at the ceiling" not in out,
              "the ceiling stopped biting")
        # The ceiling splits at the moment of commit: a pre-commit stall must
        # still fail fast (a retry is free), while a post-commit pause is a
        # think-pause and must be tolerated. Both halves are checked, so
        # neither can be collapsed back into one window unnoticed.
        check("a silence past the POST-commit ceiling is caught",
              "[PASS] the post-commit silence was caught" in out
              and "[FAIL] the post-commit silence was caught" not in out,
              "a stalled body ran to the end")
        check("a gap under the post-commit ceiling is tolerated",
              "[PASS] the pause was tolerated" in out
              and "[FAIL] the pause was tolerated" not in out,
              "a think-pause truncated a committed answer")
        check("idle ceiling confirmed",
              "IDLE CEILING: CONFIRMED" in out,
              out.strip().splitlines()[-1] if out.strip() else "no output")

    print("\n=== the per-lane timeout tally can see a timeout ===")
    # Charged in the caller's retry loop, the tally never fired: over the real
    # log, 0 times across the owner's 615 callends while 95 timeouts passed it
    # by, because the transport absorbs an uncommitted timeout and a different
    # lane carries the retry. The charge now happens in the transport.
    tally = ROOT / "tools" / "verify" / "verify_timeout_tally.py"
    try:
        res = subprocess.run([sys.executable, str(tally)], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=120)
        out = res.stdout + res.stderr
        check("the transport charges its lane",
              "the transport charges the lane it timed out on" in out
              and "[FAIL] the transport charges" not in out,
              "a timed-out lane went uncharged")
        check("timeout tally confirmed",
              "TIMEOUT TALLY: CONFIRMED" in out,
              out.strip().splitlines()[-1] if out.strip() else "no output")
    except subprocess.TimeoutExpired:
        check("timeout tally proof completes", False, "timed out")

    print("\n=== a late first token is not a timeout ===")
    # A socket timeout is per blocking read, so the ceiling is idle, not a
    # total budget. A stream that keeps arriving can run far past it -- which
    # is why the owner saw a 25s first token on a lane that was fine. Only a
    # single gap longer than the ceiling trips it.
    grace = ROOT / "tools" / "verify" / "verify_first_token_grace.py"
    try:
        res = subprocess.run([sys.executable, str(grace)], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=180)
        out = res.stdout + res.stderr
        check("a slow stream outlives the ceiling",
              "[PASS] a stream under the ceiling per read outlives" in out
              and "[FAIL] a stream under the ceiling per read" not in out,
              "the ceiling behaved like a total budget")
        check("a single gap over the ceiling is caught",
              "[PASS] a single gap over the ceiling is caught" in out
              and "[FAIL] a single gap over the ceiling" not in out,
              "a stalled read ran to the end")
        check("first token grace confirmed",
              "FIRST TOKEN GRACE: CONFIRMED" in out,
              out.strip().splitlines()[-1] if out.strip() else "no output")
    except subprocess.TimeoutExpired:
        check("first token grace proof completes", False, "timed out")

    print("\n=== a 429's retry-after is a window reset, not an exit cooldown ===")
    # 72 of the log's 87 429s carry a retry-after, and they resolve to resets at
    # one instant per day (05:30 local, +/-4s) on two consecutive days -- so the
    # number says nothing about which exit was refused. Writing it onto the exit
    # retired healthy relays for 8-14h. The suite also reports the wall span, so
    # "one instant" cannot silently mean "one instant on each of N days".
    lim = ROOT / "tools" / "verify" / "verify_limited_window.py"
    try:
        res = subprocess.run([sys.executable, str(lim)], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=120)
        out = res.stdout + res.stderr
        check("a window reset does not retire the exit",
              "[PASS] a window reset does not become the exit's deadline" in out
              and "[FAIL] a window reset does not become" not in out,
              "a healthy exit was retired for hours")
        check("every 429 names the same reset instant",
              "[PASS] every 429 in a day resolves to one instant" in out
              and "[FAIL] every 429 in a day resolves" not in out,
              "resets looked per-exit")
        check("and the reset repeats at the same time of day",
              "[PASS] and the instant repeats at the same time of day" in out
              and "[FAIL] and the instant repeats" not in out,
              "the daily repeat is not shown")
        check("limited window confirmed",
              "LIMITED WINDOW: CONFIRMED" in out,
              out.strip().splitlines()[-1] if out.strip() else "no output")
    except subprocess.TimeoutExpired:
        check("limited window proof completes", False, "timed out")

    print("\n=== the probe's refusal reaches the handler ===")
    # The two refusal RULES live in verify_limit_gates, which the audit now runs
    # as well: section A pins one strike with no confirm gate, section B pins
    # that a 403 moves nothing. This covers the WIRING from check_once to
    # on_refused, which has broken before -- a gate added to on_refused silently
    # applied to the probe too, so a burnt exit was not moved on the first sweep.
    guard = ROOT / "tools" / "verify" / "verify_refusal_guard.py"
    try:
        res = subprocess.run([sys.executable, str(guard)], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=120)
        out = res.stdout + res.stderr
        check("a probe 429 reaches the refusal handler",
              "[PASS] check_once hands a probe 429 to on_refused" in out
              and "[FAIL] check_once hands a probe 429" not in out,
              "the probe's 429 never reached on_refused")
        check("a probe 403 leaves the lane alone",
              "[PASS] a 403 probe marks the lane asked" in out
              and "[FAIL] a 403 probe marks the lane asked" not in out,
              "a 403 probe moved the lane")
        check("probe refusal wiring confirmed",
              "PROBE REFUSAL WIRING: CONFIRMED" in out,
              out.strip().splitlines()[-1] if out.strip() else "no output")
    except subprocess.TimeoutExpired:
        check("probe refusal wiring proof completes", False, "timed out")

    print("\n=== the cold-connect handshake spends one window, not two ===")
    # socks5_open makes two blocking reads. A socket timeout is per read, so
    # arming it once let a dead exit spend the window twice -- which is where
    # the log's 30-60s cold-connect timeouts came from. The second read now
    # gets only the remainder.
    cold = ROOT / "tools" / "verify" / "verify_cold_connect.py"
    try:
        res = subprocess.run([sys.executable, str(cold)], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=120)
        out = res.stdout + res.stderr
        check("the handshake stays inside one window",
              "[PASS] a late greeting does NOT push the total past the window"
              in out
              and "[FAIL] a late greeting does NOT push" not in out,
              "a late greeting pushed the handshake past the window")
        check("the bound leaves a working handshake alone",
              "[PASS] a working handshake is untouched" in out
              and "[FAIL] a working handshake is untouched" not in out,
              "the bound broke the working path")
        check("cold connect confirmed",
              "COLD CONNECT: CONFIRMED" in out,
              out.strip().splitlines()[-1] if out.strip() else "no output")
    except subprocess.TimeoutExpired:
        check("cold connect proof completes", False, "timed out")

    print("\n=== the health probe asks with a real model name ===")
    # The model name is validated BEFORE the limit check and before the client
    # gate, so a placeholder makes every lane answer 401. `check_once` reads any
    # truthy code as healthy, so the 429 branch could never fire and burnt exits
    # were never caught -- their first real request paid the 429. The live half
    # of this proof needs Tor and lives in verify_probe_status.py; this is the
    # offline half that the audit can always run.
    import json as _json
    try:
        from lingling.health import PROBE_MODEL, PROBE_PATH, _scan_body
        model = _json.loads(_scan_body(PROBE_MODEL, PROBE_PATH).decode())["model"]
    except Exception:  # noqa: BLE001
        model = ""
    check("the probe body names a real model, not a placeholder",
          len(model) > 8 and "-" in model,
          f"PROBE_MODEL={model!r} is a placeholder, so every probe "
          f"would return 401")
    # The model and the path are a PAIR: crossing them answers 500, which is
    # not a verdict, so the lane would never be marked up. Measured in
    # tools/probe/model_matrix.py.
    check("the probe's model and path are a known-good pair",
          (PROBE_MODEL, PROBE_PATH) in (
              ("muse-spark-1.3-contributor-free", "/zen/v1/responses"),
              ("mimo-v2.6-flash-free", "/zen/v1/chat/completions")),
          f"({PROBE_MODEL!r}, {PROBE_PATH!r}) is not a pair that was measured "
          f"to answer a verdict -- it will get a 500")

    print("\n=== reachable() hands back the far end's status ===")
    # Every other suite stubs `reachable` out, so its own return value was never
    # tested: it could collapse to a truthy constant and everything else would
    # still pass. The probe's 429 is INFERRED (a hand-rolled request spends no
    # quota, so a probe cannot make an exit 429 itself) -- what is testable is
    # that a 429 arriving from the far end survives to on_refused, and that a
    # 403 does not. Both regressions were shown to fail the suite.
    try:
        src = (ROOT / "lingling" / "health.py").read_text(encoding="utf-8")
        check("the 429 branch compares the status, not just truthiness",
              "if code == 429:" in src,
              "a 403 would be treated as a refusal and move a healthy lane")
        # The other half of the same bug, and the one that was left behind: the
        # verdict branch used to be `if code:`, so ANY truthy status marked the
        # lane up and set `asked` -- and it was never probed again. A rejected
        # model name answers 401 and a model/path mismatch answers 500, so
        # either one silently disables the 429 branch for the whole session.
        # Fixing the model NAME once did not fix this; the guard is here now.
        check("only a verdict marks a lane asked, not any truthy code",
              "if code in PROBE_VERDICTS:" in src,
              "a 401 or 500 from the probe reads as a healthy lane, so that "
              "exit's quota is never checked again")
    except OSError as exc:  # noqa: BLE001
        check("health.py is readable", False, repr(exc))

    print("\n=== every callend carries the fields a reader groups by ===")
    # A key written on only SOME emit paths makes a query silently wrong
    # rather than obviously broken. Counting `reused` across errors reported
    # "0 of 149 SSLEOFs on reused tunnels", because the error path never wrote
    # the key at all -- the answer looked like evidence and was an absence.
    # This is the third time this session a partial schema produced a
    # meaningless number, so it is now checked structurally.
    try:
        tree = ast.parse((ROOT / "lingling" / "mitm.py").read_text("utf-8"))
        # A key supplied through `**helper()` is invisible to a walk that reads
        # only `ast.Dict.keys`. That is how `send_s` and `peer_close` first
        # "went missing": the six emit sites were correct and this check could
        # not see them. It is also why `first_byte_s` and `first_event_s` were
        # never named in `universal` -- they have come from the `_lat` splat
        # since the tail was factored out, so the check was blind to them and
        # passed while saying nothing about them. Resolving one level of splat
        # closes both, and the two read fields are named below now that it can
        # actually see them.
        raw, splats = {}, {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for sub in ast.walk(node):
                if not (isinstance(sub, ast.Return)
                        and isinstance(sub.value, ast.Dict)):
                    continue
                raw.setdefault(node.name, set())
                splats.setdefault(node.name, set())
                for k, v in zip(sub.value.keys, sub.value.values):
                    if isinstance(k, ast.Constant):
                        raw[node.name].add(k.value)
                    elif isinstance(v, ast.Call):
                        fn = getattr(v.func, "id", None)
                        if fn:
                            splats[node.name].add(fn)
        #: helper -> every key it contributes, splats followed one level
        helpers = {}
        for name in raw:
            keys = set(raw[name])
            for other in splats.get(name, ()):
                keys |= raw.get(other, set())
            helpers[name] = keys
        emits = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name)
                    and node.func.id == "emit"):
                continue
            if not node.args or not isinstance(node.args[0], ast.Dict):
                continue
            d = node.args[0]
            # "callend" is the VALUE of the "type" key, not a key itself --
            # matching on keys found zero emits and the check passed
            # vacuously, which is the exact failure it exists to prevent.
            pairs = {k.value: v for k, v in zip(d.keys, d.values)
                     if isinstance(k, ast.Constant)}
            kind = pairs.get("type")
            if not (isinstance(kind, ast.Constant)
                    and kind.value == "callend"):
                continue
            keys = set(pairs)
            for k, v in zip(d.keys, d.values):
                if k is None and isinstance(v, ast.Call):
                    keys |= helpers.get(getattr(v.func, "id", ""), set())
            emits.append((node.lineno, keys))
        #: keys every callend must carry
        universal = ("type", "t", "n", "c", "lane", "cc", "status", "kb",
                     "secs", "err", "reused", "cut", "max_wait_s",
                     "client_wait_s", "send_s", "peer_close",
                     "first_byte_s", "first_event_s", "client_kb")
        #: keys that mean something on only one outcome, by design.
        #: `retry_after` needs a 429. `stalled` is emitted on the transport
        #: error path; the cold-connect failures return before the transport
        #: and carry `err='timed out'` instead, which is unambiguous. `note`
        #: carries the far end's error body on 4xx/5xx and is empty on 200s.
        #: Declared here so an accidental NEW partial key fails the audit
        #: rather than passing as a shrug -- a partial key has produced a
        #: confident wrong answer five times on this log.
        specific = {"retry_after", "stalled", "note"}
        gaps = [(ln, sorted(set(universal) - keys)) for ln, keys in emits
                if set(universal) - keys]
        check("every callend carries the same keys",
              bool(emits) and not gaps,
              f"found {len(emits)} callend emits; missing {gaps}")
        undeclared = sorted({k for _ln, keys in emits
                             for k in keys - set(universal) - specific})
        check("no callend carries an undeclared key", not undeclared,
              f"undeclared: {undeclared}")
    except (OSError, SyntaxError) as exc:  # noqa: BLE001
        check("mitm.py is parseable", False, repr(exc))

    print("\n=== the remaining suites, so none can break silently ===")
    # The audit used to invoke only SOME of the suites. A change to
    # `on_refused` then broke `verify_limit_gates` -- five checks -- and nothing
    # in the audit noticed, because that suite was never run here. Run them all.
    for suite in ("verify_limit_gates", "verify_country_expand",
                  "verify_call_outcome", "verify_probe_branch",
                  "verify_request_params", "verify_committed_stream",
                  "verify_client_stall", "verify_reused_send",
                  "verify_socks5_reply", "verify_lane_revive",
                  "verify_expect_header", "verify_short_body",
                  "verify_close_honoured", "verify_pool_ttl",
                  "verify_send_window", "verify_torrc",
                  # the 503/504 session: a retryable verdict must reach the
                  # client whole, a 5xx must stop after two attempts, and the
                  # far end's error body must land on the callend
                  "verify_5xx_delivery",
                  # floors the three windows that no suite was guarding: every
                  # suite touching them pins its own value, so they could be
                  # set to nonsense with the whole set still green
                  "verify_window_floors",
                  # the owner's FIRST rule -- never invent a failure state for
                  # a healthy lane -- was guarded by review alone, and a
                  # re-added `_LANE_CAP` still passed AUDIT CLEAN
                  "verify_no_lane_cap"):
        path = ROOT / "tools" / "verify" / f"{suite}.py"
        try:
            res = subprocess.run([sys.executable, str(path)], cwd=str(ROOT),
                                 capture_output=True, text=True, timeout=240)
            out = res.stdout + res.stderr
            failed = [ln.strip() for ln in out.splitlines()
                      if ln.strip().startswith("- ")]
            check(f"{suite} passes",
                  res.returncode == 0 and not failed,
                  (failed[0] if failed else f"exit {res.returncode}"))
        except subprocess.TimeoutExpired:
            check(f"{suite} completes", False, "timed out")

    print("\n=== the post-commit window is generous enough ===")
    # The window only ever measures SILENCE. A stream that is emitting is
    # unaffected at ANY value, so a small window has no upside and exactly one
    # downside: cutting an answer whose pause happened to be long. The longest
    # gap measured on a healthy stream is 16.6s, which is why even the old 20s
    # ceiling was cutting real answers. A future edit that shrinks this back is
    # a regression, so the floor is checked rather than trusted.
    try:
        src = (ROOT / "lingling" / "mitm.py").read_text("utf-8")
        line = next((ln for ln in src.splitlines()
                     if "LINGLING_STREAM_IDLE_S" in ln), "")
        parts = line.split('"')
        shipped = float(parts[3]) if len(parts) >= 4 else 0.0
        check("the post-commit window is at least 600s",
              shipped >= 600,
              f"shipped default is {shipped}s -- a shorter window cannot help "
              f"a stream that is emitting, it can only cut one that paused")
    except (OSError, ValueError) as exc:  # noqa: BLE001
        check("the post-commit window is at least 600s", False, repr(exc))

    print("\n=== the stall analyzer's join is session-aware ===")
    # tools/soak/stall_by_effort.py correlates stalls with reasoning effort by
    # joining each callend to its call on (session, n, c). Two versions of that
    # join silently produced wrong buckets -- one matched nothing, the next
    # reused the last session's key -- and a single-session test cannot see
    # either, because (n, c) only collides ACROSS sessions. Its selftest uses
    # two sessions sharing the same (n, c) values.
    tool = ROOT / "tools" / "soak" / "stall_by_effort.py"
    try:
        res = subprocess.run([sys.executable, str(tool), "--selftest"],
                             cwd=str(ROOT), capture_output=True, text=True,
                             timeout=60)
        line = (res.stdout + res.stderr).strip().splitlines()
        check("the effort join is session-aware", res.returncode == 0,
              line[-1] if line else "no output")
    except subprocess.TimeoutExpired:
        check("the effort join is session-aware", False, "timed out")

    print("\n" + "=" * 52)
    if FAIL:
        print(f"FAILURES: {len(FAIL)}")
        for f in FAIL:
            print("  - " + f)
        sys.exit(1)
    print("AUDIT CLEAN")


if __name__ == "__main__":
    main()
