"""Exit relays: pick distinct, country-filtered exits and pin them.

The generated torrc uses a bare ``SocksPort``, so Tor isolates streams by
destination and a lane's exit can differ per host. That is why ``exit_ip`` --
probed from check.torproject.org -- is **not** the exit a model call rides, and
why two lanes sharing an exit used to be undetectable.

Pinning a relay by fingerprint fixes both problems at once: the exit is known
before the lane boots, and giving every lane its own fingerprint makes "no two
lanes share an exit" a guarantee rather than a hope.

Verified live 2026-09-19: ``ExitNodes $FP`` + ``StrictNodes 1`` came out of
exactly that relay's address.

Country comes from the GeoIP CSV that Tor itself ships, so our idea of a
relay's country agrees with Tor's. Ranking is by advertised bandwidth, which is
the only quality signal available -- nobody can see how much anyone else has
used an exit.
"""
from __future__ import annotations

import ipaddress
import pathlib
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple


class Exit(NamedTuple):
    fingerprint: str
    ip: str
    nickname: str
    bandwidth: int


def load_geoip(path: pathlib.Path) -> List[Tuple[int, int, str]]:
    """Tor's GeoIP CSV as sorted ``(start, end, country)`` ranges."""
    rows: List[Tuple[int, int, str]] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.strip().split(",")
            if len(parts) != 3:
                continue
            try:
                rows.append((int(parts[0]), int(parts[1]), parts[2]))
            except ValueError:
                continue
    rows.sort()
    return rows


def country_of(ranges: Sequence[Tuple[int, int, str]], ip: str) -> str:
    """Country for an IPv4 address, or "??" when the database has no answer."""
    try:
        n = int(ipaddress.IPv4Address(ip))
    except ValueError:
        return "??"
    lo, hi = 0, len(ranges) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        start, end, cc = ranges[mid]
        if n < start:
            hi = mid - 1
        elif n > end:
            lo = mid + 1
        else:
            return cc
    return "??"


def load_relays(path: pathlib.Path,
                ranges: Sequence[Tuple[int, int, str]]
                ) -> Dict[str, List[Exit]]:
    """Usable exits from a microdesc consensus, grouped by country.

    Only relays flagged ``Exit`` and not ``BadExit`` are kept. The consensus
    carries no port policy -- that lives in the microdescriptors -- so a relay
    that cannot reach 443 is possible; `StrictNodes` would then refuse it, and
    the health daemon re-pins that lane.
    """
    import stem.descriptor

    by_country: Dict[str, List[Exit]] = {}
    with path.open("rb") as f:
        for desc in stem.descriptor.parse_file(
                f, descriptor_type="network-status-microdesc-consensus-3 1.0",
                validate=False):
            flags = set(desc.flags)
            if "Exit" not in flags or "BadExit" in flags:
                continue
            cc = country_of(ranges, desc.address)
            if cc == "??":
                continue
            by_country.setdefault(cc, []).append(
                Exit(desc.fingerprint, desc.address, desc.nickname,
                     int(desc.bandwidth or 0)))
    #: best first
    for exits in by_country.values():
        exits.sort(key=lambda e: -e.bandwidth)
    return by_country


def find_consensus(lanes_dir: pathlib.Path) -> Optional[pathlib.Path]:
    """Any lane's cached microdesc consensus; the first boot has none."""
    if not lanes_dir.exists():
        return None
    for path in sorted(lanes_dir.glob("tor-*/cached-microdesc-consensus")):
        return path
    return None


def claim(by_country: Dict[str, List[Exit]], country: str,
          avoid: Sequence[str], limited: Dict[str, float],
          used: Dict[str, float], now: float) -> Optional[Exit]:
    """A relay in ``country`` that no lane holds and nothing has limited.

    Ordered by freshness first, bandwidth second, so a lane gets an exit it
    has not been through lately rather than the same top-of-the-list relay on
    every boot. Without that the picker was deterministic: the highest
    bandwidth relay in each country won every time, which is also the most
    heavily shared one. Bandwidth only breaks ties between equally fresh
    relays."""
    held = set(avoid)
    pool = [e for e in by_country.get(country.upper(), ())
            if e.fingerprint not in held
            and limited.get(e.fingerprint, 0.0) <= now]
    if not pool:
        return None
    #: freshness
    pool.sort(key=lambda e: (used.get(e.fingerprint, 0.0), -e.bandwidth))
    return pool[0]
