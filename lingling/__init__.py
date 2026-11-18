import os
import tempfile
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import settings


def _source_version():
    path = Path(__file__).resolve().parents[1] / "pyproject.toml"

    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))["project"]["version"]
    except Exception:
        return "0+unknown"


try:
    __version__ = version(settings.PACKAGE)
except PackageNotFoundError:
    __version__ = _source_version()


def data_dir() -> Path:
    # env override
    override = os.environ.get("LINGLING_DATA_DIR")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / (settings.APP + settings.DATA_SUFFIX)
