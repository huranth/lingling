"""The log must record what a request ASKED FOR, not just its model."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling.mitm import _model_of  # noqa: E402
from lingling.health import PROBE_MODEL, PROBE_PATH, _scan_body  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    """Detail is the failure reason, so only show it when it failed."""
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    if not ok:
        FAILS.append(name)


MODEL = "muse-spark-1.3-contributor-free"


def body(**extra):
    """A request body with the free-tier model and `extra` merged in."""
    return json.dumps({"model": MODEL, **extra}).encode()


def main():
    print("=== effort and cap reach the log ===")
    got = _model_of(body(reasoning={"effort": "high"}))
    print(f"  reasoning.effort=high -> {got!r}")
    check("nested reasoning.effort is recorded",
          got == f"{MODEL} effort=high cap=-", got)

    got = _model_of(body(reasoning_effort="low"))
    print(f"  reasoning_effort=low  -> {got!r}")
    check("flat reasoning_effort is recorded",
          got == f"{MODEL} effort=low cap=-", got)

    got = _model_of(body(max_output_tokens=32000))
    print(f"  max_output_tokens     -> {got!r}")
    check("max_output_tokens is recorded",
          got == f"{MODEL} effort=- cap=32000", got)

    got = _model_of(body(reasoning={"effort": "high"}, max_output_tokens=9000))
    print(f"  both                  -> {got!r}")
    check("both together are recorded",
          got == f"{MODEL} effort=high cap=9000", got)

    print("\n=== the plain path is unchanged ===")
    got = _model_of(body())
    print(f"  model only            -> {got!r}")
    check("a body with no parameters logs the bare model",
          got == MODEL, got)

    got = _model_of(body(reasoning={"effort": ""}))
    print(f"  empty effort          -> {got!r}")
    check("an empty effort does not fabricate a parameter",
          got == MODEL, got)

    print("\n=== a body that does not parse is not a new failure mode ===")
    for bad, label in ((b"not json", "garbage"), (b"", "empty"),
                       (b'{"model":123}', "non-string model")):
        got = _model_of(bad)
        print(f"  {label:16} -> {got!r}")
    check("garbage yields an empty name, as before",
          _model_of(b"not json") == "", "raised or returned junk")
    check("an empty body yields an empty name",
          _model_of(b"") == "", "raised or returned junk")
    check("a non-string model does not crash",
          _model_of(b'{"model":123}') == "123", "raised")

    print("\n=== the probe's own body is untouched by this ===")
    probe = json.loads(_scan_body(PROBE_MODEL, PROBE_PATH).decode()).get(
        "model", "")
    print(f"  probe model = {probe!r}")
    check("the shipped probe model is a real one",
          len(probe) > 8 and "-" in probe, probe)

    print()
    if FAILS:
        print("REQUEST PARAMS LOGGED: FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("REQUEST PARAMS LOGGED: CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
