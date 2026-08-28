"""CORS origin allow-list for the Lingling gateway.

Lingling is a single-user gateway bound to loopback, and it runs open -- no API
keys, no session cookie. The one thing that still needs care is CORS: with
``allow_origins=["*"]`` any page on the internet could read ``/api/*`` or fire a
state-changing ``POST`` from a visitor's browser. Only the loopback origins the
dashboard is actually served from are allowed, plus anything the operator names
in ``LINGLING_ALLOWED_ORIGINS``.
"""

from __future__ import annotations

from core import config


def allowed_origins() -> list:
    """Explicit CORS origin allow-list (loopback dashboard ports + operator extras)."""
    origins = []
    for host in ("127.0.0.1", "localhost"):
        for port in config.DASHBOARD_PORTS:
            origins.append(f"http://{host}:{port}")
    origins.extend(o.strip() for o in config.ALLOWED_ORIGINS_ENV.split(",") if o.strip())
    return origins