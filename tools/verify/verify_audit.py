"""Repo hygiene: does the tree still meet every standard we set?"""
import ast
import io
import pathlib
import re
import subprocess
import sys
import tokenize

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

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
    """Print a verdict, and label the detail as a note or a reason."""
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
    # A lane that is healthy carries its request
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

    # `l.active` is how we *rank*. The rejected designs
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
    # `note_timeout` is the one piece of per-lane state
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
    # A field declared one width too wide makes
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
            allowed = set(EXPECTED) | {".git", ".workbuddy-ai", ".freebuff"}
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
    # The one claim the owner asked to see
    proof = ROOT / "tools" / "verify" / "verify_idle_ceiling.py"
    # This one drives a 79s wall-clock schedule for
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
        # The ceiling splits at the moment of commit:
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
    # Charged in the caller's retry loop, the tally
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
    # A socket timeout is per blocking read, so
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
    # 72 of the log's 87 429s carry a
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
    # The two refusal RULES live in verify_limit_gates, which
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
    # socks5_open makes two blocking reads. A socket timeout
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
    # The model name is validated BEFORE the limit
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
    # The model and the path are a PAIR:
    check("the probe's model and path are a known-good pair",
          (PROBE_MODEL, PROBE_PATH) in (
              ("muse-spark-1.3-contributor-free", "/zen/v1/responses"),
              ("mimo-v2.6-flash-free", "/zen/v1/chat/completions")),
          f"({PROBE_MODEL!r}, {PROBE_PATH!r}) is not a pair that was measured "
          f"to answer a verdict -- it will get a 500")

    print("\n=== reachable() hands back the far end's status ===")
    # Every other suite stubs `reachable` out, so its
    try:
        src = (ROOT / "lingling" / "health.py").read_text(encoding="utf-8")
        check("the 429 branch compares the status, not just truthiness",
              "if code == 429:" in src,
              "a 403 would be treated as a refusal and move a healthy lane")
        # The other half of the same bug, and
        check("only a verdict marks a lane asked, not any truthy code",
              "if code in PROBE_VERDICTS:" in src,
              "a 401 or 500 from the probe reads as a healthy lane, so that "
              "exit's quota is never checked again")
    except OSError as exc:  # noqa: BLE001
        check("health.py is readable", False, repr(exc))

    print("\n=== every callend carries the fields a reader groups by ===")
    # A key written on only SOME emit paths
    try:
        tree = ast.parse((ROOT / "lingling" / "mitm.py").read_text("utf-8"))
        # A key supplied through `**helper()` is invisible to
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
            # "callend" is the VALUE of the "type" key,
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
    # The audit used to invoke only SOME of
    for suite in ("verify_limit_gates", "verify_country_expand",
                  "verify_call_outcome", "verify_probe_branch",
                  "verify_request_params", "verify_committed_stream",
                  "verify_client_stall", "verify_reused_send",
                  "verify_socks5_reply", "verify_lane_revive",
                  "verify_expect_header", "verify_short_body",
                  "verify_close_honoured", "verify_pool_ttl",
                  "verify_send_window", "verify_torrc",
                  # a cold data dir must not turn the
                  "verify_cold_boot",
                  # the 503/504 session: a retryable verdict must reach
                  "verify_5xx_delivery",
                  # floors the three windows that no suite was
                  "verify_window_floors",
                  # the owner's FIRST rule -- never invent a
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
    # The window only ever measures SILENCE. A stream
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
