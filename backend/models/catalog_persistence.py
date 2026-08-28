"""Atomic JSON read/write for the recycler's burn-state manifest.

The manifest survives restarts so the gateway's view of "this model is
broken" matches its real-world experience across boots. Writes are atomic
(temp file + fsync + ``os.replace``) so a crash mid-save can never leave a
half-written file that silently resets every model to healthy.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

_log = logging.getLogger("uvicorn.error")


class BurnStateStore:
    """Single-file JSON store for the catalog's burn manifest.

    Each model id has at most one record. Records for models that have
    fully recovered (not burned, zero failures, zero hits, not blacklisted)
    are dropped from disk so the file stays small and self-cleansing.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    # -- public API -------------------------------------------------------
    def load(self) -> Dict[str, Dict[str, Any]]:
        """Return ``{model_id: entry}`` from disk, or ``{}`` if missing/corrupt."""
        with self._lock:
            try:
                raw = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return {}
            except OSError as exc:
                # Corrupt file: never let a parse failure silently wipe
                # burn state. The next reconcile tick will rewrite from
                # current in-memory truth.
                _log.warning(
                    "burn-state: %s unreadable (%s) -- starting with empty manifest",
                    self.path, exc,
                )
                return {}
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                _log.warning(
                    "burn-state: %s unreadable (%s) -- starting with empty manifest",
                    self.path, exc,
                )
                return {}
            if not isinstance(data, dict):
                return {}
            return {k: v for k, v in data.items()
                    if isinstance(k, str) and isinstance(v, dict)}

    def save(self, state: Dict[str, Dict[str, Any]]) -> None:
        """Atomically persist ``{model_id: entry}`` after pruning clean entries."""
        pruned = {mid: entry for mid, entry in state.items()
                  if entry.get("burned") or entry.get("blacklisted")
                  or entry.get("blacklist_hits") or entry.get("consecutive_failures")}
        with self._lock:
            tmp_path: Optional[str] = None
            try:
                # NamedTemporaryFile (delete=False) gives a stable temp path on
                # the same filesystem so os.replace is atomic. The temp lives
                # next to the real file, avoiding cross-device rename failures.
                tmp_dir = str(self.path.parent) if str(self.path.parent) else "."
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=tmp_dir,
                    prefix=self.path.name + ".", suffix=".tmp",
                    delete=False,
                ) as fh:
                    tmp_path = fh.name
                    json.dump(pruned, fh, indent=1, sort_keys=True)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, self.path)
            except OSError as exc:
                # Persist failure must NEVER raise into request handling -- a
                # transient disk fault must not take the gateway down. Worst
                # case the next reconcile tick rewrites from in-memory truth.
                _log.warning("burn-state: persistence to %s failed (%s)", self.path, exc)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
