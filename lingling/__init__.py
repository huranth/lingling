"""Lingling -- official OpenCode, but your requests ride rotating Tor lanes."""

import os
import sys
from pathlib import Path

__version__ = "2.0.1"


def data_dir() -> Path:
    """Per-user runtime state (tor, lanes, proof log). Lives outside the
    package -- a pip install must never write into site-packages."""
    override = os.environ.get("LINGLING_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "lingling"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "lingling"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "lingling"
