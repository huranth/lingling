import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import profile, proof, settings, state, update
from .pool import Pool
from .proxy import Proxy
from .tunnel import Tunnel
from .watch import Watcher

LOCK = threading.Lock()


def _emit(folder):
    path = Path(folder) / settings.ROUTE_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)

    def emit(kind, lane="", host="", port=0, ms=0, note="", **extra):
        item = {
            "clock": time.strftime(settings.CLOCK_FORMAT),
            "kind": kind,
            "lane": lane,
            "host": host,
            "port": port,
            "ms": ms,
            "note": note,
        }
        item.update(extra)

        with LOCK:
            with open(path, "a", encoding="utf-8") as file:
                file.write(json.dumps(item, separators=(",", ":")) + "\n")

    return emit


def _lane(folder, pool):
    data = state.load(folder)
    spent = data.get("spent") or []
    if not isinstance(spent, list):
        spent = []
    spent = set(spent)
    last = data.get("last")

    if last in pool.lanes:
        start = pool.lanes.index(last) + 1
    else:
        start = 0

    for step in range(len(pool.lanes)):
        lane = pool.lanes[(start + step) % len(pool.lanes)]
        if lane not in spent:
            return lane

    raise RuntimeError(
        "every /" + str(settings.LANE_BITS)
        + " is spent -- wait for " + settings.RESET_TEXT
    )


def _base(binary):
    if binary.lower().endswith((".cmd", ".bat")):
        return [settings.CMD, "/c", binary]
    return [binary]


def _command(binary, args):
    base = _base(binary)
    return base + [settings.OPENCODE_STANDALONE] + list(args)


def _plugin(root):
    # then copy
    target = Path(root).joinpath(*settings.OPENCODE_CONFIG_HOME, *settings.PLUGIN_DIR)
    source = Path(__file__).with_name(settings.PLUG_DIR)

    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()

    shutil.copytree(source, target)


def _env(root, proxy_url):
    env = os.environ.copy()
    root = Path(root)
    dirs = [root / name for name in settings.HOME_DIRS]

    for folder in dirs:
        folder.mkdir(parents=True, exist_ok=True)

    home = str(dirs[0])

    env.update({
        "HOME": home,
        "USERPROFILE": home,
        "XDG_CONFIG_HOME": str(dirs[1]),
        "XDG_DATA_HOME": str(dirs[2]),
        "XDG_CACHE_HOME": str(dirs[3]),
        "OPENCODE_CONFIG_DIR": str(
            root.joinpath(*settings.OPENCODE_CONFIG_HOME)
        ),
        "OPENCODE_CONFIG_CONTENT": settings.OPENCODE_CONFIG_CONTENT,
        "LINGLING_SWITCH_FILE": str(Path(root).parent / settings.SWITCH_NAME),
        "LINGLING_NAME": settings.NAME,
        "LINGLING_TOAST": str(settings.TOAST_MS),
        "LINGLING_FREE_LIMIT": settings.FREE_LIMIT,
        "LINGLING_RECOVER_FILE": str(Path(root).parent / settings.RECOVER_NAME),
        "LINGLING_RECOVER_POLL": str(settings.RECOVER_POLL),
        "LINGLING_RESCUE_WAIT": str(settings.RESCUE_WAIT),
        "LINGLING_RESCUE_POLL": str(settings.RESCUE_POLL),
        "LINGLING_RESCUE_CHECK": str(settings.RESCUE_CHECK),
        "LINGLING_RESCUE_SEEN": str(settings.RESCUE_SEEN),
        "LINGLING_RESCUE_TRIES": str(settings.RESCUE_TRIES),
        "LINGLING_TOKENS_K": str(settings.TOKENS_K),
        "LINGLING_CLAIMED_SUFFIX": settings.CLAIMED_SUFFIX,
        "LINGLING_FAIL_EVENTS": ",".join(settings.FAIL_EVENTS),
        "LINGLING_LIMIT_MARKERS": ",".join(settings.LIMIT_MARKERS),
        "LINGLING_FREE_SUFFIX": settings.FREE_SUFFIX,
        "LINGLING_PLUGIN_WALL": settings.PLUGIN_WALL,
        "LINGLING_PLUGIN_RESCUE": settings.PLUGIN_RESCUE,
        "HTTPS_PROXY": proxy_url,
        "HTTP_PROXY": proxy_url,
        "ALL_PROXY": proxy_url,
        "NO_PROXY": settings.NO_PROXY,
        "no_proxy": settings.NO_PROXY,
    })

    for key in settings.OPENCODE_DROP:
        env.pop(key, None)

    return env


def _stop(proc):
    try:
        proc.terminate()
        proc.wait(timeout=settings.OPENCODE_STOP)
    except Exception:
        pass


def _wait(proc, proof_proc):
    while True:
        code = proc.poll()
        if code is not None:
            return code

        if proof_proc is not None and proof_proc.poll() is not None:
            _stop(proc)
            return 0

        time.sleep(settings.SESSION_POLL)


def run(folder, args, opencode):
    data = profile.load(folder)

    if not data:
        print(settings.APP + ": no setup profile -- run: " + settings.APP + " setup")
        return 1

    pool = Pool(data["prefix"])
    lane = _lane(folder, pool)
    saved = state.load(folder)
    root = Path(folder) / settings.UI_DIR
    emit = _emit(folder)
    tunnel = Tunnel(folder, data["service"], data["prefix"])
    proxy = None
    watcher = None
    proof_proc = None
    log_path = Path(folder) / settings.ROUTE_LOG
    check = update.start(folder)

    try:
        tunnel.start()
        proxy = Proxy(lane, emit)
        proxy.start()

        def spend(old):
            state.spend(folder, old)

        def count(old):
            state.count(folder, old)

        def reset():
            profile.remove(folder)
            state.clear(folder)

        def rotate(old):
            if old != proxy.lane:
                return proxy.lane

            try:
                next_lane = _lane(folder, pool)
            except RuntimeError:
                return None

            if next_lane == old:
                return old

            proxy.switch(next_lane)
            state.remember(folder, next_lane)
            emit("switch", lane=next_lane, note=old)
            return next_lane

        switch_file = Path(folder) / settings.SWITCH_NAME
        recover_file = Path(folder) / settings.RECOVER_NAME
        reset_file = Path(folder) / settings.RESET_NAME
        stale = (
            switch_file,
            switch_file.with_name(switch_file.name + settings.HANDLED_SUFFIX),
            recover_file,
            recover_file.with_name(recover_file.name + settings.TMP_SUFFIX),
            recover_file.with_name(recover_file.name + settings.CLAIMED_SUFFIX),
            reset_file,
            reset_file.with_name(reset_file.name + settings.HANDLED_SUFFIX),
        )

        for item in stale:
            try:
                item.unlink()
            except OSError:
                pass

        watcher = Watcher(
            root, lane, emit, rotate, spend,
            count=count,
            served=saved.get("served", {}),
            switch_file=switch_file,
            recover_file=recover_file,
            reset_file=reset_file,
            reset=reset,
        )
        watcher.start()
        state.remember(folder, lane)
        log_path.touch(exist_ok=True)
        proof_proc = proof.spawn(log_path, log_path.stat().st_size)
        emit("start", lane=lane)

        update.emit(check)
        if check is not None:
            update.auto_install(folder, check.info)

        _plugin(root)
        env = _env(root, proxy.url)
        print(settings.APP + ": lane " + lane)
        print(settings.APP + ": starting OpenCode")

        cmd = _command(opencode, args)
        proc = subprocess.Popen(cmd, env=env)

        try:
            return _wait(proc, proof_proc)
        except KeyboardInterrupt:
            _stop(proc)
            return 130
    except Exception as exc:
        print(settings.APP + ": " + str(exc))
        return 1
    finally:
        try:
            if watcher:
                watcher.stop()
        except Exception:
            pass

        try:
            if proof_proc:
                proof_proc.terminate()
                proof_proc.wait(timeout=settings.PROOF_STOP)
        except Exception:
            pass

        try:
            if proxy:
                proxy.stop()
        except Exception:
            pass

        try:
            tunnel.stop()
        except Exception:
            pass
