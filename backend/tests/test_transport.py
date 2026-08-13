"""Tests for upstream transport headers, especially the User-Agent gate.

OpenCode gates its premium free models behind the official client's
User-Agent.  These hermetic tests assert that Lingling always sends the
configured `opencode`-style UA on every upstream call, since forgetting it
silently 429s deepseek-v4-flash-free / mimo-v2.5-free / big-pickle.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("LINGLING_DATA_DIR", tempfile.mkdtemp(prefix="lingling-ua-test-"))
os.environ.setdefault("LINGLING_REQUIRE_KEY", "0")
os.environ.setdefault("LINGLING_BOOTSTRAP_WARP", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config  # noqa: E402
from providers.opencode import OpenCodeProvider  # noqa: E402
from providers.key_pool import KeyPool  # noqa: E402


def test_auth_headers_include_user_agent():
    prov = OpenCodeProvider(KeyPool([]))
    headers = prov.auth_headers("")
    assert headers["User-Agent"] == config.UPSTREAM_USER_AGENT
    assert headers["User-Agent"].startswith("opencode")


def test_auth_headers_user_agent_present_with_secret():
    prov = OpenCodeProvider(KeyPool([]))
    headers = prov.auth_headers("sk-test")
    assert headers["User-Agent"] == config.UPSTREAM_USER_AGENT
    assert headers["Authorization"] == "Bearer sk-test"


def test_auth_headers_keyless_has_no_authorization():
    prov = OpenCodeProvider(KeyPool([]))
    headers = prov.auth_headers("")
    assert "Authorization" not in headers
    # But the UA must still be there -- that is the free-tier gate.
    assert "User-Agent" in headers


def test_config_default_user_agent_is_opencode():
    assert config.UPSTREAM_USER_AGENT.startswith("opencode")


def test_chat_completions_sends_user_agent(monkeypatch):
    """The UA must reach the wire on a real chat_completions call."""
    import httpx

    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            captured["headers"] = headers
            return FakeResp()

    # Patch the connection pool's client context manager to capture headers.
    from providers import connection_pool

    class FakePool:
        def get_client(self, proxy_id, proxy_url, timeout):
            import contextlib

            @contextlib.contextmanager
            def _cm():
                yield FakeClient()

            return _cm()

    monkeypatch.setattr(connection_pool, "get_connection_pool", lambda: FakePool())

    prov = OpenCodeProvider(KeyPool([]))
    prov.chat_completions([{"role": "user", "content": "hi"}], "deepseek-v4-flash-free", "")

    assert captured["headers"]["User-Agent"] == config.UPSTREAM_USER_AGENT
