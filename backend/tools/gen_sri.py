"""Compute SRI (subresource integrity) hashes for the pinned CDN assets.

Prints ready-to-paste integrity attributes. Without these, ~900 KB of
third-party JS is executed with no verification that it is the code we pinned.
"""
import base64
import hashlib
import sys
import urllib.request

ASSETS = [
    ("css", "https://cdn.jsdelivr.net/npm/uplot@1.6.32/dist/uPlot.min.css"),
    ("css", "https://cdn.jsdelivr.net/npm/tabulator-tables@6.3.1/dist/css/tabulator.min.css"),
    ("js",  "https://cdn.jsdelivr.net/npm/uplot@1.6.32/dist/uPlot.iife.min.js"),
    ("js",  "https://cdn.jsdelivr.net/npm/tabulator-tables@6.3.1/dist/js/tabulator.min.js"),
    ("js",  "https://cdn.jsdelivr.net/npm/lucide@0.469.0/dist/umd/lucide.min.js"),
    ("js",  "https://cdn.jsdelivr.net/npm/marked@15.0.7/marked.min.js"),
    ("js",  "https://cdn.jsdelivr.net/npm/dompurify@3.2.4/dist/purify.min.js"),
]

for kind, url in ASSETS:
    try:
        with urllib.request.urlopen(url, timeout=45) as r:
            body = r.read()
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {url} -> {exc}", file=sys.stderr)
        continue
    digest = base64.b64encode(hashlib.sha384(body).digest()).decode()
    print(f'{kind}\t{len(body):>7}\t{url}\tsha384-{digest}')
