"""Access control for the Lingling gateway.

Two callers authenticate differently: the browser dashboard uses a signed
HttpOnly session cookie issued at ``/``; API clients (Codex, Cline, scripts)
present a user key as ``Authorization: Bearer ll_...`` or ``x-api-key``.

The session secret lives only in memory, so a restart invalidates every
dashboard session (the page re-fetches ``/`` and gets a new one).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional, Tuple

from fastapi import Request

from core import api_keys
from core import config

COOKIE_NAME = "lingling_session"

# Regenerated per process: sessions don't survive a restart, no secret on disk.
_SESSION_SECRET = secrets.token_bytes(32)


def mint_session(ttl_s: Optional[int] = None) -> str:
    """Return a signed ``<expiry>.<hmac>`` session token."""
    ttl = int(ttl_s if ttl_s is not None else config.SESSION_TTL_SECONDS)
    expires = int(time.time()) + ttl
    payload = str(expires).encode()
    sig = hmac.new(_SESSION_SECRET, payload, hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def verify_session(token: Optional[str]) -> bool:
    """True if ``token`` is a valid, unexpired token from this process."""
    if not token or "." not in token:
        return False
    expires_raw, _, sig = token.partition(".")
    try:
        expires = int(expires_raw)
    except ValueError:
        return False
    if expires < time.time():
        return False
    expected = hmac.new(_SESSION_SECRET, expires_raw.encode(), hashlib.sha256).hexdigest()
    # Constant-time compare: a timing oracle would let an attacker forge a token.
    return hmac.compare_digest(sig, expected)


def _same_origin(request: Request) -> bool:
    """True when the request's Origin/Referer matches the server's own host.

    Defence in depth under the session cookie, not a substitute: Origin is
    client-settable outside a browser. It rejects cross-site cookie-bearing
    requests should a future browser relax SameSite.
    """
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if not origin:
        # Same-origin GETs and non-CORS requests often omit Origin entirely.
        return True
    host = request.headers.get("host", "")
    if not host:
        return False
    from urllib.parse import urlsplit
    return urlsplit(origin).netloc == host


def presented_key(request: Request) -> Optional[str]:
    return request.headers.get("authorization") or request.headers.get("x-api-key")


def identify(request: Request) -> Tuple[bool, str]:
    """Return ``(authorised, actor)`` -- actor is dashboard/api-key/anonymous."""
    if verify_session(request.cookies.get(COOKIE_NAME)) and _same_origin(request):
        return True, "dashboard"
    if api_keys.validate(presented_key(request)):
        return True, "api-key"
    return False, "anonymous"


def allowed_origins() -> list:
    """Explicit CORS origin allow-list.

    Never a wildcard: with ``allow_origins=["*"]`` any internet page could read
    ``/api/keys`` from a visitor's browser. Only loopback origins the dashboard
    is served from, plus anything named in ``LINGLING_ALLOWED_ORIGINS``.
    """
    origins = []
    for host in ("127.0.0.1", "localhost"):
        for port in config.DASHBOARD_PORTS:
            origins.append(f"http://{host}:{port}")
    origins.extend(o.strip() for o in config.ALLOWED_ORIGINS_ENV.split(",") if o.strip())
    return origins
