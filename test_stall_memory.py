"""Stall strikes must survive healthy probes; bad exits get blocklisted."""
import sys, time, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from lingling.lanes import Lane, TorManager
from lingling.health import HealthDaemon
from lingling.relay import Relay


def make_lane(idx, cc="gb", ip="1.2.3.4"):
    return Lane(index=idx, exit_country=cc, exit_ip=ip,
                socks_port=52000 + idx, control_port=52300 + idx,
                data_dir=Path(f"lane{idx}"))


# --- 1. healthy probe must NOT forgive stall strikes -----------------------
tor = TorManager.__new__(TorManager)  # skip filesystem/reuse setup
tor.lanes = [make_lane(1)]
tor._bad_exits = {}
tor._preferred = []
tor._quiet = ["cz"]
tor._fallback = []
lane = tor.lanes[0]
lane.healthy = True
lane.stall_cycles = 1  # one real-traffic strike

daemon = HealthDaemon.__new__(HealthDaemon)
daemon.tor = tor
daemon._emit = lambda e: None
daemon._warmup = False
daemon._stop = threading.Event()
daemon.probe_lane = lambda l: "healthy"  # probe always succeeds

daemon.check_once()
assert lane.stall_cycles == 1, "healthy probe forgave a stall strike"
print("ok: stall strike survives a healthy probe")

# --- 2. second strike pulls the lane even with a success in between --------
class FakeRelay:
    def __init__(self, tor): self.tor = tor; self.events = []
    def _emit(self, e): self.events.append(e)
    report_burn = Relay.report_burn
    report_stall = Relay.report_stall

relay = FakeRelay(tor)
relay.report_stall(lane)   # strike 1
lane.stall_cycles = 0      # old behavior: a success would erase the strike
relay.report_stall(lane)   # strike 2 -- window still remembers the first
assert lane.healthy is False, "two stalls in-window did not pull the lane"
assert ("gb", "1.2.3.4") in tor._bad_exits, "stalling exit not blocklisted"
print("ok: alternating stall/success still pulls the lane + blocklists exit")

# --- 3. successful restart heals and clears the record ---------------------
tor.restart_lane = lambda l: True
daemon.check_once()  # probe ok + parked -> forced 'dead' -> restart heal
assert lane.stall_cycles == 0, "successful heal did not clear stall record"
print("ok: successful heal clears the stall record")

# --- 4. healthy lane landing on a bad exit gets a fresh circuit ------------
lane.healthy = True
lane.stall_cycles = 0
renewed = []
tor.renew = lambda l: (renewed.append(l.index) or True)
tor.is_bad_exit = TorManager.is_bad_exit.__get__(tor)
daemon.check_once()
assert renewed == [1], "known-bad exit did not trigger a circuit renew"
print("ok: healthy lane on a blocklisted exit gets NEWNYM'd")

# --- 5. blocklist entries expire -------------------------------------------
tor._bad_exits[("gb", "9.9.9.9")] = [time.time() - TorManager._BAD_EXIT_TTL_S - 1, 1]
assert not tor.is_bad_exit("gb", "9.9.9.9"), "stale blocklist entry not expired"
print("ok: blocklist entries expire")

# --- 5b. repeat offenders earn longer bans -----------------------------------
tor._bad_exits[("gb", "8.8.8.8")] = [time.time() - TorManager._BAD_EXIT_TTL_S - 1, 3]
assert tor.is_bad_exit("gb", "8.8.8.8"), "repeat offender ban did not escalate"
print("ok: repeat offenders get escalating bans")

# --- 6. repeated failed dodges escalate to a full re-cook -------------------
lane.exit_ip = "1.2.3.4"  # still the bad exit after two NEWNYMs
lane.bad_dodges = 2
regened = []
tor.regenerate_lane = lambda l: (regened.append(l.index) or True)
daemon.check_once()
assert regened == [1], "stubborn bad exit did not escalate to re-cook"
assert lane.healthy is False, "re-cooking lane stayed in rotation"
print("ok: stubborn bad exit escalates to a full re-cook")

# --- 6b. a single flaky probe must NOT flip a lane's healthy flag ------------
# (that flip would make the next good probe re-emit a duplicate "cooking" line)
tor3 = TorManager.__new__(TorManager)  # skip filesystem setup
tor3.lanes = [make_lane(10, cc="any", ip="10.0.0.10")]
tor3._bad_exits = {}
tor3._preferred = []
tor3._quiet = []
tor3._fallback = []
l30 = tor3.lanes[0]
l30.healthy = True
l30.unhealthy_cycles = 0

daemon3 = HealthDaemon.__new__(HealthDaemon)
daemon3.tor = tor3
emitted = []
daemon3._emit = emitted.append
daemon3._stop = threading.Event()
daemon3._warmup = False
daemon3.probe_lane = lambda l: "dead"  # one transient timeout

daemon3.check_once()
assert l30.healthy is True, "a single flaky probe flipped the lane healthy flag"
assert l30.unhealthy_cycles == 1, "flaky probe did not tick the unhealthy counter"
print("ok: single flaky probe does not flip healthy flag")

# next good probe: no duplicate "cooking" line, stays healthy
daemon3.probe_lane = lambda l: "healthy"
daemon3.check_once()
assert l30.healthy is True
assert not any(e.get("kind") == "up" for e in emitted), \
    "healthy probe after a flaky one re-emitted a duplicate 'cooking' line"
print("ok: no duplicate 'cooking' line after a flaky probe")

# --- 6c. a NEWNYM renew re-fingerprints the exit immediately -----------------
# exit_ip stays the known-bad IP (fingerprint would have set it); the dodge
# must fire and then re-stamp the lane rather than leaving it stale.
# Fresh daemon + lane: the shared top-level daemon is stateful after 6.
lane6c = make_lane(20, cc="gb", ip="1.2.3.4")
tor6c = TorManager.__new__(TorManager)
tor6c.lanes = [lane6c]
tor6c._bad_exits = {("gb", "1.2.3.4"): [time.time(), 1]}
tor6c._preferred = []
tor6c._quiet = ["cz"]
tor6c._fallback = []
lane6c.healthy = True
lane6c.bad_dodges = 0

daemon6c = HealthDaemon.__new__(HealthDaemon)
daemon6c.tor = tor6c
daemon6c._emit = lambda e: None
daemon6c._warmup = False
daemon6c._stop = threading.Event()
daemon6c.probe_lane = lambda l: "healthy"
fingerprinted = []
tor6c.renew = lambda l: (fingerprinted.append("renew") or True)
tor6c.is_bad_exit = TorManager.is_bad_exit.__get__(tor6c)
daemon6c._fingerprint = lambda lane: fingerprinted.append("fingerprint")
daemon6c.check_once()
assert fingerprinted == ["renew", "fingerprint"], \
    f"renew did not re-fingerprint immediately: {fingerprinted}"
print("ok: NEWNYM renew re-fingerprints the lane right after")

print("ALL PASS")

# --- 7. preferred country is sticky until exhausted -------------------------
import tempfile
tor2 = TorManager(Path(tempfile.mkdtemp()), count=3,
                  exit_countries=["cz", "hr"], fallback_countries=["ro"],
                  preferred_countries=["gb"])
assert [l.exit_country for l in tor2.lanes] == ["gb", "gb", "gb"], \
    f"lanes did not boot on preferred country: {[l.exit_country for l in tor2.lanes]}"
lane = tor2.lanes[0]
assert tor2.rotate_exit_country(lane) == "gb", "preferred country not sticky"
print("ok: lanes boot on preferred country and stick through burns")

# exhaust gb: 4 distinct bad exits inside the window
for i in range(TorManager._PREFERRED_EXHAUST_EXITS):
    tor2._bad_exits[("gb", f"10.0.0.{i}")] = [time.time(), 1]
new_cc = tor2.rotate_exit_country(lane)
assert new_cc == "cz", f"exhausted preferred country did not fall back: {new_cc}"
print("ok: exhausted preferred country falls back to the quiet pool")
print("ALL PASS")
