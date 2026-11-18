import os
import shutil
import sys
from pathlib import Path

from . import data_dir, profile, settings, state, tunnel
from .pool import Pool


def _c(text, code=""):
    if not code or os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def _intro():
    print()
    print(_c(settings.NAME + " setup", settings.SETUP_HEAD_COLOR))
    print()
    print(settings.NETWORK + ": " + _c(settings.ROUTE64_URL, settings.SETUP_URL_COLOR))
    print("Create a WireGuard tunnel and copy its config.")
    print("Copy the routed /" + str(settings.ROUTE_BITS) + " from List IP subnets.")
    print()


def _read_config(args):
    if "--config" in args:
        path = args[args.index("--config") + 1]
        return Path(path).expanduser().read_text(encoding="utf-8")

    print("Config path:")
    raw = input("> ").strip().strip('"')

    if raw and Path(raw).expanduser().is_file():
        return Path(raw).expanduser().read_text(encoding="utf-8")

    if raw and "[Interface]" in raw:
        return raw

    print()
    print(
        "Paste the WireGuard config. Finish with a line containing "
        + settings.SETUP_EOF + ":"
    )
    lines = []

    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == settings.SETUP_EOF:
            break
        lines.append(line)

    return "\n".join(lines) + "\n"


def _prefix(args):
    if "--prefix" in args:
        return profile.prefix_network(args[args.index("--prefix") + 1])

    print()
    print("Routed /" + str(settings.ROUTE_BITS) + " from List IP subnets:")
    print(
        "Not the config Address (the tunnel /"
        + str(settings.LANE_BITS) + ")."
    )
    raw = input("> ").strip()

    if not raw:
        raise ValueError("routed prefix is required")

    return profile.prefix_network(raw)


def _confirm(args):
    if "--yes" in args:
        return True

    print()
    answer = input("Install this profile? [Y/n] ").strip().lower()
    return answer in ("", "y", "yes")


def _reset(folder):
    data = profile.load(folder)

    if data:
        try:
            tunnel.uninstall(data["service"], folder)
        except Exception as exc:
            print("Could not remove the old tunnel: " + str(exc))

    folder = Path(folder)
    saved = state.load(folder) if state.path(folder).exists() else None
    folder.mkdir(parents=True, exist_ok=True)

    for item in folder.iterdir():
        if item.name in (settings.STATE_NAME, settings.SESSION_LOCK):
            continue
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            try:
                item.unlink()
            except OSError:
                pass

    if saved:
        state.save(folder, saved)


def run(folder, args=None):
    args = list(args or [])
    folder = Path(folder)
    old = profile.load(folder)

    if not sys.stdin.isatty() and not all(
        flag in args for flag in ("--config", "--prefix", "--yes")
    ):
        print(
            "setup needs a terminal, or pass "
            "--config <file> --prefix <subnet> --yes"
        )
        return False

    if "--reset" in args:
        _reset(folder)
        print("Old profile removed.")

    if profile.complete(folder) and "--reset" not in args:
        print(settings.NAME + " is already set up.")
        print("Run: " + settings.APP + " setup --reset")
        return True

    _intro()

    try:
        text = _read_config(args)
    except (OSError, IndexError) as exc:
        print("Could not read the config: " + str(exc))
        return False

    try:
        parsed = profile.parse(text)
    except ValueError as exc:
        print("Config is not valid: " + str(exc))
        return False

    print()
    print("Tunnel address  " + parsed["address"])
    print("Endpoint        " + parsed["endpoint"])

    try:
        prefix = _prefix(args)
    except (ValueError, IndexError) as exc:
        print("Prefix is not valid: " + str(exc))
        return False

    print("Rotating prefix " + prefix)
    print("Lanes           " + str(len(Pool(prefix).lanes)))

    if not _confirm(args):
        print("Nothing was changed.")
        return False

    profile.save(folder, text, prefix)

    try:
        tunnel.install(folder, profile.config_path(folder))
    except Exception as exc:
        profile.remove(folder)
        print()
        print("Tunnel install failed: " + str(exc))
        print("Details: " + str(Path(folder) / settings.INSTALL_LOG))
        return False

    # then reset
    if not old or old.get("prefix") != prefix:
        state.save(folder, state.blank())

    print()
    print(_c("Setup complete.", settings.SETUP_OK_COLOR))
    print(settings.NAME + " will start the tunnel when you open a session.")
    print("Run: " + settings.APP)
    return True


def command(args):
    ok = run(data_dir(), args)
    return 0 if ok else 1
