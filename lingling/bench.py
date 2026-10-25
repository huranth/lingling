"""The limit bench: what a limited exit earned, and how long it sits out.

A gated exit sits out ``GATE_COOLDOWN_S``, not the free-tier day.  The gate's
403 names the CLIENT ("the free tier can only be used from within OpenCode"),
not the exit, and it refuses every exit in a wave at once.  Benching each of
them until 00:00 UTC is what turned one tripped call into a dead pool:
measured 2026-10-18, fifty refusals in ninety seconds wrote the fleet off for
the rest of the day.  A few minutes stops us re-racing the exit and still lets
the pool come back on its own.

Two ledgers live here, both keyed to the 00:00 UTC the free tier resets on.
The notepad holds what an exit was benched for.  The spend ledger holds how
much of that day we have put through each exit ourselves, which is what lets a
lane walk past an exit we have leaned on -- and off a wall we would otherwise
only meet by hitting it.  ``EXIT_DAY_CAP`` is that day's observed allowance
for one exit IP; ``EXIT_SOFT_CAP`` is how much of it one lane may lean on one
exit before the sweep moves on, and ``EXIT_HOLD_S`` is the other reason to
leave: an address is only as fresh as the last person who spent it, so a lane
that has ridden one for a long time gives up more than it gains by staying.
"""

from __future__ import annotations

import json
import time
from typing import Dict, Optional

from . import bounds, exits

RATE_LIMIT_FALLBACK_S = 1500.0

_LIMIT_CEILING_S = 24 * 3600 + 300

FREE_LIMIT = "free"
RATE_LIMIT = "rate"

#: gate closed
GATE_LIMIT = "gate"

LIMITED_PATH = "limited-exits.json"

USED_PATH = "used-exits.json"

SCORE_PATH = "country-scores.json"

VERIFIED_PATH = "verified-exits.json"

MODEL_OK_PATH = "model-exits.json"

SPENT_PATH = "exit-spend.json"

USED_TTL_S = bounds.number("LINGLING_FRESH_S", 86400,
                           0.0, 30 * bounds.WINDOW_S)

VERIFIED_TTL_S = bounds.number("LINGLING_VERIFIED_S", 1800,
                               0.0, 30 * bounds.WINDOW_S)

#: how long an exit keeps its "served a model call" mark
MODEL_OK_TTL_S = bounds.number("LINGLING_MODEL_OK_S", 3600,
                               0.0, 30 * bounds.WINDOW_S)

EXIT_DAY_CAP = bounds.count("LINGLING_EXIT_CAP", 200, 1, 1000000)

EXIT_SOFT_CAP = bounds.count("LINGLING_EXIT_SOFT_CAP",
                             EXIT_DAY_CAP // 2, 1, 1000000)

EXIT_HOLD_S = bounds.number("LINGLING_EXIT_HOLD_S", 600, 0.0, bounds.WINDOW_S)

#: gate cooldown
GATE_COOLDOWN_S = bounds.number("LINGLING_GATE_COOLDOWN_S", 300,
                                0.0, bounds.WINDOW_S)


def day_of(now: Optional[float] = None) -> int:
    """Which free-tier day ``now`` falls in: the 00:00 UTC that owns it."""
    moment = time.time() if now is None else now
    return int(moment // 86400)


def next_free_reset(now: Optional[float] = None) -> float:
    """The free tier's next 00:00 UTC, as an epoch second."""
    moment = time.time() if now is None else now
    return moment + (86400 - (moment % 86400))


def bench_span(reason: str, retry_after: float = 0.0,
               now: Optional[float] = None) -> float:
    """How long a limit benches an exit: the far end's reset, within the rule."""
    moment = time.time() if now is None else now
    if reason == RATE_LIMIT:
        return (min(retry_after, RATE_LIMIT_FALLBACK_S) if retry_after > 0
                else RATE_LIMIT_FALLBACK_S)
    if reason == GATE_LIMIT:
        return min(GATE_COOLDOWN_S, _LIMIT_CEILING_S)
    if retry_after > 0:
        return min(retry_after, _LIMIT_CEILING_S)
    return min(next_free_reset(moment) - moment, _LIMIT_CEILING_S)


class BenchMixin:
    """Held by a lane manager: reads ``self.root``, carries the ledgers.

    ``self._score``, ``self._limited``, ``self._used``, ``self._verified`` and
    ``self._spent`` are filled in by the owner's constructor. Every write is
    best effort: bookkeeping never breaks a run.
    """

    def score_of(self, country: str) -> int:
        """How this country has actually done: 200s minus everything else."""
        return self._score.get(country, 0)

    def spent_of(self, key: str) -> int:
        """How many requests we have put through one exit today."""
        return self._spent.get(key, 0)

    def note_spend(self, key: str) -> None:
        """Count one landed request against the exit that carried it.

        ``key`` is the exit as the far end meters it, which is its address
        rather than its relay: an exit IP is shared ground, so counting what we
        ourselves have put through it is what lets the picker walk past an exit
        we have been leaning on and onto one nobody has touched yet -- the
        alternative is arriving at the wall and only then finding out.
        """
        if not key:
            return
        self._roll_spent()
        self._spent[key] = self._spent.get(key, 0) + 1
        self._save_spent()

    def _roll_spent(self) -> None:
        """A new 00:00 UTC empties the ledger: the far end's day starts over."""
        day = day_of()
        if getattr(self, "_spent_day", None) != day:
            self._spent = {}
            self._spent_day = day

    def _load_spent(self) -> Dict[str, int]:
        """Today's ``{exit: requests}``; another day's ledger is dropped."""
        self._spent_day = day_of()
        raw = self._read_json(SPENT_PATH)
        if not isinstance(raw, dict):
            return {}
        try:
            if int(raw.get("day", -1)) != self._spent_day:
                return {}
        except (TypeError, ValueError):
            return {}
        counts = raw.get("spent")
        if not isinstance(counts, dict):
            return {}
        out: Dict[str, int] = {}
        for fp, seen in counts.items():
            try:
                out[str(fp)] = max(0, int(seen))
            except (TypeError, ValueError):
                continue
        return out

    def _save_spent(self) -> None:
        """Best effort: persist the day's per-exit counts, with their day."""
        self._write_json(SPENT_PATH,
                         {"day": getattr(self, "_spent_day", day_of()),
                          "spent": self._spent})

    def note_result(self, country: str, status: int) -> None:
        """Score a country by what the EXIT did: a 200, or a 429."""
        if not country or country == "*":
            return
        if status not in (200, 429):
            return
        self._score[country] = self._score.get(country, 0) + (
            1 if status == 200 else -1)
        self._save_score()

    def note_limited(self, lane, retry_after: float = 0.0,
                     reason: str = RATE_LIMIT,
                     fingerprint: str = "") -> float:
        """Write a limited exit into the notepad: the section, and until when.

        ``fingerprint`` names the exit the verdict is about, which is not always
        the one the lane rides by the time the verdict is read: a copy already
        in flight when its lane was moved keeps reporting on the exit it
        dialled.  The notepad always takes that exit, and the lane's clock is
        only set while it still rides it -- a lane that has been re-pinned is
        never left sitting out a wall that belongs to an exit it no longer
        uses, which is exactly how a whole lane used to sit out a session.
        """
        exit_fp = fingerprint or lane.exit_fingerprint
        if not exit_fp:
            return 0.0
        until = self.note_exit(exit_fp, reason, retry_after)
        if exit_fp == lane.exit_fingerprint:
            lane.limited_until = until
            lane.limit_reason = reason
        return until

    def note_exit(self, fingerprint: str, reason: str = RATE_LIMIT,
                  retry_after: float = 0.0) -> float:
        """Bench one fingerprint, and take back any 200 it earned."""
        if not fingerprint:
            return 0.0
        now = time.time()
        until = now + bench_span(reason, retry_after, now)
        self._limited[fingerprint] = {"until": until, "reason": reason}
        self._verified.pop(fingerprint, None)
        self._save_stamps(VERIFIED_PATH, self._verified)
        model_ok = getattr(self, "_model_ok", None)
        if model_ok is not None:
            model_ok.pop(fingerprint, None)
            self._save_stamps(MODEL_OK_PATH, model_ok)
        self._save_limited()
        return until

    def note_verified(self, fingerprint: str) -> None:
        """Remember an exit that answered 200, so a race can lead with it."""
        if not fingerprint:
            return
        self._verified[fingerprint] = time.time() + VERIFIED_TTL_S
        self._save_stamps(VERIFIED_PATH, self._verified)

    def note_model_ok(self, fingerprint: str) -> None:
        """Remember an exit that served a MODEL call, not just any 200.

        The far end serves its catalogue to every address, so a 200 on the
        models list proves the exit can talk, not that the exit may run a
        model. The free tier lets some addresses into the model endpoint and
        refuses the rest, and it refuses them the same way every time, so the
        only thing that moves the needle is leading with the exits that have
        already answered a model.
        """
        if not fingerprint:
            return
        model_ok = getattr(self, "_model_ok", None)
        if model_ok is None:
            return
        model_ok[fingerprint] = time.time() + MODEL_OK_TTL_S
        self._save_stamps(MODEL_OK_PATH, model_ok)

    def _save_score(self) -> None:
        """Best effort: a restart should not forget which countries worked."""
        self._write_json(SCORE_PATH, self._score)

    def _load_score(self) -> Dict[str, int]:
        """Integer running totals, so a restart keeps the ranking."""
        raw = self._read_json(SCORE_PATH)
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, int] = {}
        for cc, val in raw.items():
            try:
                out[str(cc)] = int(val)
            except (TypeError, ValueError):
                continue
        return out

    def _load_limited(self) -> Dict[str, Dict]:
        """The notepad from disk: ``{fingerprint: {until, reason}}``, fresh only."""
        raw = self._read_json(LIMITED_PATH)
        if not isinstance(raw, dict):
            return {}
        now = time.time()
        fresh: Dict[str, Dict] = {}
        for fingerprint, entry in raw.items():
            if isinstance(entry, dict):
                until = entry.get("until")
                reason = str(entry.get("reason") or "")
            else:
                until, reason = entry, ""
            try:
                until = float(until)
            except (TypeError, ValueError):
                continue
            if until > now:
                fresh[str(fingerprint)] = {"until": until, "reason": reason}
        return fresh

    def _load_stamps(self, name: str) -> Dict[str, float]:
        """``{fingerprint: valid_until}`` from disk, dropping expired entries."""
        raw = self._read_json(name)
        if not isinstance(raw, dict):
            return {}
        now = time.time()
        fresh: Dict[str, float] = {}
        for fp, until in raw.items():
            try:
                until = float(until)
            except (TypeError, ValueError):
                continue
            if until > now:
                fresh[str(fp)] = until
        return fresh

    def _read_json(self, name: str):
        """What ``name`` holds, or None when it is missing, unreadable or junk."""
        try:
            raw = (self.root / name).read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _write_json(self, name: str, payload) -> None:
        """Best effort: never let bookkeeping break a run."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(
                json.dumps(payload), encoding="utf-8")
        except OSError:
            pass

    def _save_stamps(self, name: str, stamps: Dict[str, float]) -> None:
        """Best effort: persist ``{fingerprint: valid_until}``."""
        self._write_json(name, stamps)

    def _save_limited(self) -> None:
        """Best effort: persist the notepad, dropping what has expired."""
        now = time.time()
        self._limited = {fp: rec for fp, rec in self._limited.items()
                         if exits.limited_until(rec) > now}
        self._write_json(LIMITED_PATH, self._limited)
