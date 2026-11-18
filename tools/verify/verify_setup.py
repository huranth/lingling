"""Setup checks."""

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import profile, settings, setup, state
from lingling.pool import Pool

SAMPLE = """[Interface]
PrivateKey = private
Address = 2001:db8:1:2::2/64

[Peer]
PublicKey = public
AllowedIPs = ::/1, 8000::/1
Endpoint = 192.0.2.1:51820
"""

BAD = SAMPLE + "\nping 2001:db8:1:2::1\n"

FAILS = []


def check(name, ok):
    print(("  [PASS] " if ok else "  [FAIL] ") + name)
    if not ok:
        FAILS.append(name)


def main():
    parsed = profile.parse(SAMPLE)
    check("parse address", parsed["address"] == "2001:db8:1:2::2/64")
    check("parse endpoint", parsed["endpoint"] == "192.0.2.1:51820")
    prefix = profile.prefix_network("2001:db8:1:300::/56")
    check("prefix normalization", prefix == "2001:db8:1:300::/56")

    pool = Pool("2001:db8:1:300::/56")
    expect = 1 << (settings.LANE_BITS - settings.ROUTE_BITS)
    check("lane count", len(pool.lanes) == expect)
    check("first lane", pool.lanes[0] == "2001:db8:1:300::2")
    check("last lane", pool.lanes[-1] == "2001:db8:1:3ff::2")

    with tempfile.TemporaryDirectory(prefix="ll-setup-") as folder:
        profile.save(folder, BAD, "2001:db8:1:300::/56")
        data = profile.load(folder)
        check("profile saved", data is not None)
        check("profile prefix", data["prefix"] == "2001:db8:1:300::/56")

        saved_text = profile.config_path(folder).read_text(encoding="utf-8")
        check("address kept", "Address = 2001:db8:1:2::2/64" in saved_text)
        check("junk removed", "ping" not in saved_text)

        state.spend(folder, pool.lanes[0])
        state.count(folder, pool.lanes[0])
        state.remember(folder, pool.lanes[0])
        saved = state.load(folder)
        check("lane spent", pool.lanes[0] in saved["spent"])
        check("lane counted", saved["served"].get(pool.lanes[0]) == 1)
        check("lane remembered", saved["last"] == pool.lanes[0])

        state.clear(folder)
        saved = state.load(folder)
        check("state cleared", saved["spent"] == [])
        check("counts cleared", saved["served"] == {})
        check("last cleared", saved["last"] is None)

        profile.remove(folder)
        check("profile removed", not profile.complete(folder))
        check("config removed", not profile.config_path(folder).exists())

        entry = Path(folder) / "entry.conf"
        entry.write_text(SAMPLE, encoding="utf-8")

        with patch("sys.stdin.isatty", return_value=False):
            ok = setup.run(folder, ["--config", str(entry)])
        check("setup needs all flags", not ok)

        with patch.object(setup.tunnel, "install", side_effect=RuntimeError("boom")):
            ok = setup.run(folder, [
                "--config", str(entry),
                "--prefix", "2001:db8:1:300::/56",
                "--yes",
            ])
        check("failed setup stops", not ok)
        check("failed setup keeps no profile", not profile.complete(folder))
        check("failed setup keeps no config", not profile.config_path(folder).exists())

        Path(folder, settings.SESSION_LOCK).write_text("1", encoding="utf-8")
        setup._reset(folder)
        check("reset keeps state", state.path(folder).is_file())
        check("reset keeps lock", Path(folder, settings.SESSION_LOCK).is_file())

    print()
    if FAILS:
        print("failed: " + ", ".join(FAILS))
        return 1

    print("setup checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
