"""Exit relays: pick distinct, country-filtered exits and pin them."""
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
    """Usable exits from a microdesc consensus, grouped by country."""
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
    """A relay in ``country`` that no lane holds and nothing has limited."""
    held = set(avoid)
    pool = [e for e in by_country.get(country.upper(), ())
            if e.fingerprint not in held
            and limited.get(e.fingerprint, 0.0) <= now]
    if not pool:
        return None
    #: freshness
    pool.sort(key=lambda e: (used.get(e.fingerprint, 0.0), -e.bandwidth))
    return pool[0]
