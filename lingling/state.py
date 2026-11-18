import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import settings

NAME = settings.STATE_NAME
LOCK = threading.RLock()


def path(folder):
    return Path(folder) / NAME


def today():
    return datetime.now(timezone.utc).date().isoformat()


def blank():
    return {"reset": today(), "spent": [], "served": {}, "last": None}


def load(folder):
    with LOCK:
        try:
            data = json.loads(path(folder).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}

        if not isinstance(data, dict):
            data = {}

        # then day
        if data.get("reset") != today():
            data = blank()

        if not isinstance(data.get("served"), dict):
            data["served"] = {}

        return data


def save(folder, data):
    with LOCK:
        item = path(folder)
        item.parent.mkdir(parents=True, exist_ok=True)
        tmp = item.with_suffix(settings.TMP_SUFFIX)
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, item)


def spend(folder, lane):
    with LOCK:
        data = load(folder)
        spent = data.setdefault("spent", [])

        if lane not in spent:
            spent.append(lane)

        save(folder, data)


def count(folder, lane):
    with LOCK:
        data = load(folder)
        served = data.setdefault("served", {})
        served[lane] = int(served.get(lane, 0)) + 1
        save(folder, data)
        return served[lane]


def remember(folder, lane):
    with LOCK:
        data = load(folder)
        data["last"] = lane
        save(folder, data)


def clear(folder):
    with LOCK:
        save(folder, blank())
