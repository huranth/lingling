"""The one rule the audit did not guard: no lane cap, ever."""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RELAY = ROOT / "lingling" / "relay.py"

FAILS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    if not ok:
        FAILS.append(name)


#: unmistakably a concurrency limit, whatever it is set to
HARD_CAP = ("max_active", "maxactive", "quota", "lane_cap", "active_cap",
            "cap_limit")


def _hard_cap_name(name: str) -> bool:
    low = name.lower()
    return any(tok in low for tok in HARD_CAP)


def _soft_cap_name(name: str) -> bool:
    """`cap`-ish but ambiguous -- the capture buffer is called `_cap`."""
    low = name.lower()
    if "capture" in low:          # `_CAPTURE`, `_CAPTURE_MAX`
        return False
    return "cap" in low


def _numeric(node) -> bool:
    """A cap is a NUMBER."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in ("int", "float"):
        return True
    return False


def _is_cap_name(name: str) -> bool:
    return _hard_cap_name(name) or _soft_cap_name(name)


def findings(src: str):
    """Cap-shaped constructs in `src`, as a list of human-readable strings."""
    out = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        # module/function-level assignment to a cap-like name
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if not isinstance(t, ast.Name):
                    continue
                if _hard_cap_name(t.id):
                    out.append(f"cap-like constant `{t.id}`")
                elif _soft_cap_name(t.id) and _numeric(node.value):
                    out.append(f"cap-like constant `{t.id}` (numeric)")
        # a cap parameter on the picker
        if isinstance(node, ast.FunctionDef) and node.name == "pick_lane":
            names = [a.arg for a in node.args.args]
            if any(_is_cap_name(n) for n in names):
                out.append(f"pick_lane takes a cap parameter {names}")
            for n in ast.walk(node):
                # `if l.active >= X` -- what a cap
                if isinstance(n, ast.Compare):
                    left = n.left
                    if isinstance(left, ast.Attribute) and left.attr == "active":
                        out.append("a comparison against `active`")
    return sorted(set(out))


def picker_signature(src: str):
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "pick_lane":
            return [a.arg for a in node.args.args]
    return None


POSITIVE = (
    ("_LANE_CAP = 4\n", "a cap constant"),
    ("def pick_lane(self, exclude=None, max_active=4):\n    return None\n",
     "a cap parameter"),
    ("def pick_lane(self, exclude=None):\n"
     "    for l in lanes:\n"
     "        if l.active >= 3:\n"
     "            continue\n", "a comparison against active"),
)

NEGATIVE = (
    ("_cap = bytearray()\n_CAPTURE_MAX = 8192\n", "the capture buffer"),
    ('def pick_lane(self, exclude=None):\n'
     '    """Holding a request while a healthy lane sat idle was worse."""\n'
     '    return min(lanes, key=lambda l: l.active)\n', "prose about holding"),
)


def main():
    src = RELAY.read_text("utf-8")

    print("=== the controls: this detector must bite, and must not over-bite ===")
    for code, label in POSITIVE:
        check(f"detects {label}", bool(findings(code)),
              "a cap-shaped construct went undetected -- the guard is decorative")
    for code, label in NEGATIVE:
        f = findings(code)
        check(f"ignores {label}", not f, f"false positive: {f}")

    print("\n=== the shipped picker ===")
    sig = picker_signature(src)
    print(f"  pick_lane signature: {sig}")
    check("the picker exists", sig is not None)
    check("the picker takes no cap parameter",
          sig is not None and not any(_is_cap_name(n) for n in sig),
          f"signature {sig} -- a cap needs a limit to configure")
    got = findings(src)
    check("relay.py holds no cap construct", not got, f"found: {got}")

    print()
    if FAILS:
        print("NO LANE CAP: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("NO LANE CAP: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
