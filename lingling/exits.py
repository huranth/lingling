"""Exit relays: pick distinct, country-filtered exits and pin them.

One country holds more exits than one lane could ever use, and the far end
counts its day against the exit's address, so the head of a pool is the most
contested ground in it.  A pick therefore draws a random handful of what is
open and takes the best of those: bandwidth still counts, it just stops being
the only thing that does.
"""
from __future__ import annotations

import ipaddress
import pathlib
import random
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import bounds

EXIT_SAMPLE_K = bounds.count("LINGLING_EXIT_SAMPLE", 12, 1, 4096)

WORLD_CC = frozenset([
    "us", "de", "nl", "fr", "se", "ch", "at", "ca", "gb", "ro",
    "fi", "pl", "cz", "hu", "bg", "lt", "lv", "ee", "dk", "no",
    "ie", "es", "it", "pt", "gr", "ua", "md", "rs", "hr", "si",
    "sk", "is", "lu", "be", "au", "nz", "jp", "kr", "sg", "hk",
    "tw", "th", "in", "id", "my", "za", "br", "ar", "cl", "mx",
    "il", "ae", "tr", "mt", "cy", "uy", "pe", "co", "ke", "mu",
])


class Exit(NamedTuple):
    fingerprint: str
    ip: str
    nickname: str
    bandwidth: int


def exit_key(where: Any) -> str:
    """What the far end counts an allowance against: the exit's address.

    A lane or a relay may be handed in, and the address is only unknown until
    the lane has been probed -- so a relay with no address yet is keyed by
    itself.  Two relays behind one address are one allowance, which is why the
    address is the key and the fingerprint is not.
    """
    ip = str(getattr(where, "exit_ip", "") or getattr(where, "ip", "")
             or "").strip()
    if ip and ip != "?":
        return ip
    return str(getattr(where, "exit_fingerprint", "")
               or getattr(where, "fingerprint", "") or "")


def random_countries(n: int,
                     restrict: Optional[Sequence[str]] = None) -> List[str]:
    """``n`` distinct countries drawn at random from anywhere with exits."""
    pool = sorted(set(restrict) & WORLD_CC if restrict else WORLD_CC)
    if not pool:
        pool = sorted(WORLD_CC)
    k = max(1, min(int(n), len(pool)))
    return random.sample(pool, k)


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


def limited_until(entry: Any) -> float:
    """A notepad entry's expiry, in either the current or the older shape."""
    if isinstance(entry, dict):
        entry = entry.get("until")
    try:
        return float(entry)
    except (TypeError, ValueError):
        return 0.0


def _spent_for(exit_: Exit, spent: Dict[str, int]) -> int:
    """What the day has spent on a relay, under either name it was booked by.

    A lane carries its address only once it has been probed, so a request made
    before that sits on the books under the relay's own fingerprint.
    """
    return spent.get(exit_key(exit_), 0) or spent.get(exit_.fingerprint, 0)


def _benched(exit_: Exit, limited: Dict[str, Any], now: float) -> bool:
    """True when nothing more can be had on this exit today.

    The far end meters the address, so a wall met behind one relay is a wall
    for every relay sharing that address -- and a relay booked under its own
    fingerprint is asked about under either name.
    """
    key = exit_key(exit_)
    if key and limited_until(limited.get(key, 0.0)) > now:
        return True
    return limited_until(limited.get(exit_.fingerprint, 0.0)) > now


def _has_room(exit_: Exit, spent: Dict[str, int], cap: int) -> bool:
    """True while the day's allowance on this exit is not yet used up."""
    return not cap or _spent_for(exit_, spent) < cap


def _open(relays: Sequence[Exit], avoid: Sequence[str],
          limited: Dict[str, Any], now: float,
          avoid_ip: Sequence[str] = (),
          spent: Optional[Dict[str, int]] = None, cap: int = 0) -> List[Exit]:
    """Only the relays still worth asking, and never one that is spent.

    Held by another lane, behind an address another lane rides, written off by
    the notepad, or already carried to the day's cap: none of those can serve
    a request, so none of them is handed out.  What is left is what a lane may
    be moved onto -- and if nothing is left, the answer is nothing, not a
    spent exit.
    """
    held = set(avoid)
    shared = {ip for ip in avoid_ip if ip}
    book = spent or {}
    return [e for e in relays
            if e.fingerprint not in held
            and e.ip not in shared
            and not _benched(e, limited, now)
            and _has_room(e, book, cap)]


def claim(by_country: Dict[str, List[Exit]], country: str,
          avoid: Sequence[str], limited: Dict[str, Any],
          used: Dict[str, float], now: float,
          verified: Optional[Dict[str, float]] = None,
          spent: Optional[Dict[str, int]] = None,
          avoid_ip: Sequence[str] = (),
          cap: int = 0,
          sample: int = EXIT_SAMPLE_K) -> Optional[Exit]:
    """The exit in ``country`` with the most left, or None when there is none.

    Never a spent one: a lane put on an exit the far end has already refused
    spends its first real call finding out, and that call is gone.  One
    country can hold a thousand exits, and taking its head every time is how
    a handful of well-published relays end up carrying a whole session while
    the rest of the consensus sits idle -- and those same relays are the ones every other
    user of the pool is leaning on too.  So a wide pool is drawn down to a
    random handful first, and the least spent of that handful wins: bandwidth
    still counts, it just stops being the only thing that does.
    """
    fresh = verified or {}
    book = spent or {}
    pool = _open(by_country.get(country.upper(), ()), avoid, limited, now,
                 avoid_ip, book, cap)
    if not pool:
        return None
    if sample and len(pool) > sample:
        pool = random.sample(pool, sample)
    pool.sort(key=lambda e: (
        _spent_for(e, book),
        0 if fresh.get(e.fingerprint, 0.0) > now else 1,
        used.get(e.fingerprint, 0.0),
        -e.bandwidth))
    return pool[0]


def sweep_order(by_country: Dict[str, List[Exit]], country: str,
                avoid: Sequence[str], limited: Dict[str, Any],
                used: Dict[str, float], verified: Dict[str, float],
                now: float, spent: Optional[Dict[str, int]] = None,
                avoid_ip: Sequence[str] = (),
                cap: int = 0) -> List[Exit]:
    """Relays in ``country`` still worth rotating onto, freshest first."""
    book = spent or {}
    pool = _open(by_country.get(country.upper(), ()), avoid, limited, now,
                 avoid_ip, book, cap)
    return sorted(pool,
                  key=lambda e: (_spent_for(e, book),
                                  0 if verified.get(e.fingerprint, 0.0) > now
                                  else 1,
                                  used.get(e.fingerprint, 0.0),
                                  random.random()))


def headroom(by_country: Dict[str, List[Exit]], country: str,
             avoid: Sequence[str], limited: Dict[str, Any],
             spent: Optional[Dict[str, int]], now: float,
             avoid_ip: Sequence[str] = (), cap: int = 0) -> int:
    """How many exits ``country`` can still serve us today.

    A country whose exits are all spent is a country to stop rotating into,
    however well it scored earlier in the day.
    """
    return len(_open(by_country.get(country.upper(), ()), avoid, limited, now,
                     avoid_ip, spent or {}, cap))
