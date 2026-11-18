import os
import re
import shutil
import subprocess
from pathlib import Path


def _text(name, default):
    value = os.environ.get(name)
    return value if value else default


def _num(name, default, cast=float):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _tool(name, default):
    return _text(name, shutil.which(default) or default)


def _tuple(name, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


APP = _text("LINGLING_APP", "lingling")
NAME = _text("LINGLING_NAME", APP.title())
DATA_SUFFIX = _text("LINGLING_DATA_SUFFIX", "-data")
NETWORK = _text("LINGLING_NETWORK", "Route64")
PACKAGE = _text("LINGLING_PACKAGE", APP)
SERVICE_PREFIX = _text("LINGLING_SERVICE_PREFIX", "WireGuardTunnel$")
_raw_service = _text("LINGLING_SERVICE", APP)
SERVICE_NAME = re.sub(r"[^A-Za-z0-9_.-]", "", _raw_service) or APP
SERVICE = SERVICE_PREFIX + SERVICE_NAME
CONFIG_NAME = _text("LINGLING_CONFIG", SERVICE_NAME + ".conf")
PROFILE_NAME = _text("LINGLING_PROFILE", "profile.json")
PRIVATE_MODE = _num("LINGLING_PRIVATE_MODE", 0o600, int)
STATE_NAME = _text("LINGLING_STATE", "route-state.json")
ROUTE_LOG = _text("LINGLING_ROUTE_LOG", "route.log")
SWITCH_NAME = _text("LINGLING_SWITCH", "switch.request")
RECOVER_NAME = _text("LINGLING_RECOVER", "recover.request")
RESET_NAME = _text("LINGLING_RESET", "reset.request")
FREE_LIMIT = _text("LINGLING_FREE_LIMIT", "provider.free-limit")
TUNNEL_STATUS = _text("LINGLING_TUNNEL_STATUS", "tunnel.json")
TUNNEL_STOP = _text("LINGLING_TUNNEL_STOP", "tunnel.stop")
TUNNEL_LOG = _text("LINGLING_TUNNEL_LOG", "tunnel.log")
INSTALL_LOG = _text("LINGLING_INSTALL_LOG", "wireguard-install.log")
SESSION_LOCK = _text("LINGLING_SESSION_LOCK", "session.lock")
UI_DIR = _text("LINGLING_UI_DIR", "ui")
PLUG_DIR = _text("LINGLING_PLUG_DIR", "plug")
TMP_SUFFIX = _text("LINGLING_TMP_SUFFIX", ".tmp")
HANDLED_SUFFIX = _text("LINGLING_HANDLED_SUFFIX", ".handled")
CLAIMED_SUFFIX = _text("LINGLING_CLAIMED_SUFFIX", ".claimed")
OLD_SUFFIX = _text("LINGLING_OLD_SUFFIX", ".old-")
LANES_SCRIPT = _text("LINGLING_LANES_SCRIPT", "lanes.txt")
STOP_TEXT = _text("LINGLING_STOP_TEXT", "stop\n")
OK_TEXT = _text("LINGLING_OK_TEXT", "ok")
PROOF_TITLE = _text("LINGLING_PROOF_TITLE", NAME + " proof")
PROOF_SESSION_COLOR = _text("LINGLING_PROOF_SESSION_COLOR", "1;36")
PROOF_SWITCH_COLOR = _text("LINGLING_PROOF_SWITCH_COLOR", "33")
PROOF_LIMIT_COLOR = _text("LINGLING_PROOF_LIMIT_COLOR", "33")
PROOF_WAIT_COLOR = _text("LINGLING_PROOF_WAIT_COLOR", "2")
PROOF_FAIL_COLOR = _text("LINGLING_PROOF_FAIL_COLOR", "31")
SETUP_HEAD_COLOR = _text("LINGLING_SETUP_HEAD_COLOR", "1")
SETUP_URL_COLOR = _text("LINGLING_SETUP_URL_COLOR", "36")
SETUP_OK_COLOR = _text("LINGLING_SETUP_OK_COLOR", "32")
SETUP_EOF = _text("LINGLING_SETUP_EOF", "EOF")
RESET_TEXT = _text("LINGLING_RESET_LABEL", "00:00 UTC")
CACHE_NAME = _text("LINGLING_CACHE", "update-check.json")
UPDATE_LOG = _text("LINGLING_UPDATE_LOG", "update.log")
OPENCODE_DB = _tuple("LINGLING_OPENCODE_DB", ("data", "opencode", "opencode.db"))
OPENCODE_CONFIG_HOME = _tuple("LINGLING_OPENCODE_CONFIG", ("config", "opencode"))
PLUGIN_DIR = _tuple("LINGLING_PLUGIN_DIR", ("plugins", "lingling"))
TOAST_MS = _num("LINGLING_TOAST", 8000, int)
OPENCODE_CONFIG_CONTENT = _text("LINGLING_OPENCODE_CONFIG_CONTENT", "{}")
OPENCODE_STANDALONE = _text("LINGLING_OPENCODE_STANDALONE", "--standalone")
OPENCODE_DROP = _tuple(
    "LINGLING_OPENCODE_DROP",
    ("OPENCODE_API_KEY", "OPENCODE_DISABLE_FREE_MODELS"),
)
HOME_DIRS = _tuple("LINGLING_HOME_DIRS", ("home", "config", "data", "cache"))
PLUGIN_WALL = _text("LINGLING_PLUGIN_WALL", "lingling.wall")
PLUGIN_RESCUE = _text("LINGLING_PLUGIN_RESCUE", "lingling.rescue")
FAIL_EVENTS = _tuple(
    "LINGLING_FAIL_EVENTS",
    ("session.step.failed", "session.execution.failed"),
)
LIMIT_MARKERS = _tuple(
    "LINGLING_LIMIT_MARKERS",
    ("free usage limit", "free-limit", "freeusagelimiterror"),
)
FREE_SUFFIX = _text("LINGLING_FREE_SUFFIX", "-free")
OPENCODE = _text("LINGLING_OPENCODE", "opencode")
OPENCODE_URL = _text("LINGLING_OPENCODE_URL", "https://opencode.ai")
PROXY_HOST = _text("LINGLING_PROXY_HOST", "127.0.0.1")
NO_PROXY = _text("LINGLING_NO_PROXY", "localhost,127.0.0.1,::1")
ROUTE64_URL = _text("LINGLING_ROUTE64_URL", "https://manager.route64.org")
WIREGUARD_URL = _text(
    "LINGLING_WIREGUARD_URL",
    "https://download.wireguard.com/windows-client/wireguard-installer.exe",
)
WIREGUARD_DOCS = _text("LINGLING_WIREGUARD_DOCS", "https://www.wireguard.com/install/")
WIREGUARD_ID = _text("LINGLING_WIREGUARD_ID", "WireGuard.WireGuard")
WIREGUARD_PATH = _text("LINGLING_WIREGUARD", "")
WIREGUARD_NAMES = _tuple("LINGLING_WIREGUARD_NAMES", ("wireguard.exe", "wireguard"))
WIREGUARD_DIR = _text("LINGLING_WIREGUARD_DIR", "WireGuard")
WIREGUARD_EXE = _text("LINGLING_WIREGUARD_EXE", "wireguard.exe")
MANIFEST_URL = _text(
    "LINGLING_UPDATE_URL", "https://lingling-update.vercel.app/api/latest"
)
PYPI_URL = _text("LINGLING_PYPI_URL", f"https://pypi.org/pypi/{PACKAGE}/json")
UPDATE_SCHEMA = _num("LINGLING_UPDATE_SCHEMA", 1, int)
REGISTRY_NAME = _text("LINGLING_REGISTRY_NAME", "PyPI")
REGISTRY_SOURCE = _text("LINGLING_REGISTRY_SOURCE", "pypi")
PIP = _text("LINGLING_PIP", "pip")
PIPX_LABEL = _text("LINGLING_PIPX_LABEL", "pipx")
UV_LABEL = _text("LINGLING_UV_LABEL", "uv")
MAX_LANES = _num("LINGLING_MAX_LANES", 65_536, int)
HOST_OFFSET = _num("LINGLING_HOST_OFFSET", 2, int)
ROUTE_BITS = _num("LINGLING_ROUTE_BITS", 56, int)
LANE_BITS = _num("LINGLING_LANE_BITS", 64, int)
PROXY_MAX_HEAD = _num("LINGLING_PROXY_MAX_HEAD", 64 * 1024, int)
PROXY_BACKLOG = _num("LINGLING_PROXY_BACKLOG", 32, int)
PROXY_CHUNK = _num("LINGLING_PROXY_CHUNK", 64 * 1024, int)
PROXY_HEAD_CHUNK = _num("LINGLING_PROXY_HEAD_CHUNK", 4096, int)
PROXY_SELECT = _num("LINGLING_PROXY_SELECT", 1.0)
MS_SECOND = _num("LINGLING_MS_SECOND", 1000, int)
TOKENS_K = _num("LINGLING_TOKENS_K", 1000, int)
SECONDS_MINUTE = _num("LINGLING_SECONDS_MINUTE", 60, int)
SECONDS_HOUR = _num("LINGLING_SECONDS_HOUR", 3600, int)
SECONDS_DAY = _num("LINGLING_SECONDS_DAY", 86400, int)
RESET_SLACK = _num("LINGLING_RESET_SLACK", 5, int)
RECOVER_POLL = _num("LINGLING_RECOVER_POLL", 1.0)
WATCH_PAUSE = _num("LINGLING_WATCH_PAUSE", 0.5)
WATCH_RECENT = _num("LINGLING_WATCH_RECENT", 12, int)
WATCH_DEPTH = _num("LINGLING_WATCH_DEPTH", 16, int)
WATCH_STRIKE = _num("LINGLING_WATCH_STRIKE", 1, int)
WATCH_SOFT = _num("LINGLING_WATCH_SOFT", 100, int)
WATCH_DAY = _num("LINGLING_WATCH_DAY", 200, int)
WATCH_GATE = _num("LINGLING_WATCH_GATE", 300.0)
WATCH_LONG = _num("LINGLING_WATCH_LONG", 3600.0)
WATCH_MAX = _num("LINGLING_WATCH_MAX", 86400 + 300, float)
WATCH_POLL = _num("LINGLING_WATCH_POLL", 0.5)
WATCH_FLOOR = _num("LINGLING_WATCH_FLOOR", 1.0)
WATCH_GATES = _num("LINGLING_WATCH_GATES", 2, int)
RESCUE_WAIT = _num("LINGLING_RESCUE_WAIT", 300.0)
RESCUE_POLL = _num("LINGLING_RESCUE_POLL", 0.5)
RESCUE_CHECK = _num("LINGLING_RESCUE_CHECK", 5.0)
RESCUE_SEEN = _num("LINGLING_RESCUE_SEEN", 64, int)
RESCUE_TRIES = _num("LINGLING_RESCUE_TRIES", 4, int)
WATCH_TIMEOUT = _num("LINGLING_WATCH_TIMEOUT", 1.0)
WATCH_JOIN = _num("LINGLING_WATCH_JOIN", 2.0)
LOCK_TIMEOUT = _num("LINGLING_LOCK_TIMEOUT", 5.0)
TUNNEL_READY = _num("LINGLING_TUNNEL_READY", 60.0)
TUNNEL_STOP_WAIT = _num("LINGLING_TUNNEL_STOP_WAIT", 15.0)
TUNNEL_POLL = _num("LINGLING_TUNNEL_POLL", 0.25)
SERVICE_START = _num("LINGLING_SERVICE_START", 25.0)
SERVICE_STOP = _num("LINGLING_SERVICE_STOP", 25.0)
SERVICE_SETTLE = _num("LINGLING_SERVICE_SETTLE", 1.0)
SERVICE_LOOKUP = _num("LINGLING_SERVICE_LOOKUP", 20, int)
SERVICE_LOOKUP_MS = _num("LINGLING_SERVICE_LOOKUP_MS", 500, int)
WIREGUARD_WAIT = _num("LINGLING_WIREGUARD_WAIT", 30.0)
WINGET_WAIT = _num("LINGLING_WINGET_WAIT", 15.0)
WINGET_TIMEOUT = _num("LINGLING_WINGET_TIMEOUT", 300.0)
ELEVATE_TIMEOUT = _num("LINGLING_ELEVATE_TIMEOUT", 180.0)
SPAWN_TIMEOUT = _num("LINGLING_SPAWN_TIMEOUT", 90.0)
OPENCODE_STOP = _num("LINGLING_OPENCODE_STOP", 5.0)
PROOF_STOP = _num("LINGLING_PROOF_STOP", 3.0)
UPDATE_CACHE_TTL = _num("LINGLING_UPDATE_CACHE_TTL", 6 * 3600)
UPDATE_UP_TTL = _num("LINGLING_UPDATE_UP_TTL", 15 * 60)
UPDATE_FAIL_TTL = _num("LINGLING_UPDATE_FAIL_TTL", 3600)
UPDATE_TIMEOUT = _num("LINGLING_UPDATE_TIMEOUT", 3.0)
UPDATE_BUDGET = _num("LINGLING_UPDATE_BUDGET", 8.0)
UPDATE_MIN_TIMEOUT = _num("LINGLING_UPDATE_MIN_TIMEOUT", 0.5)
HELPER_WAIT = _num("LINGLING_HELPER_WAIT", 24 * 3600)
HELPER_POLL = _num("LINGLING_HELPER_POLL", 2.0)
HELPER_MAX = _num("LINGLING_HELPER_MAX", 7 * 86400)
INSTALL_TIMEOUT = _num("LINGLING_INSTALL_TIMEOUT", 600.0)
PROOF_POLL = _num("LINGLING_PROOF_POLL", 0.3)
SESSION_POLL = _num("LINGLING_SESSION_POLL", 0.3)
CLOCK_FORMAT = _text("LINGLING_CLOCK_FORMAT", "%H:%M:%S")
LOG_FORMAT = _text("LINGLING_LOG_FORMAT", "%Y-%m-%d %H:%M:%S")
SETUP_SCRIPT = _text("LINGLING_SETUP_SCRIPT", "wireguard-setup.ps1")
INSTALL_RESULT = _text("LINGLING_INSTALL_RESULT", "wireguard-install-result.txt")
WIREGUARD_RESULT = _text("LINGLING_WIREGUARD_RESULT", "wireguard-result.txt")
WIREGUARD_INSTALLER = _text("LINGLING_WIREGUARD_INSTALLER", "wireguard-installer.exe")
PROCESS_QUERY = _num("LINGLING_PROCESS_QUERY", 0x00100000, int)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
PROGRAM_FILES = Path(
    _text(
        "LINGLING_PROGRAM_FILES",
        os.environ.get("ProgramFiles") or r"C:\Program Files",
    )
)
POWERSHELL = _tool("LINGLING_POWERSHELL", "powershell.exe")
SC = _tool("LINGLING_SC", "sc.exe")
NETSH = _tool("LINGLING_NETSH", "netsh.exe")
TASKLIST = _tool("LINGLING_TASKLIST", "tasklist.exe")
WINGET = _tool("LINGLING_WINGET", "winget")
CMD = _text("LINGLING_CMD", os.environ.get("COMSPEC") or "cmd.exe")
