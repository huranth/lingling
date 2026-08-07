"""Access control for the Lingling gateway.

Two kinds of caller need to reach this server, and they authenticate differently:

* **The dashboard**, running in a browser at the server's own origin. It gets a
  signed session cookie when it loads ``/``. The cookie is ``HttpOnly`` and
  ``SameSite=Strict``, so a malicious page on another origin cannot read it and
  the browser will not attach it to cross-site requests.
* **API clients** (Codex, Cline, scripts). They present a user API
  key as ``Authorization: Bearer ll_...`` or ``x-api-key``.

The previous gate trusted the ``sec-fetch-site`` request header. Browsers refuse
to let page scripts set that header, but *any* non-browser client can send
whatever it likes -- so a single ``-H "sec-fetch-site: same-origin"`` bypassed
auth completely. Header-asserted trust is not trust; this module replaces it
with a secret the server itself issued.

The session secret lives only in memory, so restarting the server invalidates
every dashboard session (the page simply re-fetches ``/`` and gets a new one).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional, Tuple

from fastapi import HTTPException, Request

from core import api_keys
from core import config

COOKIE_NAME = "lingling_session"

# Regenerated on every process start: sessions do not survive a restart, and no
# secret is ever written to disk.
_SESSION_SECRET = secrets.token_bytes(32)


def mint_session(ttl_s: Optional[int] = None) -> str:
    """Return a signed ``<expiry>.<hmac>`` session token."""
    ttl = int(ttl_s if ttl_s is not None else config.SESSION_TTL_SECONDS)
    expires = int(time.time()) + ttl
    payload = str(expires).encode()
    sig = hmac.new(_SESSION_SECRET, payload, hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def verify_session(token: Optional[str]) -> bool:
    """True if ``token`` is a valid, unexpired session token from this process."""
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
    # Constant-time compare: a timing oracle on the signature would let an
    # attacker forge a token byte by byte.
    return hmac.compare_digest(sig, expected)


def _same_origin(request: Request) -> bool:
    """True when the request's Origin/Referer matches the server's own host.

    This is a *defence in depth* check layered under the session cookie, not a
    substitute for it: `Origin` is also client-settable outside a browser. Its
    job is to reject cross-site requests that arrive carrying a cookie in some
    future browser that relaxes SameSite.
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
    """Return ``(authorised, actor)`` for this request.

    ``actor`` is ``"dashboard"``, ``"api-key"``, or ``"anonymous"``; it is used
    for logging and to decide whether a route that is open to the dashboard
    should also be open to a keyless client.
    """
    if verify_session(request.cookies.get(COOKIE_NAME)) and _same_origin(request):
        return True, "dashboard"
    if api_keys.validate(presented_key(request)):
        return True, "api-key"
    return False, "anonymous"


def require(request: Request) -> str:
    """Authorise a request or raise 401. Returns the actor.

    When ``config.REQUIRE_API_KEY`` is off the gate is open, which is the
    documented single-user local mode. The actor is still reported so callers
    can log it.
    """
    ok, actor = identify(request)
    if ok:
        return actor
    if not config.REQUIRE_API_KEY:
        return "open"
    raise HTTPException(
        401,
        "Unauthorised. Browser clients: load the dashboard at / to obtain a "
        "session. API clients: create a key at POST /api/keys and send it as "
        "'Authorization: Bearer ll_...' (or 'x-api-key: ll_...').",
    )


def allowed_origins() -> list:
    """Explicit CORS origin allow-list.

    A wildcard here was a real hole: with ``allow_origins=["*"]`` any page on
    the internet could read ``/api/keys`` or fire ``POST /api/warp/refresh``
    from a visitor's browser. Only the loopback origins the dashboard is
    actually served from are allowed, plus anything the operator names in
    ``LINGLING_ALLOWED_ORIGINS``.
    """
    origins = []
    for host in ("127.0.0.1", "localhost"):
        for port in config.DASHBOARD_PORTS:
            origins.append(f"http://{host}:{port}")
    origins.extend(o.strip() for o in config.ALLOWED_ORIGINS_ENV.split(",") if o.strip())
    return origins
