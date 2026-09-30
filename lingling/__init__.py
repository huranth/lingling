"""Lingling -- official OpenCode, but your requests ride rotating Tor lanes."""

import os
import tempfile
from pathlib import Path

__version__ = "2.1.20.post17"


def data_dir() -> Path:
    """Per-user runtime state, under the OS temp dir so the OS owns cleanup."""
    override = os.environ.get("LINGLING_DATA_DIR")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "lingling-data"
