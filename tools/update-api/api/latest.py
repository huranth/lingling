"""Manifest route."""

import json
import os
import re
import time
import urllib.request
from http.server import BaseHTTPRequestHandler

PACKAGE = os.environ.get("LINGLING_PACKAGE") or "lingling"
ROUTE = os.environ.get("LINGLING_API_ROUTE") or "/api/latest"
SCHEMA = int(os.environ.get("LINGLING_UPDATE_SCHEMA") or "1")
PYPI_URL = (
    os.environ.get("LINGLING_PYPI_URL")
    or f"https://pypi.org/pypi/{PACKAGE}/json"
)
PROJECT_URL = (
    os.environ.get("LINGLING_PROJECT_URL")
    or f"https://pypi.org/project/{PACKAGE}/"
)
PYPI_TIMEOUT = float(os.environ.get("LINGLING_PYPI_TIMEOUT") or "3.0")
NETWORK = os.environ.get("LINGLING_NETWORK") or "Route64"
TYPE = "application/json; charset=utf-8"
CDN_CACHE = (
    os.environ.get("LINGLING_CDN_CACHE")
    or "max-age=300, stale-while-revalidate=86400"
)
BROWSER_CACHE = os.environ.get("LINGLING_BROWSER_CACHE") or "public, max-age=60"
NOTICE = (
    os.environ.get("LINGLING_NOTICE")
    or f"{PACKAGE.title()} uses {NETWORK}. After updating, run: {PACKAGE} setup"
)
MIN_SUPPORTED = os.environ.get("LINGLING_MIN_SUPPORTED") or None
RETIRED = set(
    item.strip()
    for item in (os.environ.get("LINGLING_RETIRED") or "").split(",")
    if item.strip()
)


def _key(version):
    """Version key."""
    text = (version or "").strip().lstrip("vV").split("+", 1)[0].strip()
    post = 0
    if ".post" in text:
        text, _, tail = text.partition(".post")
        digits = re.sub(r"\D", "", tail)
        post = int(digits) if digits else 1
    parts = []
    for chunk in text.split("."):
        digits = re.sub(r"\D", "", chunk)
        parts.append(int(digits) if digits else 0)
    parts += [0] * (3 - len(parts))
    return (parts[0], parts[1], parts[2], 1 if post else 0, post)


def newest_installable():
    """Newest build."""
    try:
        request = urllib.request.Request(
            PYPI_URL, headers={"User-Agent": PACKAGE + "-update-api"}
        )
        with urllib.request.urlopen(request, timeout=PYPI_TIMEOUT) as response:
            doc = json.load(response)
    except Exception:
        return None

    if not isinstance(doc, dict):
        return None

    best = None
    for version, files in (doc.get("releases") or {}).items():
        if not files:
            continue
        if version in RETIRED:
            continue
        if all(f.get("yanked") for f in files):
            continue
        if best is None or _key(version) > _key(best):
            best = version
    if best is None:
        fallback = (doc.get("info") or {}).get("version")
        return None if fallback in RETIRED else fallback
    return best


def manifest():
    return {
        "schema": SCHEMA,
        "package": PACKAGE,
        "latest": newest_installable(),
        "min_supported": MIN_SUPPORTED,
        "notice": NOTICE,
        "url": PROJECT_URL,
        "checked_at": int(time.time()),
    }


class handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", TYPE)
        self.send_header("Vercel-CDN-Cache-Control", CDN_CACHE)
        self.send_header("Cache-Control", BROWSER_CACHE)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        """Route path."""
        path = (self.path or "").split("?", 1)[0].rstrip("/")
        if path == ROUTE:
            self._send(200, manifest())
        else:
            self._send(404, {"error": "not found", "try": ROUTE})

    def do_HEAD(self):
        self.do_GET()

    def log_message(self, fmt, *args):
        """Quiet log."""
        return
