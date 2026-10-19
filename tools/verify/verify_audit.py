"""Repo hygiene: does the tree still meet every standard we set?"""
import ast
import io
import os
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

#: every file
EXPECTED = {
    ".gitignore": "excludes local state from git",
    "LICENSE": "licence",
    "README.md": "user docs",
    "pyproject.toml": "packaging",
    "requirements.txt": "pinned deps",
}
EXPECTED_DIRS = {"lingling": "the package", "tools": "dev tooling"}

#: vocabulary
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


def _git_ignored(entry: pathlib.Path) -> bool:
    """True when git itself already excludes this root entry."""
    name = entry.name + ("/" if entry.is_dir() else "")
    return subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "-q", "--", name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


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
    #: Walked, not
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
    #: invariant, not
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
    #: one recorder sets the deadline, and one clearer drops it -- a lane is
    #: limited only while it still rides the exit that was written off, so the
    #: only other writer is the call that hands it a different relay.
    check("only the recorder and the exit change touch the deadline",
          owners == {"note_limited", "forget_limit"}, str(sorted(owners)))
    check("one recorder owns it", pkg_src.count("def note_limited") == 1)

    print("\n=== a loaded lane is never benched ===")
    # lane
    tree = ast.parse(pkg_src)
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent
    picks = [n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "pick_lane"]
    check("pick_lane appears once", len(picks) == 1, f"{len(picks)} found")
    fn = picks[0] if picks else None
    #: only statements
    own = [n for n in (fn.body if fn else []) if not isinstance(n, ast.FunctionDef)]
    own_src = "\n".join(ast.unparse(n) for n in own)

    # `l.active`
    def judges(node):
        up = getattr(node, "parent", None)
        while up is not None:
            if isinstance(up, (ast.Lambda, ast.IfExp)):
                return True
            if isinstance(up, ast.comprehension):
                return bool(up.ifs)  # filter, not
            if isinstance(up, ast.If):
                return True
            if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return False  # nested `key()`
            up = getattr(up, "parent", None)
        return False

    load_verdicts = []
    for n in ast.walk(fn) if fn else []:
        if isinstance(n, ast.Attribute) and n.attr == "active" and judges(n):
            load_verdicts.append(ast.unparse(n.parent))
    check("pick_lane only ranks lanes by load, never judges them",
          not load_verdicts, " | ".join(load_verdicts[:3]))
    #: cap
    check("pick_lane does not cap concurrency",
          not re.search(r"(max_active|_LANE_CAP|_CAP\b|>= *cap|busy *at *)",
                        own_src), "a cap is back")
    #: verdict, not
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

    print("\n=== a timeout is a verdict now, the 429 action on the first hit ===")
    # timeout
    lsrc = (ROOT / "lingling" / "lanes.py").read_text(encoding="utf-8")
    lfns = {n.name: n for n in ast.walk(ast.parse(lsrc))
            if isinstance(n, ast.FunctionDef)}
    nt_src = ast.unparse(lfns["note_timeout"]) if "note_timeout" in lfns else ""
    check("note_timeout exists", "note_timeout" in lfns)
    #: same action
    check("a timeout writes the exit off", "note_limited" in nt_src)
    check("a timeout moves the country", "rotate_exit_country" in nt_src)
    check("a timeout re-cooks the lane", "_rebuild_async" in nt_src)
    #: old strike
    check("no strike counter is left anywhere",
          "timeout_run" not in lsrc and "repins" not in lsrc
          and "_TIMEOUT_RUN" not in lsrc,
          f"timeout_run={lsrc.count('timeout_run')} "
          f"repins={lsrc.count('repins')} "
          f"_TIMEOUT_RUN={lsrc.count('_TIMEOUT_RUN')}")
    #: only
    msrc = (ROOT / "lingling" / "mitm.py").read_text(encoding="utf-8")
    want = {"_note_timeout": "TimeoutError", "_note_ssl": "SSLError",
            "_note_slow": "_SLOW_EXIT_S"}
    gates = {k: False for k in want}
    for fn in ast.walk(ast.parse(msrc)):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            for callee, needle in want.items():
                calls = [x for x in ast.walk(node)
                         if isinstance(x, ast.Call)
                         and getattr(x.func, "id", "") == callee]
                if calls and re.search(needle, ast.unparse(node.test)):
                    gates[callee] = True
    check("only a TimeoutError charges the tally", gates["_note_timeout"],
          "ungated _note_timeout call")
    check("only an SSLError charges the SSL verdict", gates["_note_ssl"],
          "ungated _note_ssl call")
    check("only the ceiling charges the slow verdict", gates["_note_slow"],
          "ungated _note_slow call")

    print("\n=== ctypes structs match the Windows ABI ===")
    # field declared
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
                continue  # list

            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if word in line and "previous design" not in line \
                        and "no account" not in line:
                    hits.append(f"{p.name}:{i} {word}")
    check("no removed concept survives", not hits, "; ".join(hits[:5]))

    print("\n=== every file has a reason ===")
    unknown = []
    for entry in sorted(ROOT.iterdir()):
        if entry.name.startswith("."):
            allowed = set(EXPECTED) | {".git"}
            if entry.name not in allowed and not _git_ignored(entry):
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
    for pattern in ("__pycache__/", "*.pyc", "*.egg-info/", "data/", ".env",
                    ".env.*", ".venv/"):
        check(f".gitignore excludes {pattern}", pattern in ignore)
    #: bytecode regenerates
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
    # console script
    import json
    import importlib.metadata as meta
    try:
        dist = meta.distribution("lingling")
        url = json.loads(dist.read_text("direct_url.json") or "{}")
        live = pathlib.Path(
            dist.locate_file("lingling/__init__.py")).resolve().parent
    except meta.PackageNotFoundError:
        url, live = {}, None
    if live is None:
        check("the package is installed, not merely present", False,
              "no lingling distribution -- the console script cannot run")
    elif url.get("dir_info", {}).get("editable"):
        # editable install
        check("the install is editable, so this tree is what runs", True,
              "no installed copy to drift")
    else:
        def flat(p):
            return p.read_bytes().replace(b"\r\n", b"\n")

        drifted = [s.name for s in PKG
                   if not (live / s.name).exists()
                   or flat(s) != flat(live / s.name)]
        check("the installed package is this tree, file for file",
              not drifted,
              f"{len(PKG) - len(drifted)}/{len(PKG)} modules match"
              + (f"; stale: {', '.join(drifted)} -- reinstall, or the "
                 f"console script keeps running the old code"
                 if drifted else ""))

    print("\n=== the 20s ceiling is an idle ceiling, not a total budget ===")
    # one claim
    proof = ROOT / "tools" / "verify" / "verify_idle_ceiling.py"
    # one drives
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
        # ceiling splits
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
    # Charged
    tally = ROOT / "tools" / "verify" / "verify_timeout_tally.py"
    try:
        res = subprocess.run([sys.executable, str(tally)], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=120)
        out = res.stdout + res.stderr
        check("the transport charges its lane",
              "the transport charges the lane it timed out on" in out
              and "[FAIL] the transport charges" not in out,
              "a timed-out lane went uncharged")
        check("an SSL cut charges its lane too",
              "an SSL cut mid-read is charged to the lane" in out
              and "[FAIL] an SSL cut" not in out,
              "an SSLEOFError lane went uncharged")
        check("a slow first event charges its lane",
              "a first event over 20s is charged to the lane" in out
              and "[FAIL] a first event" not in out,
              "a slow lane went uncharged")
        check("timeout tally confirmed",
              "TIMEOUT TALLY: CONFIRMED" in out,
              out.strip().splitlines()[-1] if out.strip() else "no output")
    except subprocess.TimeoutExpired:
        check("timeout tally proof completes", False, "timed out")

    print("\n=== a late first token is not a timeout ===")
    # socket timeout
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

    print("\n=== the notepad benches for the wall that was hit ===")
    # 72
    lim = ROOT / "tools" / "verify" / "verify_limited_window.py"
    try:
        res = subprocess.run([sys.executable, str(lim)], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=120)
        out = res.stdout + res.stderr
        check("the wall's own reset becomes the bench",
              "[PASS] the wall's own reset becomes the deadline" in out
              and "[FAIL] the wall's own reset becomes" not in out,
              "the wall's reset is not honoured")
        check("a headerless wall benches to the next 00:00 UTC",
              "[PASS] a headerless wall benches to the next 00:00 UTC" in out
              and "[FAIL] a headerless wall benches" not in out,
              "no default reset for a headerless wall")
        check("a nonsense reset cannot retire an exit forever",
              "[PASS] the claim is clipped to the ceiling" in out
              and "[FAIL] the claim is clipped" not in out, "no ceiling")
        check("the burst limit has its own bounded stand-off",
              "[PASS] no header means the stand-off" in out
              and "[PASS] a long header cannot outlast the stand-off" in out
              and "[FAIL] a long header cannot outlast" not in out,
              "the burst window is unbounded")
        check("the notepad keeps the two sections apart",
              "[PASS] a free exit and a rate exit are told apart" in out
              and "[FAIL] a free exit and a rate exit" not in out,
              "the two limits were mixed up")
        check("their edge is shown, and owns no exit",
              "[PASS] their edge is loud, and owns no exit" in out
              and "[FAIL] their edge is loud" not in out,
              "opencode's own edge was charged to an exit")
        check("every 429 names the same reset instant",
              ("[PASS] every 429 in a day resolves to one instant" in out
               or "[SKIP] every 429 in a day resolves to one instant" in out)
              and "[FAIL] every 429 in a day resolves" not in out,
              "resets looked per-exit")
        check("and the reset repeats at the same time of day",
              ("[PASS] and the instant repeats at the same time of day" in out
               or "[SKIP] and the instant repeats at the same time of day"
               in out)
              and "[FAIL] and the instant repeats" not in out,
              "the daily repeat is not shown")
        check("limited window confirmed",
              "LIMITED WINDOW: CONFIRMED" in out,
              out.strip().splitlines()[-1] if out.strip() else "no output")
    except subprocess.TimeoutExpired:
        check("limited window proof completes", False, "timed out")

    print("\n=== the probe's refusal reaches the handler ===")
    # two refusal
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

    print("\n=== one session, enforced ===")
    lock = ROOT / "tools" / "verify" / "verify_session_lock.py"
    try:
        res = subprocess.run([sys.executable, str(lock)], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=120)
        out = res.stdout + res.stderr
        check("the one-session lock holds", "SESSION LOCK: CONFIRMED" in out
              and "SESSION LOCK: FAILED" not in out,
              "a second launcher was not refused")
    except subprocess.TimeoutExpired:
        check("the one-session lock holds", False, "the suite timed out")

    print("\n=== the cold-connect handshake spends one window, not two ===")
    # socks5_open makes
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
    # model name
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
    # model
    #: measured 2026-10-17
    check("the probe's model and path are a known-good pair",
          (PROBE_MODEL, PROBE_PATH) == (
              "mimo-v2.6-flash-free", "/zen/v1/chat/completions"),
          f"({PROBE_MODEL!r}, {PROBE_PATH!r}) is not the pair measured to "
          f"answer a verdict -- the responses pair it replaced answers 410 "
          f"ModelDeprecated, so every probe is a non-verdict and no lane is "
          f"ever checked")

    print("\n=== reachable() hands back the far end's status ===")
    # Every other
    try:
        src = (ROOT / "lingling" / "health.py").read_text(encoding="utf-8")
        check("the 429 branch compares the status, not just truthiness",
              "if code == 429:" in src,
              "a 403 would be treated as a refusal and move a healthy lane")
        # other half
        check("only a verdict marks a lane asked, not any truthy code",
              "if code in PROBE_VERDICTS:" in src,
              "a 401 or 500 from the probe reads as a healthy lane, so that "
              "exit's quota is never checked again")
    except OSError as exc:  # noqa: BLE001
        check("health.py is readable", False, repr(exc))

    print("\n=== every callend carries the fields a reader groups by ===")
    # key written
    try:
        tree = ast.parse((ROOT / "lingling" / "mitm.py").read_text("utf-8"))
        # key supplied
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
        #: helper ->
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
            # "callend"
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
        #: keys every
        universal = ("type", "t", "n", "c", "lane", "cc", "status", "kb",
                     "secs", "err", "reused", "cut", "max_wait_s",
                     "client_wait_s", "send_s", "peer_close",
                     "first_byte_s", "first_event_s", "client_kb",
                     "exit", "ip", "raced")
        #: keys
        specific = {"retry_after", "stalled", "note", "limit"}
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
    # audit used
    for suite in ("verify_bounds", "verify_limit_gates", "verify_country_expand",
                  "verify_call_outcome", "verify_probe_branch",
                  "verify_request_params", "verify_committed_stream",
                  "verify_client_stall", "verify_reused_send",
                  "verify_socks5_reply", "verify_lane_revive",
                  "verify_expect_header", "verify_short_body",
                  "verify_close_honoured", "verify_pool_ttl",
                  "verify_send_window", "verify_torrc",
                  # a cert may not leave its directory
                  "verify_cert_paths",
                  # cold data
                  "verify_cold_boot",
                  # 503/504 session
                  "verify_5xx_delivery",
                  # a wall that catches the pool
                  "verify_limit_retry",
                  # floors
                  "verify_window_floors",
                  # owner's FIRST
                  "verify_no_lane_cap",
                  # owner's rule
                  "verify_first_content_deadline",
                  # committed one
                  "verify_stall_cut",
                  # lane election
                  "verify_sticky",
                  # every call
                  "verify_lane_race",
                  # probe must
                  "verify_probe_circuit",
                  # 2.0.26 opencode
                  "verify_server_proxy",
                  # the next race
                  "verify_exit_sweep",
                  # the pick
                  "verify_exit_spread",
                  # pip cannot
                  "verify_uninstall"):
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
    # window only
    env = {k: v for k, v in os.environ.items()
           if k != "LINGLING_STREAM_IDLE_S"}
    shipped = 0.0
    try:
        res = subprocess.run(
            [sys.executable, "-c",
             "import lingling.mitm as m; print(m._STREAM_IDLE_TIMEOUT)"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60)
        shipped = float(res.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        shipped = 0.0
    check("the post-commit window is at least 600s",
          shipped >= 600,
          f"shipped default is {shipped}s -- a shorter window cannot help "
          f"a stream that is emitting, it can only cut one that paused")

    print("\n=== the stall analyzer's join is session-aware ===")
    # tools/soak/stall_by_effort.py correlates
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
