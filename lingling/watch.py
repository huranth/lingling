import json
import os
import sqlite3
import threading
import time
from datetime import datetime

from . import settings

LIMIT = settings.FREE_LIMIT
PAUSE = settings.WATCH_PAUSE
STRIKE = settings.WATCH_STRIKE
SOFT = settings.WATCH_SOFT
DAY = settings.WATCH_DAY
GATE = settings.WATCH_GATE
LONG = settings.WATCH_LONG
CEIL = settings.WATCH_MAX


def clock(ms):
    try:
        return datetime.fromtimestamp(ms / settings.MS_SECOND).strftime(settings.CLOCK_FORMAT)
    except (OSError, OverflowError, TypeError, ValueError):
        return ""


def span(seconds):
    seconds = max(0, int(seconds))
    hour = settings.SECONDS_HOUR
    minute = settings.SECONDS_MINUTE

    if seconds >= hour:
        return str(seconds // hour) + "h " + str((seconds % hour) // minute) + "m"
    if seconds >= minute:
        return str(seconds // minute) + "m " + str(seconds % minute) + "s"
    return str(seconds) + "s"


def reset():
    day = settings.SECONDS_DAY
    now = time.time()
    return day - (now % day) + settings.RESET_SLACK


def size_of(data):
    tokens = data.get("tokens") or {}

    if not isinstance(tokens, dict):
        return 0

    cache = tokens.get("cache") or {}

    if not isinstance(cache, dict):
        cache = {}

    try:
        return int(tokens.get("input") or 0) + int(cache.get("read") or 0)
    except (TypeError, ValueError):
        return 0


def wait_of(data):
    error = data.get("error") or {}
    wait = error.get("retryAfterMs") if isinstance(error, dict) else 0

    if not wait:
        retry = data.get("retry")
        if isinstance(retry, dict):
            error = retry.get("error")
            if isinstance(error, dict):
                wait = error.get("retryAfterMs") or 0

    try:
        return max(0.0, float(wait)) / settings.MS_SECOND
    except (TypeError, ValueError):
        return 0.0


class Watcher:
    def __init__(self, root, lane, emit, rotate=None, spend=None,
                 count=None, served=None, switch_file=None,
                 recover_file=None, reset_file=None, reset=None):
        self.path = os.path.join(root, *settings.OPENCODE_DB)
        self.switch_file = str(switch_file) if switch_file else ""
        self.recover = str(recover_file) if recover_file else ""
        self.reset_file = str(reset_file) if reset_file else ""
        self.reset_route = reset
        self.lane = lane
        self.emit = emit
        self.rotate = rotate
        self.spend = spend
        self.count = count
        self.served = dict(served or {})
        self.tokens = {}
        self.walls = {}
        self.walled = set()
        self.stop_event = threading.Event()
        self.thread = None
        self.seen = {}
        self.lanes = {}
        self.refused = {}
        self.pending = None
        self.soft = None
        self.until = 0.0
        self.gates = 0
        self.history = [(int(time.time() * settings.MS_SECOND), lane)]
        self.since = 0
        self.updated = 0

    def _lane_at(self, start):
        lane = self.history[0][1]

        for at, name in self.history:
            if at > start:
                break
            lane = name

        return lane

    def start(self):
        self.since, self.updated = self._latest()

        if self.served.get(self.lane, 0) >= SOFT:
            self.soft = (self.lane, time.monotonic())

        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=settings.WATCH_JOIN)
        self.poll()

    def poll(self):
        self._reset_signal()
        self._signal()
        rows = self._rows()

        for row in rows:
            try:
                self._show(row)
            except Exception:
                if len(row) > 1:
                    self.seen[row[1]] = True

        if rows:
            self.since = max(self.since, max(row[0] for row in rows))
            self.updated = max(
                self.updated,
                max((row[4] or 0) for row in rows),
            )

        self._due()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.poll()
            except Exception:
                pass
            self.stop_event.wait(settings.WATCH_POLL)

    def _open(self):
        try:
            return sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, timeout=settings.WATCH_TIMEOUT
            )
        except sqlite3.Error:
            return None

    def _rows(self):
        con = self._open()

        if con is None:
            return []

        try:
            rows = con.execute(
                "select rowid, id, session_id, time_created, time_updated, data "
                "from session_message "
                "where type = 'assistant' "
                "and (rowid > ? or time_updated > ?) "
                "order by rowid",
                (self.since, self.updated),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            con.close()

        return rows

    def _latest(self):
        con = self._open()

        if con is None:
            return 0, 0

        try:
            row = con.execute(
                "select max(rowid), max(time_updated) from session_message"
            ).fetchone()
            return row[0] or 0, row[1] or 0
        except sqlite3.Error:
            return 0, 0
        finally:
            con.close()

    def _show(self, row):
        _, mid, _session, created, _, raw = row

        if self.seen.get(mid, False):
            return

        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            data = {}

        if not isinstance(data, dict):
            data = {}

        model = data.get("model") or {}
        if not isinstance(model, dict):
            model = {}
        times = data.get("time") or {}
        if not isinstance(times, dict):
            times = {}

        start = times.get("created") or created
        done = times.get("completed")
        name = model.get("id") or ""
        provider = model.get("providerID") or ""
        finish = data.get("finish") or ""
        paused = bool(self.until and time.monotonic() < self.until)

        if mid not in self.lanes:
            self.lanes[mid] = self._lane_at(start)
            if not paused:
                self.emit(
                    "request",
                    lane=self.lanes[mid],
                    clock=clock(start),
                    model=name,
                    provider=provider,
                )

        lane = self.lanes[mid]
        retry = data.get("retry") or {}
        if not isinstance(retry, dict):
            retry = {}
        retry_error = retry.get("error") or {}
        waiting = (
            isinstance(retry_error, dict)
            and retry_error.get("type") == LIMIT
        )
        wait = wait_of(data)

        if waiting and lane == self.lane:
            self._limit(mid, lane, wait, _session, str(retry_error.get("message") or ""))

        error = data.get("error") or {}
        limit = isinstance(error, dict) and error.get("type") == LIMIT

        if limit and lane == self.lane:
            self._limit(mid, lane, wait, _session, str(error.get("message") or ""))

        if not done or self.seen.get(mid):
            return

        if not paused:
            self.emit(
                "done",
                lane=lane,
                clock=clock(done),
                ms=max(0, int(done - start)),
                model=name,
            )
            self._done(lane, finish, _session, size_of(data))

        self.seen[mid] = True

    def _done(self, lane, finish, session="", size=0):
        if finish == "error":
            return

        if size and session:
            self.tokens[session] = size

        self.served[lane] = self.served.get(lane, 0) + 1

        if self.count:
            self.count(lane)

        self._clear(lane)

        if self.served[lane] >= DAY:
            if self.spend:
                self.spend(lane)
            self.soft = (lane, time.monotonic())
            return

        if (self.served[lane] >= SOFT and self.soft is None
                and self.pending is None and not self.until):
            self.soft = (lane, time.monotonic())

    def _limit(self, mid, lane, wait=0.0, session="", message=""):
        if lane != self.lane:
            return

        seen = self.refused.setdefault(lane, set())

        if mid in seen:
            return

        seen.add(mid)

        if len(seen) < STRIKE:
            return

        root = self._root(session) or session
        self._wall(lane, root, mid, wait, message)

    def _wall(self, lane, session, mid="", wait=0.0, message=""):
        if not self.recover or not session:
            self._hold(lane, wait)
            return

        if self.until and time.monotonic() < self.until:
            return

        if mid and mid in self.walled:
            return

        now = time.monotonic()
        last = self.walls.get(session, 0)

        if now - last < GATE:
            return

        if mid:
            self.walled.add(mid)

        if self.spend:
            try:
                self.spend(lane)
            except Exception:
                pass

        fresh = self._fresh(lane)

        if not fresh:
            self._hold(lane, wait or reset())
            return

        self.walls[session] = now
        self._offer(session, mid, message)

        self.emit("wall", lane=fresh, note=message)

    def _root(self, session):
        if not session:
            return ""

        con = self._open()

        if con is None:
            return session

        try:
            current = session

            for _ in range(settings.WATCH_DEPTH):
                row = con.execute(
                    "select parent_id from session_v2 where id = ?",
                    (current,),
                ).fetchone()
                parent = row[0] if row else None

                if not parent or parent == current:
                    return current

                current = str(parent)

            return current
        except sqlite3.Error:
            return session
        finally:
            con.close()

    def _last_size(self, session):
        con = self._open()

        if con is None:
            return 0

        try:
            rows = con.execute(
                "select data from session_message "
                "where session_id = ? and type = 'assistant' "
                "order by rowid desc limit ?",
                (session, settings.WATCH_RECENT),
            ).fetchall()
        except sqlite3.Error:
            return 0
        finally:
            con.close()

        for (raw,) in rows:
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue

            if isinstance(data, dict):
                size = size_of(data)
                if size:
                    return size

        return 0

    def _size(self, session):
        if not session:
            return 0

        size = self.tokens.get(session, 0)

        if size:
            return size

        return self._last_size(session)

    def _fresh(self, lane):
        if not self.rotate:
            return ""

        try:
            next_lane = self.rotate(lane)
        except Exception:
            return ""

        if not next_lane or next_lane == lane:
            return ""

        self.lane = next_lane
        self.history.append((int(time.time() * settings.MS_SECOND), next_lane))
        return next_lane

    def _offer(self, session, mid="", message=""):
        if not self.recover or not session:
            return

        item = {
            "sessionID": session,
            "messageID": mid,
            "size": self._size(session),
            "error": message,
        }
        tmp = self.recover + settings.TMP_SUFFIX

        try:
            with open(tmp, "w", encoding="utf-8") as file:
                json.dump(item, file)
            os.replace(tmp, self.recover)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _reset_signal(self):
        if not self.reset_file:
            return

        handled = self.reset_file + settings.HANDLED_SUFFIX

        try:
            os.replace(self.reset_file, handled)
        except OSError:
            return

        try:
            os.unlink(handled)
        except OSError:
            pass

        self._reset()

    def _reset(self):
        if self.reset_route:
            try:
                self.reset_route()
            except Exception:
                pass

        self.soft = None
        self.pending = None
        self.until = 0.0
        self.gates = 0
        self.served.clear()
        self.tokens.clear()
        self.walls.clear()
        self.walled.clear()
        self.refused.clear()

    def _hold(self, lane, wait=0.0):
        now = time.monotonic()

        if self.until and now < self.until:
            return

        if wait > LONG:
            seconds = wait
        elif wait > 0:
            seconds = max(wait, GATE)
        elif self.gates >= settings.WATCH_GATES:
            seconds = reset()
        else:
            seconds = GATE

        seconds = min(max(seconds, settings.WATCH_FLOOR), CEIL)
        self.gates += 1
        self.until = now + seconds
        self.pending = None
        self.soft = None
        self.emit("cool", lane=lane, note=span(seconds))

    def _signal(self):
        if not self.switch_file:
            return

        handled = self.switch_file + settings.HANDLED_SUFFIX

        try:
            os.replace(self.switch_file, handled)
        except OSError:
            return

        try:
            with open(handled, encoding="ascii") as file:
                text = file.read()
        except OSError:
            try:
                os.unlink(handled)
            except OSError:
                pass
            return

        try:
            os.unlink(handled)
        except OSError:
            pass

        if not text.strip():
            return

        wait = 0.0
        session = ""
        mid = ""
        message = ""
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            data = {}

        if isinstance(data, dict):
            try:
                wait = float(data.get("retryAfterMs") or 0) / settings.MS_SECOND
            except (TypeError, ValueError):
                wait = 0.0
            session = str(data.get("sessionID") or "")
            mid = str(data.get("messageID") or "")
            message = str(data.get("message") or "")

        if self.until and time.monotonic() < self.until:
            return
        if self.pending or self.soft:
            return

        root = self._root(session) or session
        self._wall(self.lane, root, mid, wait, message)

    def _due(self):
        now = time.monotonic()

        if self.until:
            if now < self.until:
                return
            self.until = 0.0
            self.gates = 0
            return

        if self.soft:
            lane, due = self.soft

            if now < due:
                return

            self.soft = None

            if lane == self.lane and self.rotate:
                self._move(lane, wait=False)
            return

        if not self.pending:
            return

        lane, due = self.pending

        if now < due:
            return

        self.pending = None

        if lane != self.lane or not self.rotate:
            return

        if self.spend:
            self.spend(lane)

        self._move(lane)

    def _move(self, old, wait=True):
        next_lane = self.rotate(old)

        if not next_lane or next_lane == old:
            if wait and old == self.lane:
                self._hold(old, reset())
            return

        self.lane = next_lane
        self.history.append((int(time.time() * settings.MS_SECOND), next_lane))

    def _clear(self, lane):
        if self.pending and self.pending[0] == lane:
            self.pending = None
        if self.soft and self.soft[0] == lane:
            self.soft = None

        self.refused.pop(lane, None)

        if lane == self.lane:
            self.gates = 0
