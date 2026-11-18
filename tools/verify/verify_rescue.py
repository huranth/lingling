"""Rescue checks."""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lingling import settings
from lingling.watch import Watcher

FAILS = []
FOLDERS = []
CONS = []


def check(name, ok, detail=""):
    mark = "  [PASS] " if ok else "  [FAIL] "
    line = mark + name + (("   " + detail) if detail else "")
    print(line)
    if not ok:
        FAILS.append(name)


def make(lane, served=None, tokens=None):
    root = tempfile.mkdtemp(prefix="ll-rescue-")
    FOLDERS.append(root)
    db = os.path.join(root, "data", "opencode", "opencode.db")
    os.makedirs(os.path.dirname(db))
    con = sqlite3.connect(db)
    CONS.append(con)
    con.execute(
        "create table session_message ("
        "id text primary key, session_id text, type text, "
        "time_created integer, time_updated integer, data text)"
    )
    con.execute(
        "create table session_v2 (id text primary key, parent_id text)"
    )
    con.commit()
    events, rotated, spent = [], [], []
    recover = os.path.join(root, settings.RECOVER_NAME)

    def emit(kind, **kw):
        events.append(kind)

    def rotate(old):
        rotated.append(old)
        return old + "n"

    def spend(old):
        spent.append(old)

    watcher = Watcher(
        root, lane, emit, rotate, spend,
        served=served or {},
        switch_file=os.path.join(root, settings.SWITCH_NAME),
        recover_file=recover,
    )
    watcher.tokens.update(tokens or {})
    watcher.since = 0
    watcher.updated = 0
    return con, watcher, events, rotated, spent, recover


def row(con, mid, session, created, data):
    con.execute(
        "insert into session_message "
        "(id, session_id, type, time_created, time_updated, data) "
        "values (?, ?, 'assistant', ?, ?, ?)",
        (mid, session, created, created + 1, json.dumps(data)),
    )
    con.commit()


def success(mid, created, tokens):
    return {
        "model": {"id": "m", "providerID": "p"},
        "time": {"created": created, "completed": created + 100},
        "finish": "stop",
        "content": [{"type": "text", "text": "ok"}],
        "tokens": tokens,
    }


def limited(mid, created, wait=32201000):
    return {
        "model": {"id": "m", "providerID": "p"},
        "time": {"created": created, "completed": created + 100},
        "finish": "error",
        "error": {
            "type": settings.FREE_LIMIT,
            "retryAfterMs": wait,
            "message": "Free usage limit reached.",
        },
    }


def main():
    now = int(time.time() * settings.MS_SECOND)

    print("=== free limit offers a rescue ===")
    con, w, ev, rot, spent, file = make("a600")
    row(con, "big-ok", "s-big", now, success("big-ok", now, {
        "input": 50000, "cache": {"read": 100000}}))
    w.poll()
    row(con, "big-429", "s-big", now + 10, limited("big-429", now + 10))
    w.poll()
    check("wall is reported", "wall" in ev, str(ev))
    check("rescue file exists", os.path.isfile(file))
    item = json.loads(Path(file).read_text("utf-8"))
    check("rescue names the session", item["sessionID"] == "s-big")
    check("rescue carries the error", item["error"] == "Free usage limit reached.", str(item))
    check("wall takes fresh", rot == ["a600"], str(rot))
    check("wall uses fresh", w.lane == "a600n", w.lane)
    check("wall spends the lane", spent == ["a600"], str(spent))
    check("wall does not pause", w.until == 0, str(w.until))
    check("no text dump", "transcript" not in item)

    print("=== child wall uses root ===")
    con, w, ev, rot, spent, file = make("a607")
    con.execute("insert into session_v2 (id, parent_id) values ('s-root', null)")
    con.execute("insert into session_v2 (id, parent_id) values ('s-child', 's-root')")
    con.commit()
    row(con, "root-done", "s-root", now, success(
        "root-done", now, {"input": 50000, "cache": {"read": 100000}}))
    row(con, "child-429", "s-child", now + 10, limited("child-429", now + 10))
    w.poll()
    item = json.loads(Path(file).read_text("utf-8"))
    check("child uses root", item["sessionID"] == "s-root", str(item["sessionID"]))
    check("root size kept", item["size"] == 150000, str(item["size"]))
    check("child takes fresh", rot == ["a607"], str(rot))
    check("child wall spends", spent == ["a607"], str(spent))
    check("no text dump", "transcript" not in item)

    row(con, "child-2", "s-child", now + 20, limited("child-2", now + 20))
    w.poll()
    item = json.loads(Path(file).read_text("utf-8"))
    check("same wall stays", item["messageID"] == "child-429", str(item["messageID"]))
    check("one offer only", ev.count("wall") == 1, str(ev))

    print("=== free wall starts a rescue ===")
    con, w, ev, rot, spent, file = make("a601")
    row(con, "q-429", "s-q", now, limited("q-429", now))
    w.poll()
    check("free wall triggers", "wall" in ev, str(ev))
    check("free wall has a rescue", os.path.isfile(file))
    check("free wall rotates", rot == ["a601"], str(rot))
    check("free wall spends", spent == ["a601"], str(spent))
    check("free wall does not wait", w.until == 0, str(w.until))

    print("=== soft cap moves without spending ===")
    con, w, ev, rot, spent, file = make("a602", served={"a602": 99})
    row(con, "soft-ok", "s-soft", now, success("soft-ok", now, {"input": 1000}))
    w.poll()
    check("soft moves", rot == ["a602"], str(rot))
    check("soft keeps the lane", spent == [], str(spent))

    print("=== day cap retires ===")
    con, w, ev, rot, spent, file = make("a603", served={"a603": 199})
    row(con, "day-ok", "s-day", now, success("day-ok", now, {"input": 1000}))
    w.poll()
    check("day spends", spent == ["a603"], str(spent))
    check("day moves", rot == ["a603"], str(rot))

    print("=== used lane moves on a 429 ===")
    con, w, ev, rot, spent, file = make("a604", served={"a604": 100})
    row(con, "wall-429", "s-wall", now, limited("wall-429", now))
    w.poll()
    time.sleep(0.6)
    w.poll()
    check("wall spends", spent == ["a604"], str(spent))
    check("wall moves", rot == ["a604"], str(rot))

    print("=== wall signal starts a rescue ===")
    con, w, ev, rot, spent, file = make("a605", served={"a605": 1}, tokens={"s-sig": 150000})
    Path(w.switch_file).write_text(json.dumps({
        "retryAfterMs": 32201000,
        "sessionID": "s-sig",
        "message": "Free usage limit reached.",
    }), encoding="ascii")
    w.poll()
    check("signal reports wall", "wall" in ev, str(ev))
    check("signal writes a rescue", os.path.isfile(file))
    check("signal takes fresh", rot == ["a605"], str(rot))
    check("signal spends", spent == ["a605"], str(spent))

    print("=== free wall signal does not wait ===")
    con, w, ev, rot, spent, file = make("a606")
    Path(w.switch_file).write_text(json.dumps({
        "retryAfterMs": 32201000,
        "sessionID": "s-q2",
        "message": "Free usage limit reached.",
    }), encoding="ascii")
    w.poll()
    check("free signal reports wall", "wall" in ev, str(ev))
    check("free signal has a rescue", os.path.isfile(file))
    check("free signal moves", rot == ["a606"], str(rot))
    check("free signal spends", spent == ["a606"], str(spent))
    check("free signal does not wait", w.until == 0, str(w.until))

    print("=== rescue reset clears state ===")
    con, w, ev, rot, spent, file = make("a608", served={"a608": 50})
    w.reset_file = os.path.join(os.path.dirname(file), settings.RESET_NAME)
    w.reset_route = lambda: spent.append("reset")
    Path(w.reset_file).write_text("{}", encoding="ascii")
    w.poll()
    check("reset runs", spent == ["reset"], str(spent))
    check("reset clears served", w.served == {}, str(w.served))
    check("reset clears windows", w.walls == {} and w.soft is None, str(w.walls))
    check("reset file gone", not os.path.exists(w.reset_file))

    print()
    if FAILS:
        print("failed: " + ", ".join(FAILS))
        return 1

    print("rescue checks passed")
    return 0


if __name__ == "__main__":
    code = 1

    try:
        code = main()
    finally:
        for con in CONS:
            con.close()
        for root in FOLDERS:
            shutil.rmtree(root, ignore_errors=True)

    raise SystemExit(code)
