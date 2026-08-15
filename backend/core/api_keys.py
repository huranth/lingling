"""User API keys for authenticating OpenAI-compatible clients.

Random ``ll_<32 hex>`` tokens in a JSON file, presented as
``Authorization: Bearer ll_...`` or ``x-api-key``. A local single-user store,
so tokens are plaintext -- the file is the trust boundary.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import config

_LOCK = threading.Lock()

# How stale ``last_used_at`` may get before validate() rewrites the file, so a
# disk write stays off the hot path of every authenticated request.
_LAST_USED_RESOLUTION_S = 60


def _load() -> List[Dict[str, Any]]:
    path = Path(config.API_KEYS_FILE)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save(keys: List[Dict[str, Any]]) -> None:
    """Persist the keyring atomically.

    ``_load`` treats unparseable JSON as "no keys", so a half-written file would
    silently revoke every client. Write-to-temp-then-rename means readers only
    ever see the complete old file or the new one.
    """
    path = Path(config.API_KEYS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(keys, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)  # atomic on POSIX and Windows (same directory)


def _generate_token() -> str:
    return "ll_" + secrets.token_hex(16)


def create_key(label: str = "") -> Dict[str, Any]:
    """Create and persist a key. Returns the full record (plaintext token)."""
    with _LOCK:
        keys = _load()
        key = {
            "id": "key_" + secrets.token_hex(4),
            "token": _generate_token(),
            "label": label.strip()[:80],
            "created_at": int(time.time()),
            "last_used_at": None,
        }
        keys.append(key)
        _save(keys)
        return key


def list_keys() -> List[Dict[str, Any]]:
    """All keys, secret-free (token masked to its last 4 chars)."""
    with _LOCK:
        out = []
        for k in _load():
            token = k.get("token", "")
            masked = ("..." + token[-4:]) if len(token) >= 4 else ""
            out.append({
                "id": k["id"],
                "label": k.get("label", ""),
                "token_masked": masked,
                "created_at": k.get("created_at"),
                "last_used_at": k.get("last_used_at"),
            })
        return out


def revoke_key(key_id: str) -> bool:
    with _LOCK:
        keys = _load()
        new_keys = [k for k in keys if k["id"] != key_id]
        if len(new_keys) == len(keys):
            return False
        _save(new_keys)
        return True


def validate(token: Optional[str]) -> bool:
    """Return True if ``token`` matches a stored key.

    ``last_used_at`` is refreshed at most once per ``_LAST_USED_RESOLUTION_S`` to
    keep a full keyring rewrite off the hot path of every authenticated request.
    """
    if not token:
        return False
    # Accept both raw ``ll_...`` and a ``Bearer ll_...`` value.
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    with _LOCK:
        keys = _load()
        for k in keys:
            if k.get("token") == token:
                now = int(time.time())
                last = k.get("last_used_at") or 0
                if now - int(last) >= _LAST_USED_RESOLUTION_S:
                    k["last_used_at"] = now
                    _save(keys)
                return True
        return False

