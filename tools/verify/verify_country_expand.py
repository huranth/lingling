"""Offline proof that country rotation walks the pool and then widens it, and that live lane picking ..."""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import exits, netutil
from lingling.cli import DATA_DIR, load_countries
from lingling.lanes import LIMITED_PATH, Lane, TorManager
from lingling.relay import Relay


def build(count=6):
    """A TorManager with no tor, no disk: only the rotation state."""
    mgr = TorManager.__new__(TorManager)
    mgr.lanes = [
        Lane(index=i, socks_port=52000 + i, control_port=52300 + i,
             exit_country="zz", data_dir=Path("."))
        for i in range(1, count + 1)
    ]
    mgr._preferred = []
    mgr._quiet = ["aa", "bb", "cc"]
    mgr._fallback = ["dd", "ee"]
    mgr._expand = ["ff", "gg", "hh"]
    mgr._score = {}
    #: empty, so these checks never touch the disk
    mgr._by_country = {}
    mgr._limited = {}
    mgr._used = {}
    #: no disk
    mgr._write_json = lambda *a: None
    return mgr


FAIL = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("   " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


print("\n=== A. configured pool is walked, not just the head ===")
mgr = build()
lane = mgr.lanes[0]
picks = [mgr.rotate_exit_country(lane) for _ in range(3)]
check("three different configured countries", picks == ["aa", "bb", "cc"],
      f"picks={picks}")
check("no repeat while a fresh one remains", len(set(picks)) == 3)

print("\n=== B. expansion fires once the configured pool is exhausted ===")
mgr = build()
lane = mgr.lanes[0]
picks = [mgr.rotate_exit_country(lane) for _ in range(8)]
check("fallback reached after the quiet pool", picks[3:5] == ["dd", "ee"],
      f"picks={picks}")
check("expansion reached after the fallback pool", picks[5:8] == ["ff", "gg", "hh"],
      f"picks={picks}")
check("eight fresh countries, zero repeats", len(set(picks)) == 8)

print("\n=== C. an exhausted walk starts over instead of giving up ===")
# The old design persisted a country blacklist in
mgr = build()
lane = mgr.lanes[0]
picks = [mgr.rotate_exit_country(lane) for _ in range(10)]
check("it comes back to the top of the ladder", picks[8] == "aa",
      f"picks={picks}")
check("never unpins while the ladder still has countries",
      "*" not in picks, f"picks={picks}")
check("the cursor stays bounded", len(lane.recent_countries) <= 8,
      f"cursor={len(lane.recent_countries)}")

print("\n=== C2. an empty ladder is the only route to '*' ===")
mgr = build()
mgr._preferred, mgr._quiet, mgr._fallback, mgr._expand = [], [], [], []
check("an empty ladder unpins", mgr.rotate_exit_country(mgr.lanes[0]) == "*")

print("\n=== D. 200s steer the choice ===")
mgr = build()
lane = mgr.lanes[0]
for cc, status in [("aa", 429), ("aa", 429), ("cc", 200), ("cc", 200),
                   ("bb", 429)]:
    mgr.note_result(cc, status)
check("score tracks 200s minus failures",
      mgr._score == {"aa": -2, "cc": 2, "bb": -1}, f"score={mgr._score}")
pick = mgr.rotate_exit_country(lane)
check("highest scorer wins over pool order", pick == "cc", f"pick={pick}")

print("\n=== E. unpinned and unknown countries are not scored ===")
mgr = build()
mgr.note_result("*", 200)
mgr.note_result("", 200)
check("no phantom score entries", mgr._score == {}, f"score={mgr._score}")

print("\n=== E2. only the exit's own signals score ===")
# A 403 is the free tier gating on
mgr = build()
mgr.note_result("aa", 403)
mgr.note_result("aa", 500)
mgr.note_result("aa", 404)
mgr.note_result("aa", 200)
mgr.note_result("bb", 429)
check("a client gate does not charge the country",
      mgr._score == {"aa": 1, "bb": -1},
      f"score={mgr._score} -- something other than the exit was scored")

print("\n=== F. a lane never shares a country while a fresh one exists ===")
mgr = build()
mgr.lanes[0].exit_country = "aa"
mgr.lanes[1].exit_country = "bb"
pick = mgr.rotate_exit_country(mgr.lanes[2])
check("avoids countries other lanes hold", pick == "cc", f"pick={pick}")


def live_mgr(scores):
    """Healthy idle lanes on named countries, for the picker."""
    mgr = build()
    for l, cc in zip(mgr.lanes, ("aa", "bb", "cc", "dd", "ee", "ff")):
        l.healthy = True
        l.exit_country = cc
    mgr._score = dict(scores)
    return mgr


print("\n=== G. a losing country is picked last, not never ===")
# Live proof this guards: one 25-call lane ran
mgr = live_mgr({"aa": -3, "bb": 3})
relay = Relay(mgr)
picks = [relay.pick_lane().exit_country for _ in range(4)]
check("losing country not picked while better lanes are idle",
      "aa" not in picks, f"picks={picks}")
check("score_of reads the real record", mgr.score_of("aa") == -3,
      f"score_of(aa)={mgr.score_of('aa')}")
check("an unseen country reads 0", mgr.score_of("nope") == 0)

print("\n=== H. a losing lane is still the fallback, so the pool cannot wedge ===")
mgr = live_mgr({"aa": -3, "bb": 3})
relay = Relay(mgr)
for l in mgr.lanes:
    if l.exit_country != "aa":
        l.active = 2
pick = relay.pick_lane()
check("falls back to the losing lane instead of stalling",
      pick is not None and pick.exit_country == "aa",
      f"pick={getattr(pick, 'exit_country', None)}")

print("\n=== H2. a burst diverges evenly across the whole pool ===")
# The pool is a load balancer, not a
mgr = live_mgr({})
relay = Relay(mgr)
counts = {l.index: 0 for l in mgr.lanes}
for _ in range(24):
    lane = relay.pick_lane()
    lane.active += 1
    counts[lane.index] += 1
spread = max(counts.values()) - min(counts.values())
check("every lane carries its share", all(c > 0 for c in counts.values()),
      str(sorted(counts.values())))
check("and the spread is at most one request", spread <= 1,
      f"spread={spread} counts={sorted(counts.values())}")

print("\n=== I. one 429 is tolerated; a losing record is not ===")
mgr = live_mgr({"aa": -1, "bb": -2})
relay = Relay(mgr)
pick = relay.pick_lane()
check("a single failure outranks a losing record",
      pick.exit_country == "aa", f"pick={pick.exit_country}")

print("\n=== J. a thin pool spreads instead of playing favourites ===")
# Below _LOSING_MIN_POOL the losing rule is off on
mgr = build()
for l in mgr.lanes[:3]:
    l.healthy = True
for l, cc in zip(mgr.lanes[:3], ("aa", "bb", "cc")):
    l.exit_country = cc
mgr._score = {"aa": -9, "bb": 9, "cc": 9}
relay = Relay(mgr)
picks = [relay.pick_lane().exit_country for _ in range(3)]
check("a thin pool still uses the losing lane", "aa" in picks,
      f"picks={picks}")

print("\n=== K. an exit the far end limited is picked last ===")
# Live proof the rule exists: identical probes through
mgr = live_mgr({})
relay = Relay(mgr)
mgr.lanes[0].limited_until = time.time() + 12000
picks = [relay.pick_lane().index for _ in range(3)]
check("a limited lane is not picked while others are free",
      1 not in picks, f"picks={picks}")

mgr.lanes[0].limited_until = 0.0
picks = [relay.pick_lane().index for _ in range(3)]
check("and it returns once its deadline passes", 1 in picks,
      f"picks={picks}")

print("\n=== L. every lane limited -> still no deadlock ===")
mgr = live_mgr({})
relay = Relay(mgr)
for l in mgr.lanes:
    l.limited_until = time.time() + 12000
pick = relay.pick_lane()
check("a fully limited pool still hands out a lane", pick is not None,
      str(pick))

print("\n=== M. every lane gets its own pinned exit ===")
# Live proof the pin holds: ExitNodes $FP +
LANES = DATA_DIR / "lanes"
GEO = DATA_DIR / "geoip"


def pinned_mgr(countries=("tr", "ua", "is", "hr", "bg", "hk")):
    """A real TorManager, built normally, pointed at the on-disk relay list."""
    mgr = TorManager(DATA_DIR, count=len(countries),
                     exit_countries=list(countries), log=lambda *a: None)
    return mgr


mgr = pinned_mgr()
loaded = mgr._load_exits()
check("the relay list loads from a lane's cached consensus", loaded)
if loaded:
    for l in mgr.lanes:
        mgr._pin(l)
    fps = [l.exit_fingerprint for l in mgr.lanes]
    check("every lane got a pin", all(fps), f"{sum(1 for f in fps if f)}/6")
    check("no two lanes share an exit", len(set(fps)) == len(fps), str(fps))
    check("the exit is known without probing",
          all(l.exit_ip for l in mgr.lanes),
          str([l.exit_ip for l in mgr.lanes]))
    cfg = mgr._lane_config(mgr.lanes[0])
    check("the torrc pins that exact relay",
          cfg.get("ExitNodes") == "$" + mgr.lanes[0].exit_fingerprint
          and cfg.get("StrictNodes") == "1", str(cfg.get("ExitNodes")))

    ranges = exits.load_geoip(GEO)
    wrong = [f"{l.exit_country}->{exits.country_of(ranges, l.exit_ip)}"
             for l in mgr.lanes
             if exits.country_of(ranges, l.exit_ip) != l.exit_country.upper()]
    check("every pinned exit is in its lane's own country", not wrong,
          str(wrong))

    print("\n=== N. a relay the far end limited is retired, not re-handed ===")
    victim = mgr.lanes[0]
    first = victim.exit_fingerprint
    mgr._limited[first] = time.time() + 12000
    mgr._pin(victim)
    check("the lane moves to a different relay",
          victim.exit_fingerprint != first,
          f"{first[:12]} -> {victim.exit_fingerprint[:12]}")
    check("the limited relay is not given to any lane",
          first not in [l.exit_fingerprint for l in mgr.lanes])

    print("\n=== O. two lanes in the SAME country still differ ===")
    mgr2 = pinned_mgr(countries=("de", "de", "de", "de", "de", "de"))
    mgr2._load_exits()
    for l in mgr2.lanes:
        mgr2._pin(l)
    fps2 = [l.exit_fingerprint for l in mgr2.lanes]
    check("six lanes, six distinct exits in one country",
          all(fps2) and len(set(fps2)) == 6,
          f"{sum(1 for f in fps2 if f)}/6 pins, "
          f"{len(set(fps2))} distinct")

    print("\n=== P. an exhausted country degrades to country-only ===")
    mgr3 = pinned_mgr(countries=("hk",) * 6)      # 7 exits, ask for 6
    mgr3._load_exits()
    for l in mgr3.lanes:
        mgr3._pin(l)
    hk = len([l for l in mgr3.lanes if l.exit_fingerprint])
    check("it pins as many as it can", hk > 0, f"{hk}/6")
    # retire every HK relay there is, not just
    for exit_ in (mgr3._by_country or {}).get("HK", []):
        mgr3._limited[exit_.fingerprint] = time.time() + 12000
    mgr3._pin(mgr3.lanes[0])
    check("with none left it falls back to country-only",
          mgr3.lanes[0].exit_fingerprint == "",
          str(mgr3.lanes[0].exit_fingerprint))
    check("and the torrc then uses the country form",
          mgr3._lane_config(mgr3.lanes[0]).get("ExitNodes") == "{hk}",
          str(mgr3._lane_config(mgr3.lanes[0]).get("ExitNodes")))
else:
    check("relay list unavailable -- pinning degrades to country only", True)

print("\n=== Q. countries.txt parsing ===")
# A comment line must vanish rather than count
sample = """# a comment line
de,nl  # trailing note
# fallback next
sg,ro

# preferred is blank on purpose
"""
with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp) / "countries.txt"
    p.write_text(sample, encoding="utf-8")
    primary, fallback, preferred = load_countries(p)
check("comments never become pools", primary == ["de", "nl"], str(primary))
check("a commented-out list does not leak", "jp" not in primary
      and "md" not in primary, str(primary))
check("the fallback lands on pool two", fallback == ["sg", "ro"],
      str(fallback))
check("a blank third pool stays empty", preferred == [], str(preferred))

with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp) / "countries.txt"
    p.write_text("de,nl\n\nsg,ro\n", encoding="utf-8")
    primary, fallback, preferred = load_countries(p)
check("a real blank line still skips a pool",
      primary == ["de", "nl"] and fallback == [] and preferred == ["sg", "ro"],
      f"{primary} / {fallback} / {preferred}")

print("\n=== R. limited exits survive a restart ===")
# A retry-after runs to hours while a session
LIMITED = "AA" * 20
EXPIRED = "BB" * 20
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    first = TorManager(root, count=1, exit_countries=["de"],
                       log=lambda *a: None)
    lane = first.lanes[0]
    # Windows-excluded ranges swallow whole blocks. The defaults (socks
    check("the lane's ports are bindable",
          netutil.bindable(lane.socks_port)
          and netutil.bindable(lane.control_port),
          f"{lane.socks_port}/{lane.control_port}")
    lane.exit_fingerprint = LIMITED
    lane.limited_until = time.time() + 12000
    first.note_limited(lane)
    check("the limit is written down",
          (root / LIMITED_PATH).is_file())

    second = TorManager(root, count=1, exit_countries=["de"],
                        log=lambda *a: None)
    check("a new manager remembers it",
          second._limited.get(LIMITED, 0) > time.time(), str(second._limited))

    (root / LIMITED_PATH).write_text(
        json.dumps({EXPIRED: time.time() - 1}), encoding="utf-8")
    third = TorManager(root, count=1, exit_countries=["de"],
                       log=lambda *a: None)
    check("an expired limit is forgotten", EXPIRED not in third._limited,
          str(third._limited))

    (root / LIMITED_PATH).write_text("not json at all",
                                           encoding="utf-8")
    fourth = TorManager(root, count=1, exit_countries=["de"],
                        log=lambda *a: None)
    check("a corrupt file never breaks startup", fourth._limited == {},
          str(fourth._limited))

print("\n=== S. restarting a limited lane re-pins it ===")
# The pin lives in the torrc, so a
mgr5 = build()
mgr5._by_country = {}
mgr5._limited = {}
mgr5._stopping = False
mgr5._stop_lane_process = lambda l: None
mgr5._write_torrc = lambda l: None
mgr5._launch_lane = lambda l: "started"
mgr5._write_json = lambda *a: None   # persistence is section R's job
pinned = {"n": 0}
mgr5._pin = lambda l: pinned.__setitem__("n", pinned["n"] + 1)
victim5 = mgr5.lanes[0]
victim5.exit_fingerprint = "C" * 40
victim5.socks_port = 59901        # unused, so no live process is ever probed
victim5.control_port = 59902
victim5.limited_until = 0.0
mgr5.note_limited(victim5, 9518)      # what the transport does on a 429
mgr5.restart_lane(victim5)
check("a limited lane is re-pinned on restart", pinned["n"] == 1,
      f"pins={pinned['n']}")
check("its old relay is out until the named reset",
      mgr5._limited.get("C" * 40, 0) > time.time(), str(mgr5._limited))

pinned["n"] = 0
fresh = mgr5.lanes[1]
fresh.exit_fingerprint = "D" * 40
fresh.socks_port = 59903
fresh.control_port = 59904
fresh.limited_until = 0.0         # not limited: a plain dead-lane restart
mgr5.restart_lane(fresh)
check("a lane that is merely dead is NOT re-pinned", pinned["n"] == 0,
      f"pins={pinned['n']}")

print("\n=== T. a lane with a live request is not torn down mid-flight ===")
# Tearing a lane down while it is carrying
mgr6 = build()
mgr6._by_country = {}
mgr6._limited = {}
mgr6._stopping = False
mgr6._write_json = lambda *a: None
mgr6._write_torrc = lambda l: None
mgr6._launch_lane = lambda l: "started"
stopped = []
mgr6._stop_lane_process = lambda l: stopped.append(l.index)

lane6 = mgr6.lanes[0]
lane6.socks_port = 59905
lane6.control_port = 59906
lane6.active = 1
threading.Thread(target=lambda: (time.sleep(0.6),
                                 setattr(lane6, "active", 0)),
                 daemon=True).start()
t0 = time.time()
mgr6.restart_lane(lane6)
elapsed = time.time() - t0
check("the restart waited for the live request",
      elapsed >= 0.5 and stopped == [1],
      f"waited {elapsed:.1f}s, stopped={stopped}")

lane7 = mgr6.lanes[1]
lane7.socks_port = 59907
lane7.control_port = 59908
lane7.active = 0
t0 = time.time()
mgr6.restart_lane(lane7)
idle = time.time() - t0
check("an idle lane restarts without waiting for a drain", idle < elapsed,
      f"idle={idle:.2f}s vs busy={elapsed:.2f}s")

print("\n=== U. a fully limited pool stops retrying instead of amplifying ===")
# Live: 3.5 attempts per request with 71% of
mgr7 = build()
relay7 = Relay(mgr7)
for l in mgr7.lanes:
    l.limited_until = 0.0
check("an unlimited lane counts", relay7.any_unlimited(set()))
check("excluding every lane leaves none",
      not relay7.any_unlimited({1, 2, 3, 4, 5, 6}))
for l in mgr7.lanes:
    l.limited_until = time.time() + 12000
check("a fully limited pool has nothing left",
      not relay7.any_unlimited(set()))
mgr7.lanes[2].limited_until = 0.0
check("one unlimited lane is enough", relay7.any_unlimited(set()))

print("\n=== V. a lane already carrying traffic is still used ===")
# There is no concurrency cap. A healthy lane
mgr8 = live_mgr({})
for l in mgr8.lanes:
    l.active = 5
relay8 = Relay(mgr8)
pick = relay8.pick_lane()
check("a loaded pool still hands out a lane", pick is not None,
      str(getattr(pick, "index", None)))
check("nothing is benched for being loaded",
      all(l.healthy for l in mgr8.lanes))

mgr9 = live_mgr({})
for l in mgr9.lanes:
    l.active = 5
mgr9.lanes[3].active = 0
check("the least loaded lane wins", Relay(mgr9).pick_lane().index == 4)

print("\n=== W. every exit handed out is fresh and distinct ===")
# A lane must not get the same top-bandwidth
small = exits.Exit("A" * 40, "10.0.0.1", "small", 1)
big = exits.Exit("B" * 40, "10.0.0.2", "big", 999)
pool = {"DE": [big, small]}
now = time.time()

pick = exits.claim(pool, "de", [], {}, {}, now)
check("with both fresh, bandwidth decides", pick.fingerprint == big.fingerprint,
      pick.nickname)
pick = exits.claim(pool, "de", [], {}, {big.fingerprint: now + 100}, now)
check("a used relay is passed over for a fresh one", pick is small,
      pick.nickname)
pick = exits.claim(pool, "de", [], {},
                   {small.fingerprint: now + 100,
                    big.fingerprint: now + 200}, now)
check("with both used, the least recent wins", pick is small, pick.nickname)
check("a limited relay is never handed out",
      exits.claim(pool, "de", [], {small.fingerprint: now + 100,
                                   big.fingerprint: now + 100}, {}, now) is None)
check("a relay another lane holds is never handed out",
      exits.claim(pool, "de", [small.fingerprint, big.fingerprint],
                  {}, {}, now) is None)
check("an empty country hands out nothing",
      exits.claim(pool, "zz", [], {}, {}, now) is None)

print("\n" + "=" * 46)
if FAIL:
    print(f"FAILURES: {len(FAIL)}")
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("COUNTRY EXPANSION OK")
