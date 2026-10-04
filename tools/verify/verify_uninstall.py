"""`lingling uninstall` wipes the data dir -- and never the countries file.

pip removes only the package: 18 files in site-packages. The lanes, the Tor
bundle, the geoip database and the proof logs live in the data dir, which
pip does not know exists. A plain `pip uninstall` therefore leaves hundreds
of megabytes and a stale relay cache behind, and the next install reads
that cache as a warm boot.

The one thing in there a user cannot get back is countries.txt: it is
hand-written, not downloaded. Every path below is checked against that.

Runs entirely in a scratch data dir via LINGLING_DATA_DIR, so the real one
is never opened, let alone written.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

FAILS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    if not ok:
        FAILS.append(name)


def _seed(root: pathlib.Path, countries="us,nl,de\nhr,is\n") -> None:
    """A data dir shaped like a real warm one."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "countries.txt").write_text(countries, encoding="utf-8")
    (root / "lanes" / "tor-1").mkdir(parents=True, exist_ok=True)
    (root / "lanes" / "tor-1" / "cached-microdescs").write_bytes(b"x" * 4096)
    (root / "tools" / "tor" / "data").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "tor" / "data" / "geoip").write_bytes(b"g" * 2048)
    (root / "mitm").mkdir(exist_ok=True)
    (root / "proof.log").write_text("{}\n", encoding="utf-8")
    (root / "used-exits.json").write_text("{}", encoding="utf-8")


def _run(data_dir: pathlib.Path, *args):
    """Invoke the CLI in a scratch data dir; returns (code, output).

    stdin is DEVNULL so the shell is non-interactive by construction:
    a refusal must come from the --yes check, never from whatever tty
    happened to be attached to the test runner.
    """
    env = dict(os.environ)
    env["LINGLING_DATA_DIR"] = str(data_dir)
    res = subprocess.run(
        [sys.executable, "-m", "lingling", *args],
        cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=90)
    return res.returncode, res.stdout + res.stderr


def main() -> int:
    base = pathlib.Path(tempfile.mkdtemp(prefix="lingling-uninst-"))

    print("\n=== 1. the subcommand exists and does not reach opencode ===")
    sb = base / "a"
    _seed(sb)
    code, out = _run(sb, "uninstall", "--help")
    check("uninstall --help exits 0", code == 0, f"code={code}")
    check("it prints the uninstall help, not opencode's",
          "wipe everything lingling put on disk" in out
          and "npm uninstall -g opencode-ai" not in out,
          out[:200])
    check("the countries promise is stated",
          "countries.txt" in out, "help does not mention countries.txt")

    print("\n=== 2. no --yes in a non-interactive shell refuses ===")
    sb = base / "b"
    _seed(sb)
    before = sorted(p.name for p in sb.iterdir())
    code, out = _run(sb, "uninstall")
    check("it exits non-zero", code != 0, f"code={code}")
    check("it says why", "refusing to wipe without --yes" in out, out[:200])
    check("nothing was touched",
          sorted(p.name for p in sb.iterdir()) == before,
          f"{sorted(p.name for p in sb.iterdir())}")

    print("\n=== 3. --yes wipes the state and keeps countries.txt ===")
    sb = base / "c"
    _seed(sb)
    keep = (sb / "countries.txt").read_bytes()
    code, out = _run(sb, "uninstall", "--yes")
    check("it exits 0", code == 0, f"code={code}")
    check("countries.txt survives", (sb / "countries.txt").exists())
    check("byte for byte", (sb / "countries.txt").read_bytes() == keep)
    check("the lanes are gone", not (sb / "lanes").exists())
    check("the tor bundle is gone", not (sb / "tools").exists())
    check("the geoip is gone", not (sb / "geoip").exists())
    check("the proof log is gone", not (sb / "proof.log").exists())
    check("it says what it kept", "kept: countries.txt" in out, out[:300])

    print("\n=== 4. the delete list never contains a countries file ===")
    sb = base / "d"
    _seed(sb)
    (sb / "countries.txt.bak-20260101").write_text("us\n", encoding="utf-8")
    code, out = _run(sb, "uninstall", "--yes")
    listing = [ln for ln in out.splitlines() if ln.startswith("  ")]
    check("no countries entry is offered for deletion",
          not any("countries" in ln for ln in listing), str(listing))
    check("the backup survives too",
          (sb / "countries.txt.bak-20260101").exists(),
          f"{sorted(p.name for p in sb.iterdir())}")

    print("\n=== 5. second run is a no-op, not an error ===")
    sb = base / "e"
    _seed(sb)
    _run(sb, "uninstall", "--yes")
    code, out = _run(sb, "uninstall", "--yes")
    check("it exits 0", code == 0, f"code={code}")
    check("it says there is nothing left",
          "only the countries override is left" in out, out[:200])

    print("\n=== 6. a missing data dir is a no-op, not a traceback ===")
    code, out = _run(base / "nope", "uninstall", "--yes")
    check("it exits 0", code == 0, f"code={code}")
    check("it reports the absence",
          "no lingling data dir" in out, out[:200])
    check("no traceback", "Traceback" not in out, out[:300])

    print("\n=== 7. the guard skips itself ===")
    # The uninstaller is a lingling process; if it counted itself the
    # command could never run at all.
    code, out = _run(base / "f", "uninstall", "--yes")
    check("it never reports itself as still running",
          "still running" not in out, out[:200])

    shutil.rmtree(base, ignore_errors=True)
    print()
    if FAILS:
        print(f"{len(FAILS)} failed:")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("all uninstall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())