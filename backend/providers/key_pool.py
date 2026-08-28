"""Generic API-key pool with round-robin rotation and exponential cooldown.

Every provider Lingling aggregates owns one of these pools. Keys (API keys or
auth tokens) are tried round-robin; a key that hits a rate limit (HTTP 429) or
an auth failure (401/403) is put into an exponentially growing cooldown so load
spreads across the pool and a single exhausted key never blocks the gateway. A
successful request resets the key. This mirrors the rotation policy proven in
OmniRoute's ``OpencodeExecutor`` and is the basis of the per-provider key router.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core import config


@dataclass
class Key:
    """A single credential. The secret is never exposed through the API."""

    id: str
    secret: str
    label: str = ""
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    total_requests: int = 0
    total_429: int = 0

    def in_cooldown(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) < self.cooldown_until

    def cooldown_remaining(self, now: Optional[float] = None) -> float:
        return max(0.0, self.cooldown_until - (now or time.time()))

    def status(self) -> Dict[str, Any]:
        """A secret-free view, safe to return from the API."""
        return {
            "id": self.id,
            "label": self.label or self.id,
            "in_cooldown": self.in_cooldown(),
            "cooldown_remaining_s": round(self.cooldown_remaining(), 1),
            "consecutive_failures": self.consecutive_failures,
            "total_requests": self.total_requests,
            "total_429": self.total_429,
        }


class KeyPool:
    """Round-robin credential pool with exponential cooldown backoff."""

    def __init__(self, keys: Optional[List[Key]] = None) -> None:
        self.keys: List[Key] = keys or []
        self._cursor = 0
        self._lock = threading.Lock()
        # Monotonic, so remove-then-add cannot reuse a live id (deriving from
        # len(keys) did: removing key-2 of three left ['key-1','key-3'] and the
        # next add() was 'key-3' again, and remove() acts on the first match).
        self._next_id = len(self.keys) + 1

    def _fresh_id(self) -> str:
        """A pool-unique generated id, immune to removals."""
        while True:
            candidate = f"key-{self._next_id}"
            self._next_id += 1
            if all(k.id != candidate for k in self.keys):
                return candidate

    @classmethod
    def from_list(cls, items: Optional[List[Any]]) -> "KeyPool":
        """Build a pool from a list of strings or dicts.

        Accepted dict keys for the secret: ``secret``, ``api_key``, ``key``,
        ``token``. Optional ``id`` and ``label``.
        """
        keys: List[Key] = []
        for i, item in enumerate(items or []):
            if isinstance(item, str):
                item = {"secret": item}
            if not isinstance(item, dict):
                continue
            secret = (
                item.get("secret")
                or item.get("api_key")
                or item.get("key")
                or item.get("token")
            )
            if not secret:
                continue
            keys.append(
                Key(
                    id=item.get("id") or f"key-{i + 1}",
                    secret=secret,
                    label=item.get("label", ""),
                )
            )
        return cls(keys)

    # -- mutation ----------------------------------------------------------
    def add(self, secret: str, label: str = "", key_id: str = "") -> Key:
        with self._lock:
            key = Key(
                id=key_id or self._fresh_id(),
                secret=secret,
                label=label,
            )
            self.keys.append(key)
            return key

    def remove(self, key_id: str) -> bool:
        with self._lock:
            for i, key in enumerate(self.keys):
                if key.id == key_id:
                    self.keys.pop(i)
                    return True
            return False

    # -- selection ---------------------------------------------------------
    def pick(self) -> Optional[Key]:
        """Pick the next available key, skipping ones in cooldown.

        Returns ``None`` only when the pool is empty. If every key is in
        cooldown, the one closest to becoming available is returned so callers
        can decide whether to wait or fail fast.
        """
        with self._lock:
            if not self.keys:
                return None
            now = time.time()
            n = len(self.keys)
            for _ in range(n):
                key = self.keys[self._cursor % n]
                self._cursor += 1
                if not key.in_cooldown(now):
                    return key
            return min(self.keys, key=lambda k: k.cooldown_until)

    # -- feedback ----------------------------------------------------------
    def mark_success(self, key: Key) -> None:
        with self._lock:
            key.consecutive_failures = 0
            key.cooldown_until = 0.0
            key.total_requests += 1

    def mark_failure(self, key: Key, status_code: int) -> float:
        """Apply cooldown backoff for rate-limit / auth / server failures.

        Returns the cooldown duration (seconds) that was applied.
        """
        with self._lock:
            key.total_requests += 1
            if status_code == 429:
                key.total_429 += 1
            # Shared source of truth with executor._RETRYABLE and ProxyPool,
            # so a status that retried on a fresh key ALSO records a key
            # cooldown -- otherwise (426/409/410/428/504) a transient bad key
            # stayed hot and consumed the next request before it cooled.
            if status_code not in config.RECONCILED_FAILURE_STATUSES:
                return 0.0
            key.consecutive_failures += 1
            base_s = config.COOLDOWN_BASE_MS / 1000.0
            max_s = config.COOLDOWN_MAX_MS / 1000.0
            delay = min(max_s, base_s * (2 ** (key.consecutive_failures - 1)))
            key.cooldown_until = time.time() + delay
            return delay

    # -- introspection -----------------------------------------------------
    def status(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            available = sum(1 for k in self.keys if not k.in_cooldown(now))
            return {
                "total": len(self.keys),
                "available": available,
                "in_cooldown": len(self.keys) - available,
                "keys": [k.status() for k in self.keys],
            }

    def __len__(self) -> int:
        return len(self.keys)
