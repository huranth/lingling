import shutil
import sys

from . import (
    __version__,
    data_dir,
    lock,
    profile,
    route,
    setup,
    settings,
    tunnel,
    update,
)

HELP = f"""{settings.APP} -- OpenCode over local {settings.NETWORK} tunnels

    {settings.APP}                 start a session
    {settings.APP} setup           set up or replace your {settings.NETWORK} profile
    {settings.APP} setup --reset   remove the old profile and run setup again
    {settings.APP} update          install the newest {settings.APP}
    {settings.APP} uninstall       remove the profile and tunnel

The first launch runs setup automatically.
"""


def _busy(other):
    running = f" (pid {other})" if other > 0 else ""
    print(settings.APP + " is already running" + running)
    return 1


def _uninstall(args):
    folder = data_dir()
    other = lock.claim(folder)

    if other:
        return _busy(other)

    try:
        return _remove(folder, args)
    finally:
        lock.release(folder)


def _remove(folder, args):
    data = profile.load(folder)

    if "--yes" not in args:
        if not sys.stdin.isatty():
            print("refusing to uninstall without --yes")
            return 1

        answer = input("Remove the " + settings.NAME + " profile and tunnel? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("cancelled")
            return 0

    if data:
        try:
            tunnel.uninstall(data["service"], folder)
        except Exception as exc:
            print("Could not remove the tunnel: " + str(exc))

    shutil.rmtree(folder, ignore_errors=True)

    print(settings.NAME + " profile removed.")
    print("Remove the package with: " + settings.PIP + " uninstall " + settings.PACKAGE)
    return 0


def _setup(args):
    folder = data_dir()
    other = lock.claim(folder)

    if other:
        return _busy(other)

    try:
        return setup.command(args)
    finally:
        lock.release(folder)


def main(argv):
    args = list(argv)

    if args and args[0] in ("setup", "--setup"):
        return _setup(args[1:])
    if args and args[0] in ("update", "--update"):
        return update.update_command(args[1:])
    if args and args[0] in ("uninstall", "--uninstall"):
        return _uninstall(args[1:])
    if args and args[0] in ("--version", "-v"):
        print(settings.APP + " " + __version__)
        update.cached_hint(data_dir())
        return 0
    if args and args[0] in ("--help", "-h"):
        print(HELP)
        return 0

    opencode = shutil.which(settings.OPENCODE)
    if opencode is None:
        print(
            settings.APP + ": couldn't find `"
            + settings.OPENCODE + "` on your PATH."
        )
        print("Install it first: " + settings.OPENCODE_URL)
        return 1

    folder = data_dir()
    other = lock.claim(folder)

    if other:
        return _busy(other)

    try:
        if not profile.complete(folder):
            if not setup.run(folder):
                return 1

        return route.run(folder, args, opencode)
    finally:
        lock.release(folder)


def entry():
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print(settings.APP + ": closed")
        sys.exit(130)
