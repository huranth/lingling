"""Real routing tests for the Lingling routing proxy.

Two layers, neither of which mocks the upstream APIs:

1. HERMETIC unit tests -- exercise the executor's failover CONTROL FLOW with
   tiny in-process fake providers (no network). These verify the keyless
   attempt path, cross-provider failover, and that retryable codes
   (428/426/409/410) are treated as failover triggers.

2. LIVE integration tests -- run the real FastAPI app in-process via
   ``TestClient`` and talk to the REAL OpenCode Zen free tier, which is
   keyless (verified): ``GET /v1/models`` and the free models on
   ``POST /v1/chat/completions`` need no credential. These hit the live
   network (OpenCode Zen + models.dev) and the real dispatcher model.

3. USER API KEY tests -- exercise the new key-creation / auth-gate flow
   against the real app (hermetic, no network).

Run directly (``python tests/test_routing.py``) or with pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import importlib
import unittest

# Isolated data dir so tests never touch the real usage database. NO accounts
# file -> OpenCode runs keyless, exactly like a fresh install.
os.environ["LINGLING_DATA_DIR"] = tempfile.mkdtemp(prefix="lingling-test-")
os.environ["LINGLING_ACCOUNTS_FILE"] = os.path.join(
    os.environ["LINGLING_DATA_DIR"], "accounts.json"
)
os.environ["LINGLING_API_KEYS_FILE"] = os.path.join(
    os.environ["LINGLING_DATA_DIR"], "api_keys.json"
)
# Live chat tests run keyless against the real free tier; auth is exercised
# separately by test_unit_apikey_gate, so we disable the gate here.
os.environ["LINGLING_REQUIRE_KEY"] = "0"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import config  # noqa: E402
from core import api_keys  # noqa: E402
from routing import executor  # noqa: E402
from providers.base import Provider, UpstreamError  # noqa: E402
from providers.key_pool import KeyPool  # noqa: E402
from providers.proxy_pool import ProxyPool  # noqa: E402

from app import app, providers, catalog  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)


class SkipTest(unittest.SkipTest):
    """Raised by a test to mark itself skipped (e.g. missing credential).

    Subclasses ``unittest.SkipTest`` so pytest reports the test as ``skipped``
    natively (rather than as an error) while the script runner in ``main()``
    still catches it via ``except SkipTest``.
    """


class _QuietLog:
    """Logger stand-in: stream_guard logs recovery decisions, and the test
    output should stay readable.
    """

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


# ---------------------------------------------------------------------------
# Fakes for the HERMETIC executor unit tests (control flow only, no network).
# ---------------------------------------------------------------------------
class FakeProvider(Provider):
    """A no-network provider whose chat_completions behavior is injected."""

    display_name = "Fake"
    base_url = "http://fake.invalid"

    def __init__(self, pid, behavior, keyed=False, nkeys=1, use_proxy=False, direct=False):
        keys = KeyPool.from_list([{"secret": f"k{i}"} for i in range(nkeys)]) if keyed else KeyPool([])
        super().__init__(keys)
        self.id = pid
        self.display_name = pid
        self._behavior = behavior
        self._keyed = keyed
        self._use_proxy = use_proxy
        self._direct = direct
        self.calls = []

    def requires_key(self):
        return self._keyed

    def needs_proxy(self):
        return self._use_proxy

    def prefer_direct(self, model_id):
        return self._direct

    def fetch_model_ids(self):
        return []

    def is_model_free(self, model_id, meta):
        return True

    def chat_completions(self, messages, model, secret, timeout=None, **params):
        self.calls.append({"model": model, "secret": secret})
        b = self._behavior
        if isinstance(b, UpstreamError):
            raise b
        if callable(b):
            return b(self, messages, model, secret)
        return b

    def stream_chat(self, messages, model, secret, timeout=None, **params):
        self.calls.append({"model": model, "secret": secret, "stream": True})
        b = self._behavior
        if isinstance(b, UpstreamError):
            raise b
        if callable(b):
            b = b(self, messages, model, secret)
        yield from b


CANNED = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
}


# ---------------------------------------------------------------------------
# 1. HERMETIC executor unit tests
# ---------------------------------------------------------------------------
def test_unit_keyless_single_attempt():
    """A keyless provider succeeds in one attempt with an empty secret."""
    fk = FakeProvider("keyless", CANNED, keyed=False)
    resp, prov, key, attempts = executor.execute_nonstream(
        [{"role": "user", "content": "hi"}], "m", [fk]
    )
    assert resp is CANNED and prov is fk and key is None and attempts == []
    assert fk.calls[0]["secret"] == ""  # no credential sent


def test_unit_keyless_failover_to_keyed():
    """Keyless 429 falls over to a keyed provider; the attempt is recorded."""
    bad = FakeProvider("keyless", UpstreamError(429, "rate limited", "keyless"))
    good = FakeProvider("keyed", CANNED, keyed=True)
    resp, prov, key, attempts = executor.execute_nonstream(
        [{"role": "user", "content": "hi"}], "m", [bad, good]
    )
    assert resp is CANNED and prov is good and key is not None
    assert attempts and attempts[0]["provider"] == "keyless" and attempts[0]["status"] == 429


def test_unit_retry_codes_failover():
    """Retryable codes (428/426/409/410/429) trigger failover, not a hard fail."""
    for code in (428, 426, 409, 410, 429):
        bad = FakeProvider("fb", UpstreamError(code, "x", "fb"))
        good = FakeProvider("keyed", CANNED, keyed=True)
        resp, prov, _, attempts = executor.execute_nonstream(
            [{"role": "user", "content": "hi"}], "m", [bad, good]
        )
        assert resp is CANNED and prov is good, f"code {code} did not fail over"
        assert attempts[0]["status"] == code


def test_unit_non_retryable_stops():
    """A non-retryable status (400) raises AllFailedError immediately."""
    bad = FakeProvider("keyless", UpstreamError(400, "bad request", "keyless"))
    good = FakeProvider("keyed", CANNED, keyed=True)
    try:
        executor.execute_nonstream([{"role": "user", "content": "hi"}], "m", [bad, good])
    except executor.AllFailedError as exc:
        assert exc.attempts and exc.attempts[0]["status"] == 400
        assert good.calls == []  # never reached the second provider
    else:
        raise AssertionError("expected AllFailedError for non-retryable 400")


def test_unit_all_fail():
    """Every provider failing raises AllFailedError with the full attempt log."""
    a = FakeProvider("a", UpstreamError(429, "x", "a"))
    b = FakeProvider("b", UpstreamError(503, "y", "b"))
    try:
        executor.execute_nonstream([{"role": "user", "content": "hi"}], "m", [a, b])
    except executor.AllFailedError as exc:
        assert [x["provider"] for x in exc.attempts] == ["a", "b"]
    else:
        raise AssertionError("expected AllFailedError when all providers fail")


def test_unit_retry_after_parses_seconds_and_http_dates():
    """Retry-After is either delta-seconds or an HTTP-date; both must parse so
    the real backoff ask is honored, a back-dated header clamps to 0, and a
    missing/unparseable one yields None so the caller keeps the heuristic base."""
    import datetime
    from providers.base import _retry_after_seconds
    assert _retry_after_seconds({"retry-after": "120"}) == 120.0
    assert _retry_after_seconds({"retry-after": "30.5"}) == 30.5
    # A back-dated HTTP-date does not park the lane for a negative window.
    assert _retry_after_seconds({"retry-after": "Wed, 21 Oct 2010 07:28:00 GMT"}) == 0.0
    # A future HTTP-date yields a positive delta around the requested wait.
    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=90)
    httpdate = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert 80.0 < _retry_after_seconds({"retry-after": httpdate}) <= 95.0
    # Missing or unparseable -> None (the executor skips the extension).
    assert _retry_after_seconds({}) is None
    assert _retry_after_seconds({"retry-after": "later"}) is None


def test_unit_retry_after_extends_cooldown_past_heuristic_base():
    """A 429 carrying Retry-After parks the proxy for the upstream-advised
    window (clamped), not the heuristic exponential base. Re-tried early, the
    lane just 429s again and re-burns the failover budget; the upstream's
    explicit ask beats the guess. extend_cooldown never bumps the tally."""
    bad = FakeProvider(
        "keyless",
        UpstreamError(429, "rate limited", "keyless", retry_after=30),
        use_proxy=True,
    )
    pool = ProxyPool.from_list([{"id": "px", "url": "socks5://127.0.0.1:1"}])
    try:
        executor.execute_nonstream(
            [{"role": "user", "content": "hi"}], "m", [bad], proxy_pool=pool
        )
    except executor.AllFailedError:
        pass
    px = pool.get_by_id("px")
    # mark_failure(429) parks ~10s (BLOCKED base); honoring Retry-After extends
    # it to ~30s. A 10s-only park would mean the header was ignored.
    assert px.cooldown_remaining() > 20.0, px.cooldown_remaining()
    assert px.consecutive_failures == 1   # extend_cooldown leaves the tally alone


def test_unit_retry_after_honored_on_stream_before_first_token():
    """A pre-200 stream 429 with Retry-After parks the originating proxy for the
    advised window too -- the failure surfaces before first token, so the stream
    path cools the proxy exactly like non-stream. ``good`` is a direct provider
    (no proxy) so the burned lane deterministically owns the single pool exit."""
    bad = FakeProvider(
        "keyless", UpstreamError(429, "rate limited", "keyless", retry_after=45),
        use_proxy=True,
    )
    good = FakeProvider("keyless2", iter([b'data: {"choices":[]}', b"data: [DONE]"]))
    pool = ProxyPool.from_list([{"id": "px", "url": "socks5://127.0.0.1:1"}])
    stream, prov, key, attempts = executor.execute_stream(
        [{"role": "user", "content": "hi"}], "m", [bad, good], proxy_pool=pool
    )
    assert prov is good
    px = pool.get_by_id("px")
    assert px.cooldown_remaining() > 35.0, (px.cooldown_remaining(), attempts)
    assert px.consecutive_failures == 1


def test_unit_stream_first_chunk_failover():
    """A pre-first-token proxy/provider failure retries before HTTP 200 is sent."""
    bad = FakeProvider("bad", UpstreamError(504, "proxy unreachable", "bad"))
    good = FakeProvider("good", iter([b'data: {"choices":[]}', b"data: [DONE]"]))
    stream, prov, key, attempts = executor.execute_stream(
        [{"role": "user", "content": "hi"}], "m", [bad, good]
    )
    assert prov is good and key is None
    assert attempts[0]["provider"] == "bad" and attempts[0]["status"] == 504
    assert list(stream)[-1] == b"data: [DONE]"


def test_unit_fast_model_bypasses_proxy_pool():
    """The fast route must not touch a dead WARP/SOCKS endpoint."""
    fast = FakeProvider("fast", CANNED, direct=True)
    pool = ProxyPool.from_list([{"id": "dead-warp", "url": "socks5://127.0.0.1:1"}])
    resp, prov, _, attempts = executor.execute_nonstream(
        [{"role": "user", "content": "hi"}], "ling-3.0-flash-free", [fast], proxy_pool=pool
    )
    assert resp is CANNED and prov is fast and attempts == []
    assert pool.proxies[0].total_requests == 0


# ---------------------------------------------------------------------------
# 2. LIVE integration tests (real OpenCode Zen free tier, keyless, no mocks)
# ---------------------------------------------------------------------------
def test_live_health():
    r = client.get("/api/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["name"] == "Lingling"
    # OpenCode is keyless -> always configured.
    assert body["providers"]["opencode"]["configured"] is True


def _live_free_ids() -> set:
    """The free model ids OpenCode advertises *right now*.

    Fetched live and filtered with the exact rule Lingling's catalog applies
    (``-free`` suffix or a curated keyless entry), so these tests can never go
    stale against the catalog: when OpenCode ships a new free model, this picks
    it up automatically and asserts the catalog serves it. A hardcoded list
    here is what let the tests and the catalog disagree (the catalog showed 8
    free models while the tests only knew about 6).
    """
    from providers.opencode import OpenCodeProvider

    prov = OpenCodeProvider(KeyPool([]))
    live = {
        mid for mid in prov.fetch_model_ids()
        if prov.is_model_free(mid, {})
    }
    # Models seeded as retired are deliberately hidden from the catalog before
    # any runtime 400, so the live-sync expectation must subtract them too --
    # otherwise a model Lingling now pre-retires (ling-3.0-flash-free) would
    # read as "missing from the catalog" and fail the test.
    return live - set(config.retired_seed_ids())


def test_live_models():
    r = client.get("/api/models")
    assert r.status_code == 200, r.text
    body = r.json()
    mm = body["multimodel"]
    assert mm["multi_model"] is True and mm["id"] == config.MULTIMODEL_ID
    models = body["models"]
    assert models, "expected a non-empty free model list"
    ids = {m["id"] for m in models}
    # Every surfaced model is free and carries its provider list.
    for m in models:
        assert m["free"] is True
        assert isinstance(m["providers"], list) and m["providers"], m["id"]
        assert m["provider_count"] == len(m["providers"])
    # Live sync check: every free model OpenCode advertises today must be in
    # the catalog -- no hardcoded list to maintain when new models appear.
    missing = _live_free_ids() - ids
    assert not missing, f"expected free models missing from catalog: {missing}"


def test_live_v1_models_openai_compatible():
    """Jan/Cline/OpenAI-compatible clients load models from GET /v1/models."""
    r = client.get("/v1/models")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list) and body["data"], body
    ids = {m["id"] for m in body["data"]}
    assert config.MULTIMODEL_ID in ids
    # Same live sync as /api/models: whatever OpenCode marks free today must be
    # advertised through the OpenAI-compatible list too.
    assert _live_free_ids() <= ids
    for m in body["data"]:
        assert m["object"] == "model"
        assert m["id"]
        assert "owned_by" in m


def test_live_responses_nonstream_keyless():
    """Codex uses /v1/responses; the bridge returns a Responses object."""
    # Pick the model from the live catalog, never a hardcoded id: OpenCode
    # retires free models without notice (ling-3.0-flash-free was dropped while
    # these tests were in service), and a stale id makes the suite fail on a
    # model nobody serves anymore.
    ids = _live_free_ids()
    assert ids, "expected at least one live free model"
    mid = config.DISPATCHER_MODEL if config.DISPATCHER_MODEL in ids else sorted(ids)[0]
    r = client.post(
        "/v1/responses",
        json={
            "model": mid,
            "input": "Reply with exactly one word: pong",
            "stream": False,
            "max_output_tokens": 16,
        },
        timeout=90,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["output"] and body["output"][0]["type"] == "message", body
    assert body["usage"]["total_tokens"] >= 0
    l = body["lingling"]
    assert l["routed_model"] in _live_free_ids(), l
    # When the named model is served, it must stay on it. A live 429/400 can
    # legitimately fall back to another free model (routed_by="fallback").
    if l["routed_by"] != "fallback":
        assert l["routed_model"] == mid, l


def test_live_free_chat_direct():
    """Direct selection of a free model runs keyless on OpenCode Zen."""
    # Pick the id from the live catalog (``catalog.free`` -- the handler's own
    # source), never a hardcoded string: OpenCode retires free models without
    # notice, and a pinned id makes the suite fail against a model nobody
    # serves anymore. A live 429/400 may legitimately fall back to another
    # free model (``routed_by == "fallback"``); only when the named model
    # actually answered do we pin the routed id and the ``user`` wire contract.
    ids = sorted({m.id for m in catalog.free()})
    assert ids, "expected at least one live free model"
    mid = config.DISPATCHER_MODEL if config.DISPATCHER_MODEL in ids else ids[0]
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": mid,
            "messages": [{"role": "user", "content": "Reply with exactly one word: pong"}],
            "max_tokens": 16,
        },
        timeout=90,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["choices"], body
    ll = body["lingling"]
    assert ll["provider"] == "opencode"
    assert ll["routed_model"] in _live_free_ids(), ll
    if ll["routed_by"] != "fallback":
        assert ll["routed_by"] == "user", ll
        assert ll["routed_model"] == mid, ll


def test_live_multimodel_routing():
    """lingling-auto runs the real dispatcher and routes to a free model.

    Skips (with a breadcrumb) when OpenCode's free tier is transitorily cooked
    for the dispatcher's routed model AND every fallback, so an upstream outage
    surfaces as an explanatory skip rather than a red that implies a Lingling
    regression -- the same skip-on-cooked contract as the streaming sibling
    above. When the tier is healthy the full routing assertion still runs.
    """
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": config.MULTIMODEL_ID,
            "messages": [
                {"role": "user", "content": "Write a Python function that reverses a string."}
            ],
            "max_tokens": 200,
        },
        timeout=120,
    )
    if r.status_code == 503:
        raise SkipTest(
            f"lingling-auto 503'd ({r.json().get('detail', r.text)[:200]}) -- "
            f"the dispatcher's routed model and its fallbacks all exhausted "
            f"against OpenCode's free tier upstream; re-run once the tier is "
            f"serving again. (A 2026-08-23 deepseek-v4-flash-free outage "
            f"reproduced this way with the sampler disabled, so it is upstream "
            f"of Lingling's code.)"
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["choices"], body
    ll = body["lingling"]
    assert ll["routed_by"] in ("dispatcher", "fallback"), ll
    assert ll["routed_model"] and ll["routed_model"] != config.MULTIMODEL_ID
    # Whatever it picked must be a free model the catalog knows about.
    assert catalog.is_free(ll["routed_model"]) is not False


def test_live_streaming_keyless():
    """Streaming binds to the keyless OpenCode path and yields SSE chunks.

    Picks every id from the live catalog (never a hardcoded name) and tries the
    first few against ``/v1/chat/completions`` streaming, DISPATCHER_MODEL LAST,
    skipping the test if NONE of them currently returns 200. A transient
    upstream 'Model is unavailable' on one id (deepseek-v4-flash-free on
    2026-08-23) is upstream of Lingling's code and not actionable from a red
    test -- the SKIP preserves the green signal and the reason names the last
    status it saw so an operator investigating the outage has a breadcrumb.
    """
    ids = sorted({m.id for m in catalog.free()})
    assert ids, "expected at least one live free model"

    # DISPATCHER_MODEL (default deepseek-v4-flash-free) recovers slowest from
    # an upstream outage announcement: OpenCode marks its stream endpoint
    # 'Model is unavailable' while the non-stream path still 200s and /models
    # keeps listing it -- so DISPATCHER_MODEL is left as the LAST poll, and
    # any earlier live id's stream that returns 200 wins.
    ordered = [i for i in ids if i != config.DISPATCHER_MODEL][:3]
    if config.DISPATCHER_MODEL in ids:
        ordered.append(config.DISPATCHER_MODEL)

    chunks = None
    last_status = None
    for candidate in ordered:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": candidate,
                "messages": [{"role": "user", "content": "Count from 1 to 3."}],
                "max_tokens": 40,
                "stream": True,
            },
            timeout=45,
        ) as r:
            if r.status_code != 200:
                last_status = r.status_code
                continue
            chunks = [line for line in r.iter_lines() if line.startswith("data:")]
        break  # 200 -- stop polling more models

    if chunks is None:
        raise SkipTest(
            f"no live free model's stream endpoint returned 200 on "
            f"/v1/chat/completions (last_status={last_status}); OpenCode's free "
            f"streaming tier is transitorily unavailable upstream -- investigate "
            f"if every free id stays 'Model is unavailable' while /models keeps "
            f"listing them"
        )
    assert chunks, "expected at least one SSE data chunk"
    assert any("[DONE]" in c for c in chunks), "stream did not terminate with [DONE]"


def test_live_premium_rejected():
    """A known premium model is rejected (Lingling is free-tier only)."""
    r = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hi"}]},
        timeout=60,
    )
    # The model is unknown to all providers ├óΓÇáΓÇÖ 400 with an explanatory message.
    assert r.status_code == 400, r.text
    assert "unknown" in r.json()["detail"].lower()


def test_live_usage_recorded():
    """The calls above were logged, with a per-provider breakdown."""
    r = client.get("/api/usage")
    assert r.status_code == 200, r.text
    summary = r.json()["summary"]
    assert summary["totals"]["requests"] >= 3, summary["totals"]
    # per_provider is a list of {provider, requests, ...} rows; the empty-
    # provider row is the premium rejection (logged before any routing).
    per_provider = {row["provider"]: row for row in summary["per_provider"]}
    assert "opencode" in per_provider, summary["per_provider"]
    assert per_provider["opencode"]["requests"] >= 1


def test_live_apikey_gate():
    """User API keys gate /v1/chat/completions: rejected without a key,
    accepted with a freshly-created key. (Uses a short isolated app.)"""
    import importlib
    # Re-import config + app with auth re-enabled so the gate is live here.
    os.environ["LINGLING_REQUIRE_KEY"] = "1"
    importlib.reload(config)
    importlib.reload(api_keys)
    # Fresh app instance wired to the current config.
    import app as app_module
    importlib.reload(app_module)
    gated_client = TestClient(app_module.app)
    # Only the 401 gate is asserted here, so the model is inert -- but no
    # hardcoded id anywhere in the live suite: pick one the fresh catalog
    # serves RIGHT NOW so the test never reads like it pins a model.
    mid = sorted({m.id for m in app_module.catalog.free()})[0]
    assert mid, "expected at least one live free model"
    try:
        # No key -> 401.
        assert gated_client.get("/v1/models").status_code == 200
        r = gated_client.post(
            "/v1/chat/completions",
            json={"model": mid,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 401, r.text
        # Create a key. Key management is itself gated now, so the client
        # first loads / to obtain a dashboard session cookie -- the same
        # sequence a browser performs.
        assert gated_client.post("/api/keys", json={"label": "x"}).status_code == 401
        assert gated_client.get("/").status_code == 200
        created = gated_client.post("/api/keys", json={"label": "test"}).json()["created"]
        token = created["token"]
        # With key -> not 401 (may be 200/4xx/5xx from upstream; we only assert
        # the gate passed).
        r = gated_client.post(
            "/v1/chat/completions",
            json={"model": mid,
                  "messages": [{"role": "user", "content": "hi"}],
                  "max_tokens": 8},
            headers={"Authorization": f"Bearer {token}"},
            timeout=90,
        )
        assert r.status_code != 401, r.text
        # Wrong key -> 401. The dashboard session cookie the client picked up
        # above would legitimately authorise it, so drop the cookie first and
        # test the key path in isolation.
        gated_client.cookies.clear()
        r = gated_client.post(
            "/v1/chat/completions",
            json={"model": mid,
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer ll_wrong"},
        )
        assert r.status_code == 401, r.text
        # Revoke -> gone. Needs a credential again; reuse the real key.
        r = gated_client.delete(
            f"/api/keys/{created['id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
    finally:
        # Restore the keyless mode for any later tests.
        os.environ["LINGLING_REQUIRE_KEY"] = "0"
        importlib.reload(config)
        importlib.reload(api_keys)
        importlib.reload(app_module)


def test_unit_session_token_signing():
    """Session tokens are HMAC-signed, expiring, and per-process.

    The old gate trusted the ``sec-fetch-site`` header, which any non-browser
    client can set -- auth was bypassable with one extra curl flag. These
    assertions pin the replacement's properties.
    """
    import importlib

    from core import auth

    token = auth.mint_session()
    assert auth.verify_session(token), "a freshly minted token must verify"

    expires, _, sig = token.partition(".")
    assert not auth.verify_session(f"{expires}.{'0' * len(sig)}"), "forged signature accepted"
    assert not auth.verify_session(f"{int(expires) + 3600}.{sig}"), "extended expiry accepted"
    assert not auth.verify_session("nonsense"), "malformed token accepted"
    assert not auth.verify_session(""), "empty token accepted"
    assert not auth.verify_session(None), "None accepted"

    assert not auth.verify_session(auth.mint_session(ttl_s=-10)), "expired token accepted"

    # The signing secret is per-process: a restart invalidates old sessions.
    importlib.reload(auth)
    assert not auth.verify_session(token), "token survived a secret rotation"


def test_unit_secfetch_header_no_longer_grants_access():
    """Regression guard: the historical sec-fetch-site bypass stays closed."""
    import importlib

    os.environ["LINGLING_REQUIRE_KEY"] = "1"
    importlib.reload(config)
    importlib.reload(api_keys)
    import app as app_module
    importlib.reload(app_module)
    gated = TestClient(app_module.app)
    try:
        body = {"model": "deepseek-v4-flash-free",
                "messages": [{"role": "user", "content": "hi"}]}
        for value in ("same-origin", "same-site", "none"):
            r = gated.post("/v1/chat/completions", json=body,
                           headers={"sec-fetch-site": value})
            assert r.status_code == 401, f"sec-fetch-site={value} bypassed the gate"

        # The destructive /api routes are gated too, not just chat.
        for method, path in (("GET", "/api/keys"),
                            ("GET", "/api/proxies"),
                            ("DELETE", "/api/usage"),
                            ("POST", "/api/warp/refresh")):
            r = gated.request(method, path, headers={"sec-fetch-site": "same-origin"})
            assert r.status_code == 401, f"{method} {path} was open ({r.status_code})"

        # Key creation is itself gated; these two stay deliberately keyless.
        r = gated.post("/api/keys", json={"label": "gate"},
                       headers={"sec-fetch-site": "same-origin"})
        assert r.status_code == 401, "key creation must be gated"
        assert gated.get("/api/health").status_code == 200
        assert gated.get("/v1/models").status_code == 200
    finally:
        os.environ["LINGLING_REQUIRE_KEY"] = "0"
        importlib.reload(config)
        importlib.reload(api_keys)
        importlib.reload(app_module)


def test_unit_session_cookie_authenticates():
    """A cookie minted by GET / authorises the guarded routes."""
    import importlib

    os.environ["LINGLING_REQUIRE_KEY"] = "1"
    importlib.reload(config)
    importlib.reload(api_keys)
    import app as app_module
    importlib.reload(app_module)
    gated = TestClient(app_module.app)
    try:
        assert gated.get("/api/keys").status_code == 401
        # TestClient keeps cookies across requests, exactly like a browser.
        root = gated.get("/")
        assert root.status_code == 200
        assert app_module.auth.COOKIE_NAME in root.cookies, root.cookies
        assert gated.get("/api/keys").status_code == 200, "session cookie was not accepted"
        assert gated.get("/api/proxies").status_code == 200
    finally:
        os.environ["LINGLING_REQUIRE_KEY"] = "0"
        importlib.reload(config)
        importlib.reload(api_keys)
        importlib.reload(app_module)


def test_unit_usage_finalize_and_prune():
    """Streamed rows finalize their tokens; retention pruning drops old rows."""
    from usage.store import UsageStore

    store = UsageStore(os.path.join(tempfile.mkdtemp(prefix="lingling-prune-"), "u.db"))
    try:
        # A streamed request logs at first chunk with zero tokens, then finalizes.
        rid = store.log("m", "m", "user", status="ok_stream", streamed=True, latency_ms=12.0)
        assert isinstance(rid, int) and rid > 0
        assert store.recent(5)[0]["tokens_out"] == 0

        store.finalize(rid, tokens_in=11, tokens_out=22, reasoning_tokens=5, latency_ms=99.0)
        row = store.recent(5)[0]
        assert (row["tokens_in"], row["tokens_out"], row["reasoning_tokens"]) == (11, 22, 5), row
        assert round(row["latency_ms"]) == 99, row
        assert row["status"] == "ok_stream", "finalize must not clobber a good status"
        assert store.summary()["totals"]["tokens_total"] == 33

        # since() drives the dashboard's incremental poll.
        assert store.since(rid) == []
        rid2 = store.log("m", "m", "user", status="ok")
        assert [r["id"] for r in store.since(rid)] == [rid2]

        # buckets() is the live series: fixed width, zero-filled.
        buckets = store.buckets(minutes=5, bucket_s=60)
        assert len(buckets) == 5
        assert sum(b["requests"] for b in buckets) == 2, buckets

        # Backdate one row past the window, then prune.
        store._conn.execute(  # noqa: SLF001 - the test reaches in deliberately
            "UPDATE request_log SET ts = ts - ? WHERE id = ?", (100 * 86400, rid))
        store._conn.commit()  # noqa: SLF001
        assert store.prune(90) == 1
        assert store.summary()["totals"]["requests"] == 1
        assert store.prune(0) == 0, "0 must disable pruning"
    finally:
        store.close()


def test_unit_stream_guard_recovers_broken_stream():
    """A stream that dies before completing is retried once on a fresh stream.

    The client sees a `lingling_reset` frame telling it to discard the partial
    answer, then the replacement answer. This is the mid-flight case HTTP cannot
    fail over: the 200 is already sent.
    """
    from routing import stream_guard

    def dying():
        yield b'data: {"choices":[{"delta":{"content":"half an ans"}}]}'
        raise UpstreamError(504, "tunnel dropped", "opencode")

    def healthy():
        yield b'data: {"choices":[{"delta":{"content":"a full answer"}}]}'
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
        yield b"data: [DONE]"

    outcome = stream_guard.StreamOutcome()
    frames = list(stream_guard.guarded_stream(
        open_stream=healthy, first=dying(), outcome=outcome,
        on_chunk=lambda raw: None, log=_QuietLog(),
    ))
    body = b"".join(frames).decode()
    assert "lingling_reset" in body, body
    assert "a full answer" in body, body
    assert outcome.recovered is True
    assert outcome.attempts == 2
    assert outcome.completed is True
    assert outcome.error is None
    # The reset frame must arrive before the replacement text, or a client would
    # discard the new answer instead of the old one.
    assert body.index("lingling_reset") < body.index("a full answer")


def test_unit_stream_guard_no_retry_after_completion():
    """A disconnect after finish_reason is harmless and must not burn a retry."""
    from routing import stream_guard

    calls = []

    def after_done():
        yield b'data: {"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}'
        raise UpstreamError(504, "connection reset", "opencode")

    def should_not_run():
        calls.append(1)
        yield b"data: [DONE]"

    outcome = stream_guard.StreamOutcome()
    list(stream_guard.guarded_stream(
        open_stream=should_not_run, first=after_done(), outcome=outcome,
        on_chunk=lambda raw: None, log=_QuietLog(),
    ))
    assert calls == [], "retried a stream that had already completed"
    assert outcome.completed is True
    assert outcome.recovered is False
    assert outcome.error is None


def test_unit_stream_guard_heartbeat_logs_a_long_stream(monkeypatch):
    """A stream still emitting frames across many seconds logs a heartbeat.

    The silence watchdog catches a hidden-reasoning model that never speaks;
    the heartbeat complements it for a stream that keeps flowing -- an operator
    tailing the log sees "still streaming" every _HEARTBEAT_INTERVAL_S and can
    tell a long live reasoning token from a hung connection without crossing
    into the ledger. A short stream that completes inside the interval streams
    quietly (no heartbeat)."""
    from routing import stream_guard

    class _CaptureLog:
        def __init__(self):
            self.records = []

        def info(self, fmt, *a, **k):
            try:
                self.records.append(fmt % a if a else fmt)
            except Exception:
                self.records.append(fmt)

        warning = info  # noqa: E702 - mirror the logger interface

    def gen(n):
        for _ in range(n):
            yield b'data: {"choices":[{"delta":{"content":"chunk"}}]}'
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
        yield b"data: [DONE]"

    def noop_open():
        yield b"data: [DONE]"

    # Interval cranked to zero so every frame fires a heartbeat (no real sleeps).
    monkeypatch.setattr(stream_guard, "_HEARTBEAT_INTERVAL_S", 0.0)
    cap = _CaptureLog()
    outcome = stream_guard.StreamOutcome()
    list(stream_guard.guarded_stream(
        open_stream=noop_open, first=gen(5), outcome=outcome,
        on_chunk=lambda raw: None, log=cap,
        model_id="muse-spark-1.2-contributor-free",
    ))
    beats = [m for m in cap.records if "still streaming" in m]
    assert beats, cap.records
    assert any(
        "muse-spark-1.2-contributor-free" in m and "attempt=1" in m for m in beats
    ), beats
    assert outcome.completed is True

    # A short stream under a large interval completes before any heartbeat fires.
    monkeypatch.setattr(stream_guard, "_HEARTBEAT_INTERVAL_S", 9999.0)
    cap2 = _CaptureLog()
    outcome2 = stream_guard.StreamOutcome()
    list(stream_guard.guarded_stream(
        open_stream=noop_open, first=gen(2), outcome=outcome2,
        on_chunk=lambda raw: None, log=cap2,
        model_id="muse-spark-1.2-contributor-free",
    ))
    assert not any("still streaming" in m for m in cap2.records), cap2.records
    assert outcome2.completed is True


def test_unit_stream_guard_opencode_usage_frame_is_terminal():
    """OpenCode's free stream (MuseSpark) ends without finish_reason or [DONE]:
    content chunks carry finish_reason:null, then a `choices:[]`+`usage` frame
    and a trailing `cost` frame, then a clean close. That completion must be
    recognized so the stream is NOT misread as a mid-flight break and retried
    (which would emit a reset frame and regenerate the entire answer, doubling
    request/egress spend and delivering garbled doubled content to the client).
    """
    from routing import stream_guard

    calls = []

    def opencode():
        # cushion/preamble frame, no content, no usage, no finish_reason:
        yield b'data: {"choices":[]}'
        # content, finish_reason:null as on the live wire:
        yield b'data: {"choices":[{"delta":{"content":"Hi there"},"finish_reason":null}]}'
        yield b'data: {"choices":[{"delta":{"content":"!"},"finish_reason":null}]}'
        # OpenCode's terminal usage frame, then its trailing cost frame:
        yield b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":4}}'
        yield b'data: {"choices":[],"cost":"0"}'

    def should_not_run():
        calls.append(1)
        yield b"data: [DONE]"

    seen = []
    outcome = stream_guard.StreamOutcome()
    frames = list(stream_guard.guarded_stream(
        open_stream=should_not_run, first=opencode(), outcome=outcome,
        on_chunk=seen.append, log=_QuietLog(),
    ))
    assert calls == [], "a completed OpenCode stream must not be retried"
    assert outcome.completed is True
    assert outcome.attempts == 1
    assert outcome.recovered is False
    assert outcome.error is None
    assert not any(b"lingling_reset" in f for f in frames), "no reset on completion"
    # Content reached the client (split across two finish_reason:null frames,
    # so check the pieces); usage/cost trailing frames forwarded verbatim.
    body = b"".join(frames)
    assert b'"content":"Hi there"' in body and b'"content":"!"' in body
    assert b'"usage":' in body and b'"cost":"0"' in body
    assert all(s in seen for s in (
        b'data: {"choices":[]}',
        b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":4}}',
    ))


def test_unit_stream_guard_cushion_frame_alone_is_not_terminal():
    """An empty-choices cushion frame on its own (the preamble OpenCode sends
    before content) is NOT terminal: a stream that only yields it then dies has
    not finished, and must still retry. Guards against over-triggering
    completion on the preamble."""
    from routing import stream_guard

    # The function names usage/cost/finish_reason as terminal, never a bare
    # empty-choices array (OpenCode sends several as a preamble before content).
    assert stream_guard.chunk_is_terminal({"choices": []}) is False
    assert stream_guard.chunk_is_terminal({"choices": [{"finish_reason": None}]}) is False
    assert stream_guard.chunk_is_terminal({
        "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}
    }) is True
    assert stream_guard.chunk_is_terminal({"choices": [], "cost": "0"}) is True
    assert stream_guard.chunk_is_terminal({
        "choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]
    }) is True


def test_unit_stream_guard_chunk_reasons_helper():
    """chunk_reasons recognizes reasoning/thinking tokens in any of the shapes
    gateways ship (reasoning_content, reasoning, thinking -- str or dict), and
    ignores a bare false/empty value. This helper is how a hidden-reasoning
    model gets *learned* from its wire behaviour so it self-adapts."""
    from routing import stream_guard as sg
    assert sg.chunk_reasons({"choices": [{"delta": {"reasoning_content": "hmm"}}]}) is True
    assert sg.chunk_reasons({"choices": [{"delta": {"reasoning": "thinking..."}}]}) is True
    assert sg.chunk_reasons({"choices": [{"delta": {"thinking": {"text": "x"}}}]}) is True
    assert sg.chunk_reasons({"choices": [{"message": {"reasoning_content": "hmm"}}]}) is True
    # Not reasoning-in-progress:
    assert sg.chunk_reasons({"choices": [{"delta": {"reasoning": False}}]}) is False
    assert sg.chunk_reasons({"choices": [{"delta": {"reasoning": {}}}]}) is False
    assert sg.chunk_reasons({"choices": [{"delta": {"content": "answer"}}]}) is False
    assert sg.chunk_reasons({"choices": []}) is False
    assert sg.chunk_reasons({}) is False


def test_unit_stream_guard_marks_pacing_on_reasoning_chunk():
    """A stream emitting reasoning tokens learns the model as reasoning, so the
    next turn grants it thinking patience even when its listing does not
    advertise reasoning (the dynamic-adapt path for a future MuseSpark-class
    model). model_id carries the resolved target."""
    from routing import stream_guard, pacing_memory

    pacing_memory.reset_for_test()
    target = "future-hidden-reasoning-free"

    def streams():
        yield b'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}'
        yield b'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}'

    outcome = stream_guard.StreamOutcome()
    list(stream_guard.guarded_stream(
        open_stream=lambda: iter([b"data: [DONE]"]), first=streams(),
        outcome=outcome, on_chunk=lambda raw: None, log=_QuietLog(),
        model_id=target,
    ))
    assert outcome.completed is True
    assert outcome.attempts == 1
    assert pacing_memory.is_reasoning(target) is True


def test_unit_stream_guard_marks_pacing_on_stall_before_content():
    """A stream that trips the idle watchdog before emitting any visible content
    is the signature of a hidden-reasoning model thinking through its first
    token. It is learned *before* the retry re-derives pacing, so the retry gets
    the thinking patience and can wait the silence out."""
    from routing import stream_guard, stream_idle, pacing_memory

    pacing_memory.reset_for_test()
    target = "silent-thinker-free"

    def stalls():
        # Preamble only (no visible content), then the watchdog fires.
        yield b'data: {"choices":[]}'
        raise stream_idle.StreamStalled(seconds=90.0, frames=1)

    def completes():
        yield b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}'

    outcome = stream_guard.StreamOutcome()
    list(stream_guard.guarded_stream(
        open_stream=completes, first=stalls(),
        outcome=outcome, on_chunk=lambda raw: None, log=_QuietLog(),
        model_id=target,
    ))
    assert outcome.completed is True
    assert outcome.attempts == 2
    assert outcome.recovered is True
    assert pacing_memory.is_reasoning(target) is True


def test_unit_stream_guard_no_mark_on_stall_after_content():
    """A stall mid-content (the model already spoke) is NOT hidden-thinking, so
    it must not be learned -- otherwise a one-time mid-stream stall would loosen
    that model's first-token failover for nothing. text_chars>0 is the gate."""
    from routing import stream_guard, stream_idle, pacing_memory

    pacing_memory.reset_for_test()
    target = "ordinary-model"

    def stalls():
        yield b'data: {"choices":[{"delta":{"content":"partial answer"}}]}'
        raise stream_idle.StreamStalled(seconds=90.0, frames=1)

    def completes():
        yield b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}'

    outcome = stream_guard.StreamOutcome()
    list(stream_guard.guarded_stream(
        open_stream=completes, first=stalls(),
        outcome=outcome, on_chunk=lambda raw: None, log=_QuietLog(),
        model_id=target,
    ))
    assert outcome.completed is True
    assert outcome.recovered is True
    assert pacing_memory.is_reasoning(target) is False, \
        "mid-content stall must not learn the model as reasoning"


def test_unit_stream_guard_no_mark_without_model_id():
    """Without a model_id there is nothing to learn; reasoning tokens are
    observed but dropped (a direct/passthrough stream has no model to mark)."""
    from routing import stream_guard, pacing_memory

    pacing_memory.reset_for_test()

    def streams():
        yield b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}'
        yield b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":"stop"}]}'

    outcome = stream_guard.StreamOutcome()
    list(stream_guard.guarded_stream(
        open_stream=lambda: iter([b"data: [DONE]"]), first=streams(),
        outcome=outcome, on_chunk=lambda raw: None, log=_QuietLog(),
        # model_id omitted on purpose
    ))
    assert outcome.completed is True
    assert pacing_memory.snapshot() == []


def test_unit_stream_guard_no_mark_on_clean_completion():
    """A plain non-reasoning completion teaches nothing -- the registry is fed
    only by observed reasoning, not by every stream that finishes."""
    from routing import stream_guard, pacing_memory

    pacing_memory.reset_for_test()
    target = "plain-model"

    def streams():
        yield b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}'

    outcome = stream_guard.StreamOutcome()
    list(stream_guard.guarded_stream(
        open_stream=lambda: iter([b"data: [DONE]"]), first=streams(),
        outcome=outcome, on_chunk=lambda raw: None, log=_QuietLog(),
        model_id=target,
    ))
    assert outcome.completed is True
    assert pacing_memory.is_reasoning(target) is False


def test_unit_stream_pacing_body_asks_reasoning():
    """_stream_pacing grants patience and LEARNS a model when the request body
    asks for reasoning -- the fallback for a hidden-reasoning model whose
    listing does not advertise it and whose client never sends reasoning tokens
    but does ask for thinking. Covers the OpenAI/Codex/Anthropic vocabularies.
    """
    from app import _stream_pacing, _body_asks_reasoning
    from routing import pacing_memory

    pacing_memory.reset_for_test()
    target = "soon-learned-free"
    # Confirm helper detects every vocabulary:
    assert _body_asks_reasoning({"reasoning_effort": "high"}) is True
    assert _body_asks_reasoning({"reasoning": {"effort": "high"}}) is True
    assert _body_asks_reasoning({"reasoning": True}) is True
    assert _body_asks_reasoning({"thinking": {"type": "enabled"}}) is True
    assert _body_asks_reasoning({"thinking": {"type": "adaptive"}}) is True
    assert _body_asks_reasoning({"thinking": {"budget_tokens": 1024}}) is True
    assert _body_asks_reasoning({}) is False
    assert _body_asks_reasoning({"thinking": {"type": "disabled"}}) is False
    # Pre-mark: not in override/catalog, empty registry, no body -> default.
    idle_no_body, read_no_body = _stream_pacing(target, body=None)
    assert pacing_memory.is_reasoning(target) is False
    # With a reasoning body: patient now AND learned for next time.
    idle_body, read_body = _stream_pacing(target, body={"reasoning_effort": "high"})
    assert read_body > read_no_body  # the thinking patience raised the read timeout
    assert pacing_memory.is_reasoning(target) is True
    # Next turn, even without the body, the learned entry grants patience:
    idle_next, read_next = _stream_pacing(target, body=None)
    assert read_next == read_body


def test_unit_stream_guard_gives_up_after_one_retry():
    """Two failures in a row end the stream; no infinite retry loop."""
    from routing import stream_guard

    def dying():
        yield b'data: {"choices":[{"delta":{"content":"x"}}]}'
        raise UpstreamError(504, "dropped", "opencode")

    outcome = stream_guard.StreamOutcome()
    frames = list(stream_guard.guarded_stream(
        open_stream=dying, first=dying(), outcome=outcome,
        on_chunk=lambda raw: None, log=_QuietLog(),
    ))
    assert outcome.attempts == 2, outcome.attempts
    assert outcome.completed is False
    assert outcome.error, "a doubly-failed stream must record why"
    assert sum(1 for f in frames if b"lingling_reset" in f) == 1


def test_unit_stream_guard_respects_opt_out():
    """enabled=False keeps the old truncate-and-stop behaviour."""
    from routing import stream_guard

    calls = []

    def dying():
        yield b'data: {"choices":[{"delta":{"content":"x"}}]}'
        raise UpstreamError(504, "dropped", "opencode")

    def should_not_run():
        calls.append(1)
        yield b"data: [DONE]"

    outcome = stream_guard.StreamOutcome()
    frames = list(stream_guard.guarded_stream(
        open_stream=should_not_run, first=dying(), outcome=outcome,
        on_chunk=lambda raw: None, log=_QuietLog(), enabled=False,
    ))
    assert calls == []
    assert outcome.recovered is False
    assert outcome.error
    assert not any(b"lingling_reset" in f for f in frames)


def test_unit_stream_guard_forwards_every_chunk_verbatim():
    """on_chunk sees the raw bytes, and yielded frames are unmodified."""
    from routing import stream_guard

    seen = []
    payload = [
        b'data: {"choices":[{"delta":{"content":"one "}}]}',
        b'data: {"choices":[{"delta":{"content":"two"},"finish_reason":"stop"}]}',
        b"data: [DONE]",
    ]
    outcome = stream_guard.StreamOutcome()
    frames = list(stream_guard.guarded_stream(
        open_stream=lambda: iter(()), first=iter(payload), outcome=outcome,
        on_chunk=seen.append, log=_QuietLog(),
    ))
    assert seen == payload, "usage harvesting must see every raw frame"
    assert frames == [f + b"\n\n" for f in payload], "frames must pass through verbatim"
    assert outcome.text_chars == len("one ") + len("two")


def test_unit_stream_guard_client_disconnect_does_not_retry():
    """A client hanging up must not trigger a recovery attempt.

    When the browser aborts, Starlette closes the generator, raising
    GeneratorExit at the yield. That inherits BaseException rather than
    Exception, so the recovery handler must not see it -- otherwise every
    cancelled request would burn a second upstream call for an answer nobody is
    listening to.
    """
    from routing import stream_guard

    calls = []

    def slow():
        yield b'data: {"choices":[{"delta":{"content":"one"}}]}'
        yield b'data: {"choices":[{"delta":{"content":"two"}}]}'

    def should_not_run():
        calls.append(1)
        yield b"data: [DONE]"

    outcome = stream_guard.StreamOutcome()
    gen = stream_guard.guarded_stream(
        open_stream=should_not_run, first=slow(), outcome=outcome,
        on_chunk=lambda raw: None, log=_QuietLog(),
    )
    next(gen)          # consume one frame, as a client would
    gen.close()        # then hang up
    assert calls == [], "a client disconnect must not trigger a retry"
    assert outcome.recovered is False


def test_unit_responses_bridge_request_maps_codex_shape_to_chat():
    """Codex's Responses request becomes one chat history plus chat tools."""
    from routing import responses_bridge

    body = {
        "model": "lingling-auto",
        "instructions": "system rules",
        "input": [
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "dev"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call", "call_id": "call_1", "name": "shell_command", "arguments": "{\"command\":\"pwd\"}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "C:/repo"},
        ],
        "tools": [{
            "type": "function", "name": "shell_command", "description": "run",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
        }],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "max_output_tokens": 123,
    }
    model, messages, params = responses_bridge.request_to_chat(body)
    assert model == "lingling-auto"
    assert messages[0] == {"role": "system", "content": "system rules"}
    assert messages[1] == {"role": "system", "content": "dev"}
    assert messages[2] == {"role": "user", "content": "hi"}
    assert messages[3]["tool_calls"][0]["function"]["name"] == "shell_command"
    assert messages[4] == {"role": "tool", "tool_call_id": "call_1", "content": "C:/repo"}
    assert params["tools"][0]["function"]["name"] == "shell_command"
    assert params["max_tokens"] == 123
    assert params["tool_choice"] == "auto"


def test_unit_responses_bridge_stream_events_for_text_and_tool_calls():
    """Chat SSE chunks become the Responses events Codex 0.144 accepts."""
    from routing import responses_bridge

    chat = iter([
        b'data: {"choices":[{"delta":{"content":"he"}}]}',
        b'data: {"choices":[{"delta":{"content":"llo"}}]}',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"call_x","function":{"name":"shell_command","arguments":"{\\\"command\\\":"}}]}}]}',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"function":{"arguments":"\\\"pwd\\\"}"}}]}}]}',
        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":7,"completion_tokens":3}}',
        b"data: [DONE]",
    ])
    body = b"".join(responses_bridge.stream_events(chat, "m")).decode()
    assert "event: response.created" in body
    assert "event: response.output_text.delta" in body
    assert '"delta":"he"' in body and '"text":"hello"' in body
    assert "event: response.function_call_arguments.done" in body
    assert '"call_id":"call_x"' in body
    assert '"input_tokens":7' in body and '"output_tokens":3' in body

def test_unit_responses_bridge_keeps_images_and_indexes_items():
    """Two regressions the first bridge draft had, both silent.

    1. An `input_image` part was dropped, so a screenshot turn reached the
       provider as text and `vision_bridge` routed it to a text-only model.
    2. A narrated tool call put the message and the function_call both at
       `output_index` 0, so the two items collided in the client's table.
    """
    from routing import responses_bridge
    from models.vision_bridge import messages_have_images

    _, messages, _ = responses_bridge.request_to_chat({
        "model": "m",
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "what is in this shot"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
        ]}],
    })
    assert messages_have_images(messages), messages
    parts = messages[0]["content"]
    assert parts[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}

    chat = iter([
        b'data: {"choices":[{"delta":{"content":"let me look"}}]}',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_a","function":{"name":"shell","arguments":"{}"}}]}}]}',
        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
    ])
    frames = [f.decode() for f in responses_bridge.stream_events(chat, "m")]
    added = [f for f in frames if "response.output_item.added" in f]
    assert len(added) == 2, added
    assert '"output_index":0' in added[0] and '"type":"message"' in added[0]
    assert '"output_index":1' in added[1] and '"type":"function_call"' in added[1]


def test_unit_responses_bridge_reports_a_truncated_stream():
    """A stream that stops without finish_reason must not be filed as success.

    `/v1/responses` has no mid-flight retry (the Responses protocol has no
    equivalent of the `lingling_reset` frame), so the only honest thing to do is
    record the turn as broken. `outcome.completed` is what the endpoint reads.
    """
    from routing import responses_bridge
    from routing.stream_guard import StreamOutcome

    outcome = StreamOutcome()
    truncated = iter([b'data: {"choices":[{"delta":{"content":"half"}}]}'])
    body = b"".join(responses_bridge.stream_events(truncated, "m", outcome)).decode()
    assert outcome.completed is False
    assert '"status":"completed"' in body, "the client still needs a terminal event"

    outcome2 = StreamOutcome()
    whole = iter([
        b'data: {"choices":[{"delta":{"content":"all of it"},"finish_reason":"stop"}]}',
        b"data: [DONE]",
    ])
    b"".join(responses_bridge.stream_events(whole, "m", outcome2))
    assert outcome2.completed is True


def test_unit_effort_maps_harness_labels_by_rank():
    """Harness vocabularies only line up through a shared rank scale.

        Codex        none  minimal  low  medium  high  xhigh  max  ultra
        Claude Code                 low  medium  high  xhigh  max  ultracode

    A name means nothing on its own -- `ultra` is Codex's ceiling, `ultracode` is
    Claude Code's, and neither exists in OpenCode's vocabulary. normalize() maps
    a label to a canonical word; rank is what lets it cross the boundary.
    """
    from routing import effort

    for label in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
        assert effort.normalize(label) == label
    assert effort.normalize("ultra") == "ultra"
    assert effort.normalize("ultracode") == "ultracode"
    assert effort.normalize("  XHigh ") == "xhigh", "case and padding must not matter"

    # Unrecognised or non-string -> None, meaning "send no parameter at all".
    assert effort.normalize("turbo-mega") is None
    assert effort.normalize("") is None
    assert effort.normalize(None) is None
    # Anthropic's deprecated numeric thinking budget is not an effort label.
    assert effort.normalize(31999) is None

    # ultra pins to the ceiling; ultracode is xhigh-level depth because the
    # sub-agent orchestration half cannot cross a provider boundary.
    assert effort.clamp("ultra", ["low", "high", "max"]) == "max"
    assert effort.clamp("ultracode", ["low", "medium", "xhigh"]) == "xhigh"


def test_unit_effort_clamps_to_each_models_published_values():
    """Effort values come from models.dev, and every model publishes a different set.

    OpenCode answers 200 for a value a model does not implement, silently
    ignoring it -- so a value must be translated to something the model really
    honours, and dropped entirely when it honours nothing. The sets below are the
    live ones (`reasoning_options`), the same data OpenCode's CLI shows under
    `/variants`.
    """
    from routing import effort

    deepseek = ["low", "high", "max"]       # + an on/off toggle
    ling = ["low", "medium", "high"]        # no max
    north = ["none", "high"]                # sparse, non-contiguous
    mimo = []                               # no effort control at all

    # An exact match always passes through untouched.
    assert effort.resolve("high", deepseek) == "high"
    assert effort.resolve("medium", ling) == "medium"

    # Below a model's floor clamps up to its weakest rung: `low` on deepseek.
    assert effort.resolve("none", deepseek) == "low"
    assert effort.resolve("low", deepseek) == "low"
    assert effort.resolve("minimal", deepseek) == "low"
    assert effort.resolve("none", ling) == "low"

    # Above a model's ceiling clamps down: ling has no `max`.
    assert effort.resolve("max", ling) == "high"
    assert effort.resolve("ultra", ling) == "high"

    # A sparse set resolves by rank, not by list position.
    assert effort.resolve("medium", north) == "high", "0.50 is nearer 0.70 than 0.0"
    assert effort.resolve("low", north) == "none", "0.30 is nearer 0.0 than 0.70"

    # Equidistant resolves to the weaker value, so a translation never spends
    # more thinking than was asked for. `medium` is exactly midway between
    # `low` (0.30) and `high` (0.70); raw float arithmetic makes `high` look
    # closer (0.7-0.5 == 0.19999999999999996 < 0.2) and must not win.
    assert effort.resolve("ultracode", deepseek) == "high", "0.85 sits midway; take the floor"
    assert effort.resolve("medium", deepseek) == "low", "midpoint tie must take the weaker rung"

    # A model with no effort control must never be sent the parameter -- sending
    # one would be accepted and ignored, which looks like success.
    for label in ("none", "low", "high", "max", "ultra", "ultracode"):
        assert effort.resolve(label, mimo) is None, label
    assert effort.resolve("high", None) is None, "unknown model data is not a licence to guess"


def test_unit_effort_sets_come_from_models_dev_not_a_hardcoded_table():
    """The per-model sets must be read live, so new free models arrive correct.

    An earlier version of this module probed the endpoint to build its own table
    and was wrong for six of seven free models, because OpenCode's 200 response
    cannot distinguish "honoured" from "silently ignored".
    """
    from models import metadata
    from providers.opencode import FREE_MODEL_CAPS

    for caps in FREE_MODEL_CAPS.values():
        assert "effort" not in caps, "effort must not be hardcoded per model"

    meta = {"reasoning_options": [
        {"type": "toggle"},
        {"type": "effort", "values": ["high", "max"]},
    ]}
    assert metadata.reasoning_effort_values(meta) == ["high", "max"]
    assert metadata.reasoning_toggle(meta) is True

    # A model with no reasoning_options exposes no effort control.
    assert metadata.reasoning_effort_values({}) == []
    assert metadata.reasoning_toggle({}) is False
    # A toggle alone is not an effort level -- it is a separate on/off control.
    assert metadata.reasoning_effort_values({"reasoning_options": [{"type": "toggle"}]}) == []


def test_unit_codex_reasoning_block_becomes_chat_reasoning_effort():
    """Codex nests effort under `reasoning`; chat wants it flat.

    The bridge passes the label through unresolved on purpose -- the legal set
    depends on which model routing picks, which is not known this early.
    """
    from routing import responses_bridge

    _, _, params = responses_bridge.request_to_chat({
        "model": "lingling-auto",
        "input": "hi",
        "reasoning": {"effort": "xhigh", "summary": "auto"},
    })
    assert params["reasoning_effort"] == "xhigh"

    # No reasoning block, or a block without effort: nothing is invented.
    _, _, bare = responses_bridge.request_to_chat({"model": "m", "input": "hi"})
    assert "reasoning_effort" not in bare
    _, _, summary_only = responses_bridge.request_to_chat({
        "model": "m", "input": "hi", "reasoning": {"summary": "auto"},
    })
    assert "reasoning_effort" not in summary_only


def _codex_template():
    """A stand-in for one model out of `codex debug models`.

    Same 35-field shape, with the fields the generator reads or must preserve.
    Not the real 20 KB prompt -- the point is that whatever the template holds
    survives into every entry, which a short marker string proves just as well.
    """
    return {
        "slug": "gpt-5.5",
        "display_name": "GPT-5.5",
        "description": "Latest frontier agentic coding model.",
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [
            {"effort": "low", "description": "Fast responses with lighter reasoning"},
            {"effort": "medium", "description": "Balances speed and reasoning depth"},
            {"effort": "high", "description": "Greater reasoning depth"},
            {"effort": "xhigh", "description": "Extra high reasoning depth"},
        ],
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 7,
        "additional_speed_tiers": ["fast"],
        "service_tiers": [{"id": "priority", "name": "Fast", "description": "1.5x speed"}],
        "availability_nux": {"message": "GPT-5.5 is now available in Codex."},
        "base_instructions": "You are Codex, a coding agent based on GPT-5. <the entire harness>",
        "model_messages": {"instructions_template": "...", "approvals": None},
        "default_reasoning_summary": "none",
        "support_verbosity": True,
        "default_verbosity": "low",
        "apply_patch_tool_type": "freeform",
        "web_search_tool_type": "text_and_image",
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "supports_parallel_tool_calls": True,
        "supports_image_detail_original": False,
        "context_window": 272000,
        "max_context_window": 272000,
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [],
        "input_modalities": ["text", "image"],
        "supports_search_tool": True,
        "use_responses_lite": False,
    }


def _lingling_model(model_id, effort_values, context=200000, vision=False):
    """A Lingling catalog model in `LogicalModel.to_dict()` shape."""
    return {
        "id": model_id,
        "name": model_id.replace("-", " ").title(),
        "free": True,
        "vision": vision,
        "reasoning": True,
        "context_length": context,
        "max_output": 32000,
        "modalities": ["text", "image"] if vision else ["text"],
        "providers": ["opencode"],
        "capabilities": {"effort": effort_values, "desc": "a free model"},
        "description": "a free model",
    }


class _RoutingModel:
    """A catalog model with just the fields the router reads."""

    def __init__(self, model_id, desc, vision=False, context_length=200_000,
                 reasoning=True, providers=("opencode",)):
        self.id = model_id
        self.vision = vision
        self.reasoning = reasoning
        self.context_length = context_length
        self.max_output = 32000
        self.modalities = ["text", "image"] if vision else ["text"]
        self.provider_ids = list(providers)
        self.capabilities = {"desc": desc, "effort": []}


class _RoutingCatalog:
    """Minimal catalog stand-in for `fallback_model`."""

    def __init__(self, models):
        self._models = models

    def free(self):
        return list(self._models)

    def vision_free(self):
        return [m for m in self._models if m.vision]


def test_unit_the_fallback_routes_on_the_request_not_on_the_router_model():
    """A dispatcher outage must still send the work to a suitable model.

    `fallback_model` returned `DISPATCHER_MODEL` whenever that model was in the
    pool, so all three no-decision paths -- dispatcher unreachable, unparseable
    reply, hallucinated id -- ignored the request entirely. A screenshot went to a
    text-only model that cannot see it; a refactor went to a general chat model
    instead of the code specialist; `hi` spent a deep-reasoning model.
    """
    from routing import dispatcher

    cat = _RoutingCatalog([
        _RoutingModel("mimo-v2.5-free", vision=True,
                      desc="multimodal: understands images/screenshots; the ONLY free vision model"),
        _RoutingModel("north-mini-code-free",
                      desc="specialized for code generation, refactoring and software engineering"),
        _RoutingModel("nemotron-3-ultra-free", context_length=1_000_000,
                      desc="tuned for deep multi-step reasoning, math and planning"),
        _RoutingModel("ling-3.0-flash-free",
                      desc="balanced general-purpose chat and light coding; fast"),
        _RoutingModel("deepseek-v4-flash-free",
                      desc="huge context, deep thinking mode, excellent at coding"),
    ])

    def pick(text, has_images=False):
        msgs = [{"role": "user", "content": text}]
        return dispatcher.fallback_model(cat, has_images, messages=msgs)

    # An image can only go to the model that can see it.
    assert pick("what is wrong here", has_images=True) == "mimo-v2.5-free"
    # Writing code goes to the code specialist, not a general chat model.
    assert pick("Refactor this component to use hooks") == "north-mini-code-free"
    assert pick("Write a unit test for the parser") == "north-mini-code-free"
    # Diagnosis and design are reasoning work even when the subject is code.
    assert pick("Why does the deploy keep failing?") == "nemotron-3-ultra-free"
    assert pick("Design a schema for multi-tenant billing") == "nemotron-3-ultra-free"
    # A greeting must not spend the heaviest model available.
    assert pick("hi") == "ling-3.0-flash-free"
    # Frontend and backend are both code work.
    assert pick("Build me a login page with Tailwind") == "north-mini-code-free"
    assert pick("Add a POST /users endpoint") == "north-mini-code-free"
    # Explanation stays general even when the subject is code -- the topic does
    # not decide this, the kind of work does.
    assert pick("Explain what a closure is") == "ling-3.0-flash-free"

    # `exclude` is still honoured, so model-level failover moves on.
    msgs = [{"role": "user", "content": "Refactor this component"}]
    second = dispatcher.fallback_model(
        cat, False, exclude={"north-mini-code-free"}, messages=msgs)
    assert second != "north-mini-code-free", second

    # And calling it with no messages at all must not raise -- the signature is
    # public and the argument is optional.
    assert dispatcher.fallback_model(cat, False) in {m.id for m in cat.free()}


def test_unit_a_reason_with_camelcase_words_is_not_mangled():
    """`_clean_reason` must not insert spaces into real words.

    It split on every lowercase->uppercase boundary to repair run-together output
    from a free model. Applied unconditionally that corrupted ordinary sentences,
    and routing reasons are full of exactly the words it breaks: `TypeScript` ->
    `Type Script`, `GitHub` -> `Git Hub`, `iOS` -> `i OS`. The reason is shown in
    the dashboard and mirrored into a response header, so it is user-visible.
    """
    from routing.dispatcher import _clean_reason

    for text in (
        "Chose north-mini-code-free for TypeScript refactoring.",
        "Routed here because the iOS build fails.",
        "Picked this model for GitHub Actions work.",
    ):
        assert _clean_reason(text) == text, _clean_reason(text)

    # Genuinely run-together text is still repaired.
    assert " " in _clean_reason("ThisIsACodingQuestion")
    # The empty cases stay safe.
    assert _clean_reason("") == "no reason given"
    assert _clean_reason("   ") == "no reason given"
    # A closing period is added; an existing terminator is left alone.
    assert _clean_reason("fast model chosen") == "Fast model chosen."
    assert _clean_reason("why not?") == "Why not?"


def test_unit_codex_catalog_declares_effort_so_codex_stops_nulling_it():
    """Codex only sends `reasoning.effort` for a model in its own catalog.

    Verified against a capture proxy: with Codex's stock catalog a request for
    `deepseek-v4-flash-free` carries `"reasoning": null` whatever
    `model_reasoning_effort` says. Declared with a non-empty
    `supported_reasoning_levels`, the same request carries
    `{"effort": "high", "summary": "auto"}`.

    So the generator's contract is: every listed model carries at least one level,
    because an empty list gets the field nulled again -- exactly the broken state
    this module exists to fix. A model publishing no options gets the `default`
    rung, which resolves to "send no effort parameter".
    """
    from models import codex_catalog

    catalog_json = codex_catalog.build(
        _codex_template(),
        [
            _lingling_model("deepseek-v4-flash-free", ["high", "max"], context=200000),
            _lingling_model("north-mini-code-free", ["none", "high"], context=256000),
            _lingling_model("mimo-v2.5-free", [], context=262144, vision=True),
        ],
    )
    listed = {m["slug"]: m for m in catalog_json["models"]}
    assert set(listed) == {"deepseek-v4-flash-free", "north-mini-code-free",
                           "mimo-v2.5-free"}, sorted(listed)
    for slug, entry in listed.items():
        assert entry["supported_reasoning_levels"], slug
        # Codex 0.146 replaced the boolean `supports_reasoning_summaries` with
        # the string `default_reasoning_summary`; entries inherit it from the
        # template and must not resurrect the stale key.
        assert entry.get("default_reasoning_summary") in ("none", "auto", "hidden"), slug
        assert "supports_reasoning_summaries" not in entry, \
            f"{slug}: stale pre-0.146 key must not be written"

    # models.dev is third-party data, so build() must survive shapes its schema
    # does not promise: a bare number where a list belongs, a duplicate id, a
    # blank id. None of these may raise or produce a duplicate slug.
    hostile = codex_catalog.build(_codex_template(), [
        {"id": "junk-effort", "name": "J", "capabilities": {"effort": 42}},
        {"id": "junk-str", "name": "J", "capabilities": {"effort": "high"}},
        {"id": "", "name": "blank", "capabilities": {"effort": ["high"]}},
        _lingling_model("dupe", ["high"]),
        _lingling_model("dupe", ["max"]),
    ])
    slugs = [m["slug"] for m in hostile["models"]]
    assert "" not in slugs, "an empty model id must never become a slug"
    assert len(slugs) == len(set(slugs)), f"duplicate slugs: {slugs}"
    assert "dupe" in slugs and slugs.count("dupe") == 1

    # Levels are the model's published set, in order, with picker copy attached.
    assert [lv["effort"] for lv in listed["deepseek-v4-flash-free"]["supported_reasoning_levels"]] \
        == ["high", "max"]
    assert [lv["effort"] for lv in listed["north-mini-code-free"]["supported_reasoning_levels"]] \
        == ["none", "high"]
    assert all(lv["description"] for lv in listed["north-mini-code-free"]["supported_reasoning_levels"]), \
        "Codex's parser rejects a level with no description"

    # The picker starts on the weakest rung, never above what was asked for.
    assert listed["deepseek-v4-flash-free"]["default_reasoning_level"] == "high"
    assert listed["north-mini-code-free"]["default_reasoning_level"] == "none"


def test_unit_codex_catalog_entries_clone_a_real_codex_model():
    """An entry is a real Codex model with its identity swapped, not a fresh dict.

    `base_instructions` is the whole Codex agent prompt (11-21 KB). A hand-built
    entry with a short one parses fine and quietly makes Codex a much dumber
    agent, because that string *is* the harness. Everything the generator has no
    opinion about must therefore survive untouched -- including fields it has
    never heard of, so the file stays valid when Codex adds some.
    """
    from models import codex_catalog

    template = _codex_template()
    template["some_future_codex_field"] = {"added": "by a later release"}
    entry = codex_catalog.entry_for(
        template, "ling-3.0-flash-free", "Ling 3.0 Flash Free",
        ["low", "medium", "high"], context_length=262144,
    )

    assert entry["base_instructions"] == template["base_instructions"]
    assert entry["shell_type"] == template["shell_type"]
    assert entry["some_future_codex_field"] == {"added": "by a later release"}
    assert entry["slug"] == "ling-3.0-flash-free"
    assert entry["context_window"] == entry["max_context_window"] == 262144

    # Never null: Codex's parser dumps null for absent optional fields but
    # rejects it on input for some of them (web_search_tool_type reproducibly
    # fails with "expected value"), so no override may write one.
    for field in ("web_search_tool_type", "apply_patch_tool_type", "shell_type",
                  "truncation_policy", "default_reasoning_level"):
        assert entry[field] is not None, field

    # A template that is not a real Codex model fails here, rather than as an
    # opaque byte-offset parse error from Codex after the file is written.
    try:
        codex_catalog.entry_for({"slug": "x", "display_name": "X"}, "m", "M", ["high"])
    except ValueError as exc:
        assert "base_instructions" in str(exc)
    else:
        raise AssertionError("an incomplete template must be rejected")

    # An empty slug is refused: it would be written verbatim and is useless to
    # Codex. An empty level list is *not* an error -- it falls back to the
    # `default` rung so the model still appears in the picker.
    try:
        codex_catalog.entry_for(template, "", "M", ["high"])
    except ValueError:
        pass
    else:
        raise AssertionError("an empty model id must be rejected")

    for levels in ([], ["banana"], 42, None):
        fallback = codex_catalog.entry_for(template, "m", "M", levels)
        assert [lv["effort"] for lv in fallback["supported_reasoning_levels"]] == ["default"], \
            f"{levels!r} should fall back to the default rung"
        assert fallback["default_reasoning_level"] == "default"


def test_unit_codex_catalog_clamps_context_window_to_the_free_relay_ceiling():
    """A model whose paper context exceeds OpenCode's free relay cap is
    advertised at the relay's actual ceiling, not the model's.

    OpenCode's free relay rejects any request whose input exceeds ~262K tokens
    -- verified against laguna-s-2.1's endpoint with
    ``[400] This endpoint's maximum context length is 262144 tokens`` -- and
    behaves identically on muse-spark (an empty ``chat.completion`` stub
    wrapped in 400). models.dev publishes the upstream model's paper context
    (muse-spark 1M, nemotron-3-ultra 1M, x-preview-f 1M), which is the model's
    own limit, NOT the relay's. Advertising the paper number in
    ``lingling_models.json`` made Codex 0.146 send an ~870K-token session
    thinking it fit; the relay 400'd every free model attempted in turn and the
    /v1/responses path fell back to a terminal 503.

    Clamping at ``codex_catalog.entry_for`` time to
    ``FREE_TIER_RELAY_CONTEXT_CAP`` (with ``min``) lets Codex compact against
    the relay's actual budget, while every sub-cap model is advertised at the
    value models.dev really publishes (so e.g. hy3-free is not bumped up to
    262144 against its real 190K budget).
    """
    from models import codex_catalog

    template = _codex_template()

    # A model whose paper context is 1M is advertised at the relay's 262K cap.
    clamped = codex_catalog.entry_for(
        template, "muse-spark-1.2-contributor-free", "Muse Spark 1.2 Contributor",
        ["minimal", "low", "medium", "high", "xhigh"], context_length=1_048_576,
    )
    assert clamped["context_window"] == clamped["max_context_window"] == 262144, \
        "paper context (1M) must clamp to the relay's actual ceiling"
    # Everything else about the entry is untouched -- this is a clamp, not a
    # rewrite, so the cloned identity / levels / instructions all survive.
    assert clamped["context_window"] == codex_catalog.FREE_TIER_RELAY_CONTEXT_CAP
    assert [lv["effort"] for lv in clamped["supported_reasoning_levels"]] \
        == ["minimal", "low", "medium", "high", "xhigh"]
    assert clamped["slug"] == "muse-spark-1.2-contributor-free"

    # A model whose published context is below the cap is advertised at its
    # real value -- the clamp doesn't bump small-context models up to the cap.
    preserved = codex_catalog.entry_for(
        template, "hy3-free", "HY3 Free",
        ["high"], context_length=190_000,
    )
    assert preserved["context_window"] == preserved["max_context_window"] == 190000, \
        "sub-cap context must be advertised exactly as published, not raised to the cap"

    # A model that sits exactly at the cap stays at the cap (min's identity).
    at_cap = codex_catalog.entry_for(
        template, "nemotron-3.5-lightning-free", "Nemotron Lightning",
        ["high"], context_length=262144,
    )
    assert at_cap["context_window"] == at_cap["max_context_window"] == 262144


def test_unit_codex_catalog_auto_entry_spans_every_routable_model():
    """`lingling-auto` cannot know its target, so it advertises the union.

    The dispatcher picks the model *after* Codex has sent the request, so the
    entry offers every level any routable model honours and Lingling clamps the
    value once the target is known. Context is the floor, not the max: Codex
    compacts against this number and the router may land on the smallest model.
    """
    from models import codex_catalog

    catalog_json = codex_catalog.build(
        _codex_template(),
        [
            _lingling_model("deepseek-v4-flash-free", ["high", "max"], context=200000),
            _lingling_model("ling-3.0-flash-free", ["low", "medium", "high"], context=262144),
            _lingling_model("north-mini-code-free", ["none", "high"], context=256000),
        ],
        auto_id="lingling-auto",
        auto_name="Lingling Auto",
    )
    auto = catalog_json["models"][0]
    assert auto["slug"] == "lingling-auto", "the router must be the first-listed model"
    assert [lv["effort"] for lv in auto["supported_reasoning_levels"]] \
        == ["none", "low", "medium", "high", "max"], "union, ordered weakest first"
    assert auto["default_reasoning_level"] == "none"
    assert auto["context_window"] == 200000, "the floor, so no routed model overflows"
    assert "image" in auto["input_modalities"], "the router can reach a vision model"

    # A pool of only dial-less models still gets a router entry: it can route to
    # them, and its own rung is the `default` stand-in.
    dialless = codex_catalog.build(
        _codex_template(),
        [_lingling_model("mimo-v2.5-free", []), _lingling_model("big-pickle", [])],
        auto_id="lingling-auto",
    )
    slugs = [m["slug"] for m in dialless["models"]]
    assert slugs[0] == "lingling-auto", slugs
    assert [lv["effort"] for lv in dialless["models"][0]["supported_reasoning_levels"]] \
        == ["default"]

    # With no models at all there is nothing to route to, so no router entry.
    assert codex_catalog.build(_codex_template(), [], auto_id="lingling-auto")["models"] == []


def test_unit_api_key_store_survives_a_truncated_write():
    """The keyring is written atomically and last_used_at is throttled.

    `_save` used to truncate the live file in place, and `validate` called it on
    every authenticated request. A crash mid-write left unparseable JSON, which
    `_load` reports as "no keys" -- silently revoking every client's access.
    """
    import importlib
    from pathlib import Path

    tmp_dir = tempfile.mkdtemp(prefix="lingling-keys-")
    os.environ["LINGLING_API_KEYS_FILE"] = os.path.join(tmp_dir, "api_keys.json")
    importlib.reload(config)
    keys_mod = importlib.reload(api_keys)
    try:
        rec = keys_mod.create_key("atomic-test")
        path = Path(os.environ["LINGLING_API_KEYS_FILE"])
        assert path.exists(), "create_key must persist"
        # No temp file is left behind after a successful rename.
        assert not list(path.parent.glob("*.tmp")), "temp file leaked"

        # First validate stamps last_used_at.
        assert keys_mod.validate(rec["token"]) is True
        stamped = keys_mod.list_keys()[0]["last_used_at"]
        assert stamped, "first validate should record last_used_at"

        # Subsequent validates inside the resolution window must not rewrite.
        mtime = path.stat().st_mtime_ns
        for _ in range(20):
            assert keys_mod.validate(rec["token"]) is True
        assert path.stat().st_mtime_ns == mtime, (
            "validate rewrote the keyring inside the throttle window")

        # A partially-written file is what the old code could leave behind.
        path.write_text('[{"id": "key_dead", "token": "ll_trunc', encoding="utf-8")
        assert keys_mod.validate(rec["token"]) is False
        assert keys_mod.list_keys() == [], "corrupt keyring reads as empty"

        # And recovery is a normal create, which rewrites the file cleanly.
        fresh = keys_mod.create_key("after-corruption")
        assert keys_mod.validate(fresh["token"]) is True
        assert len(keys_mod.list_keys()) == 1
    finally:
        os.environ["LINGLING_API_KEYS_FILE"] = os.path.join(
            os.environ["LINGLING_DATA_DIR"], "api_keys.json")
        importlib.reload(config)
        importlib.reload(api_keys)


def test_unit_usage_row_limit_is_capped():
    """`recent` and `since` bound their limit; the value comes from a query string."""
    from usage.store import UsageStore
    from usage import store as usage_mod

    store = UsageStore(os.path.join(tempfile.mkdtemp(prefix="lingling-cap-"), "u.db"))
    try:
        for _ in range(5):
            store.log("m", "m", "user", status="ok")

        # An absurd limit must not be passed through to SQLite unbounded.
        assert len(store.recent(10**9)) == 5
        assert len(store.since(0, 10**9)) == 5

        # The cap is read at call time, so lowering it proves it is applied
        # rather than the row count merely being small.
        original = usage_mod.MAX_ROWS
        usage_mod.MAX_ROWS = 3
        try:
            assert len(store.recent(10**9)) == 3, "recent ignored the cap"
            assert len(store.since(0, 10**9)) == 3, "since ignored the cap"
        finally:
            usage_mod.MAX_ROWS = original

        # Sane values are still honoured verbatim.
        assert len(store.recent(2)) == 2
        assert len(store.recent(0)) == 1, "limit floors at 1"
    finally:
        store.close()


def test_unit_stream_headers_survive_nonlatin1_reason():
    """A dispatcher reason with an em-dash/emoji must not 500 the stream response.

    Starlette encodes header values as latin-1; the streaming path mirrors the
    routing ``reason`` into X-Lingling-* headers. Model-generated reasons often
    contain typographic punctuation above U+00FF, which raised UnicodeEncodeError
    when the StreamingResponse was constructed -- a raw ASGI 500 with no ledger row.
    """
    import app as app_mod
    from starlette.responses import StreamingResponse

    # The sanitiser itself: transliterates common punctuation, replaces the rest.
    assert app_mod._header_safe("Chose deepseek\u2014it is fast.") == "Chose deepseek--it is fast."
    assert app_mod._header_safe("smart \u201cquote\u201d") == 'smart "quote"'
    got = app_mod._header_safe("emoji \U0001f600 tail")
    got.encode("latin-1")  # must not raise
    assert app_mod._header_safe("") == ""

    # Control characters are the sharper edge: CR, LF and NUL are all valid
    # latin-1, so they survived the encode and Starlette then rejected the header
    # with RuntimeError("Invalid HTTP header value") -- the response aborted and
    # the client received nothing at all. A model emitting a line break inside
    # its one-sentence reason is enough to trigger it.
    for raw in ("ok\r\nX-Injected: yes", "ok\nsecond line", "ok\rcarriage", "ok\x00nul"):
        safe = app_mod._header_safe(raw)
        assert not any(ch in safe for ch in "\r\n\x00"), f"{raw!r} -> {safe!r}"
        StreamingResponse(iter([b""]), headers={"X-Lingling-Reason": safe})

    # And the exact header dict the handler builds must construct cleanly.
    reason = app_mod._header_safe("Chose deepseek\u2014it is fast \U0001f600")

    def gen():
        yield b"data: hi\n\n"

    resp = StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={
            "X-Lingling-Routed-Model": app_mod._header_safe("deepseek-v4-flash-free"),
            "X-Lingling-Reason": reason,
        },
    )
    # raw_headers is where the latin-1 encode happens; force it.
    for _name, _value in resp.raw_headers:
        pass


def test_unit_exhausted_503_carries_provenance_headers():
    """An exhausted-503 now carries the same X-Lingling-Routed-* a success does.

    Before this, a 503 dropped provenance, so an operator chasing the "429
    prison" had to read the ledger to learn which model was tried and who chose
    it. The handler now attaches the routing story to the 503 too (model/by/
    reason -- no provider/account, since every provider refused or none was
    available), so a curl -i on the failure surfaces it the way a 200 already
    does on every success path (chat, responses and messages).
    """
    import app as app_mod

    ids = sorted({m.id for m in app_mod.catalog.free()})
    assert len(ids) >= 1, "expected at least one routable free model"
    primary = ids[0]

    def exhaust(messages, model_id, providers, **kwargs):
        # A non-retryable 400 propagates from the egress-wait wrapper without
        # parking (nothing to cool, nothing to wait for), so the handler reaches
        # its AllFailedError branch at once rather than after the wait budget.
        raise executor.AllFailedError(UpstreamError(400, "bad shape", "opencode"), [])

    orig_exec = app_mod.executor.execute_nonstream
    # No fallback: the handler's else-branch raises the single-model 503, so the
    # assertion targets the primary's prelude routing story (user / "user
    # requested") rather than a fallback that never happened.
    orig_fb = app_mod.dispatcher.fallback_model
    app_mod.executor.execute_nonstream = exhaust
    app_mod.dispatcher.fallback_model = (
        lambda catalog, has_images, exclude=None, messages=None: None
    )
    try:
        r = client.post("/v1/chat/completions", json={
            "model": primary,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
        })
        assert r.status_code == 503, r.text
        assert r.headers["X-Lingling-Routed-Model"] == primary, (
            r.headers.get("X-Lingling-Routed-Model"))
        assert r.headers["X-Lingling-Routed-By"] == "user", (
            r.headers.get("X-Lingling-Routed-By"))
        assert r.headers["X-Lingling-Reason"] == "user requested", (
            r.headers.get("X-Lingling-Reason"))
    finally:
        app_mod.executor.execute_nonstream = orig_exec
        app_mod.dispatcher.fallback_model = orig_fb


def test_unit_sampler_status_surfaces_pacing_and_active_streams():
    """/api/sampler is the dashboard's one poll for pool-health, so it surfaces
    the routing snapshots the request path consumes but the UI could not see:
    pacing_reasoning (ids pacing_memory learned reason -- a hidden reasoner's
    longer thinking patience is now effective, not just internal) and
    active_streams (in-flight counts per egress id -- busy vs idle lane). Both
    are present and correctly typed even on a fresh, idle boot, and they mirror
    the live module values (not a stale copy), so a learned model materialises in
    the next poll and the frontend can render the keys unconditionally.
    """
    import app as app_mod

    r = client.get("/api/sampler")
    assert r.status_code == 200, r.text
    body = r.json()
    # The sampler's core keys survive the enrichment (additive, not replaced),
    # in either the live branch or the disabled/pending envelope.
    assert {"enabled", "models", "canary_ok_exits"} <= set(body.keys()), body.keys()
    # The two surfaced snapshots are present and correctly typed -- empty on a
    # fresh, idle boot but never missing, so the frontend renders unconditionally.
    assert isinstance(body.get("pacing_reasoning"), list), body.get("pacing_reasoning")
    assert isinstance(body.get("active_streams"), dict), body.get("active_streams")
    # They read the live module state the endpoint imports, not a stale copy.
    assert body["pacing_reasoning"] == app_mod.pacing_memory.snapshot()
    assert body["active_streams"] == app_mod.active_streams.snapshot()


def test_unit_catalog_serves_last_good_list_through_a_fetch_failure():
    """A transient /models outage must not empty the catalog.

    The model list is fetched live (never hard-coded), but if a refresh cannot
    reach the upstream, the catalog keeps serving the previous good list rather
    than blanking out -- otherwise the dashboard/CLI picker would go empty on a
    momentary blip. A genuine empty result (fetch OK, zero models) still clears.
    """
    from models.catalog import UnifiedCatalog
    from providers.base import Provider, ProviderModel
    from providers.key_pool import KeyPool

    class FetchProvider(Provider):
        id = "opencode"
        display_name = "Fetch"
        priority = 10

        def __init__(self):
            super().__init__(KeyPool([]))
            self._ids = []
            self._raise = False

        def requires_key(self):
            return False

        def fetch_model_ids(self):
            if self._raise:
                raise RuntimeError("/models unreachable")
            return self._ids

        def is_model_free(self, model_id, meta):
            return model_id.endswith("-free")

        def build_model(self, model_id):
            return ProviderModel(
                id=model_id, provider_id=self.id, name=model_id, free=True,
                vision=False, reasoning=True, context_length=1000, max_output=100,
            )

    prov = FetchProvider()
    cat = UnifiedCatalog({"opencode": prov})

    # 1. Healthy fetch populates the catalog.
    prov._ids = ["alpha-free", "beta-free"]
    cat.refresh(force=True)
    assert sorted(m.id for m in cat.free()) == ["alpha-free", "beta-free"]
    assert cat.meta()["stale"] is False

    # 2. The next refresh fails outright -> the last good list survives.
    prov._raise = True
    cat.refresh(force=True)
    assert sorted(m.id for m in cat.free()) == ["alpha-free", "beta-free"], \
        "a failed fetch must not empty the catalog"
    assert cat.meta()["stale"] is True
    assert cat.meta()["providers"]["opencode"]["stale"] is True

    # 3. Recovery with a changed list replaces the cache (proves it is live).
    prov._raise = False
    prov._ids = ["gamma-free"]
    cat.refresh(force=True)
    assert [m.id for m in cat.free()] == ["gamma-free"]
    assert cat.meta()["stale"] is False

    # 4. A genuine empty result (fetch OK) correctly clears the catalog.
    prov._ids = []
    cat.refresh(force=True)
    assert cat.free() == [], "an honest empty list must not be masked by the cache"


def test_unit_explicit_model_resolves_from_cache_without_a_refetch():
    """A cached model must resolve without re-fetching the upstream list.

    `by_id`/`providers_for` run on every routed request. When the catalog TTL
    expired, the old code re-fetched OpenCode /models on that request path --
    one slow request every refresh window. Serving the cached view first keeps
    request latency flat; only an unknown model triggers a refresh.
    """
    from models.catalog import UnifiedCatalog
    from providers.base import Provider, ProviderModel
    from providers.key_pool import KeyPool

    class CountingProvider(Provider):
        id = "opencode"
        display_name = "Count"
        priority = 10

        def __init__(self):
            super().__init__(KeyPool([]))
            self._ids = []
            self.fetches = 0

        def requires_key(self):
            return False

        def fetch_model_ids(self):
            self.fetches += 1
            return self._ids

        def is_model_free(self, mid, meta):
            return mid.endswith("-free")

        def build_model(self, mid):
            return ProviderModel(
                id=mid, provider_id=self.id, name=mid, free=True,
                vision=False, reasoning=True, context_length=1000, max_output=100,
            )

    prov = CountingProvider()
    cat = UnifiedCatalog({"opencode": prov})
    prov._ids = ["alpha-free"]
    cat.refresh(force=True)
    assert prov.fetches == 1

    # Age the view past its TTL: the explicit-model path must still resolve
    # straight from the cache without an upstream round-trip.
    cat._generated_at = 0.0
    assert cat.by_id("alpha-free") is not None
    assert cat.providers_for("alpha-free"), "providers_for must resolve from cache"
    assert prov.fetches == 1, f"by_id re-fetched the upstream list ({prov.fetches}x)"

    # A model the cached view does not know still triggers a refresh, so a
    # brand-new upstream model resolves by name.
    prov._ids = ["alpha-free", "brand-new-free"]
    assert cat.by_id("brand-new-free") is not None
    assert prov.fetches == 2, "an unknown model must trigger a refresh"


def test_unit_hot_models_route_through_the_proxy_pool():
    """The dispatcher and the fast chat model must rotate egress, not bypass it.

    The gate is ``LINGLING_FAST_MODELS_DIRECT`` (default off): when it is off,
    ``OpenCodeProvider.prefer_direct`` returns ``False`` for *every* model, so
    the dispatcher (runs on every lingling-auto turn) and the free chat models
    the dispatcher lands on all go through the egress pool -- and the WARP rack
    counts them. The pre-fix here had the two hottest paths opted out of the
    pool via the old ``prefer_direct`` allowlist, so the rack showed 0 requests
    on every slot. This is the regression guard against flipping the flag back
    on or re-introducing the allowlist.

    The assert set is config-provided (``DISPATCHER_MODEL`` / ``MULTIMODEL_ID``
    -- env-overridable, never a pinned product literal) plus a few live free
    chat ids when the catalog is reachable, so the test stays hermetic in an
    offline env and adaptive against upstream retirements when online.
    """
    from providers.opencode import OpenCodeProvider
    from providers.key_pool import KeyPool

    prov = OpenCodeProvider(KeyPool([]))
    assert prov.needs_proxy() is True
    # The default must hold. An operator setting LINGLING_FAST_MODELS_DIRECT=1
    # opts fast models out of the pool -- this guard fails to alert them that
    # the dashboard's per-exit rack would go to zero again.
    assert config.FAST_MODELS_DIRECT is False, (
        "FAST_MODELS_DIRECT must default off; an opt-in direct route for hot "
        "models defeats the per-IP rotation the egress pool exists to provide"
    )

    # Config-provided ids first (env-overridable knobs -- never a pinned
    # product literal), then expand with a few live free chat ids the catalog
    # reports right now so the guard also covers what the dispatcher actually
    # lands on. Adaptive against upstream retirements when online; when the
    # catalog is unreachable the gate is all that matters, and the config-only
    # ids keep the assertion meaningful offline too.
    try:
        live_ids = sorted(_live_free_ids())[:3]
    except Exception:  # noqa: BLE001 -- live catalog optional here, not the gate
        live_ids = []
    hot_ids = {config.DISPATCHER_MODEL, config.MULTIMODEL_ID}
    hot_ids.update(live_ids)
    for model_id in sorted(hot_ids):
        assert prov.prefer_direct(model_id) is False, \
            f"{model_id} would bypass the egress pool"

    # And a real attempt records the proxy it used, so the rack counts it. The
    # id here is an opaque tag the executor carries through; the FakeProvider
    # ignores it -- DISPATCHER_MODEL is also env-overridable, so this is never
    # a hardcoded product literal either.
    fake = FakeProvider("opencode", CANNED, keyed=False, use_proxy=True)
    pool = ProxyPool.from_list([{"id": "p1", "url": "socks5://127.0.0.1:51001"}])
    resp, prov2, key, attempts = executor.execute_nonstream(
        [{"role": "user", "content": "hi"}], config.DISPATCHER_MODEL, [fake],
        proxy_pool=pool, session_id="",
    )
    assert resp is CANNED
    assert pool.proxies[0].total_requests == 1, \
        "a successful call must be counted against the exit it used"


def test_unit_one_session_still_spreads_across_every_exit():
    """A coding agent's session must not pin all its traffic to one exit IP.

    Codex sends a stable `session-id` header for its whole run, and sticky
    sessions were on by default -- so `pick_sticky` hashed that one id to one
    proxy and every request for hours went out through a single WARP identity.
    OpenCode meters the free tier per IP, so one identity absorbed the entire
    session's quota while the other nine sat idle. The dashboard showed it
    plainly: 8 requests on slot #2, 3 on #8, zero on the rest.

    Rotation is the default now. This is the regression guard: flip the default
    back, or return `pick_sticky` to hashing, and this fails.
    """
    from routing.executor import _pick_proxy

    assert config.PROXY_STICKY_SESSIONS is False, (
        "sticky sessions must stay off by default -- they defeat per-IP rotation, "
        "which is the whole reason the egress pool exists"
    )

    class Prov:
        id = "opencode"

        def needs_proxy(self):
            return True

        def prefer_direct(self, model_id):
            return False

    pool = ProxyPool.from_list([
        {"id": f"warp-{i + 1}", "url": f"socks5://127.0.0.1:{51001 + i}"}
        for i in range(10)
    ])
    session = "01999b4c-8f4a-7b31-9c2e-4d5a6b7c8d9e"   # one Codex session, 40 turns
    used = {}                                            # proxy.id -> hit count
    for _ in range(40):
        proxy = _pick_proxy(Prov(), pool, session, "deepseek-v4-flash-free")
        used[proxy.id] = used.get(proxy.id, 0) + 1
        pool.mark_success(proxy)

    assert len(used) == 10, f"only {len(used)} of 10 exits carried traffic: {used}"
    assert max(used.values()) - min(used.values()) <= 1, \
        f"load should be even across exits, got {used}"


def test_unit_sticky_sessions_assign_by_load_not_by_hash():
    """When affinity *is* wanted, it must respect load and survive a restart.

    The old implementation picked `hash(session_id) % len(proxies)`, which was
    wrong twice: it never looked at how busy the chosen proxy was, so a session
    could be pinned to the most-loaded exit in the pool; and `hash(str)` is
    salted per process, so the "deterministic" mapping changed on every restart.
    Assigning via `pick()` and remembering the result gives real affinity *and*
    real balance.
    """
    from providers import proxy_pool as pp

    pool = ProxyPool.from_list([
        {"id": f"warp-{i + 1}", "url": f"socks5://127.0.0.1:{51001 + i}"}
        for i in range(10)
    ])
    # Load every exit except warp-7, making it the unique least-loaded one. A
    # load-based assignment must therefore choose warp-7 for *any* session id; a
    # hash-based one lands wherever the hash falls.
    for px in pool.get_all_proxies():
        if px.id == "warp-7":
            continue
        for _ in range(5):
            pool.mark_success(px)

    chosen = {pool.pick_sticky(f"session-{i}").id for i in range(10)}
    assert chosen == {"warp-7"}, (
        f"every session must be assigned the least-loaded exit, got {sorted(chosen)} "
        "-- assignment is not looking at load"
    )

    # Affinity: once assigned, every later turn returns the same exit, even after
    # that exit becomes the busiest.
    session = "session-0"
    for _ in range(20):
        pool.mark_success(pool.get_by_id("warp-7"))
    assert {pool.pick_sticky(session).id for _ in range(5)} == {"warp-7"}, \
        "an assigned session must keep its exit"

    # A cooling exit releases the session rather than holding it hostage.
    pool.mark_failure(pool.get_by_id("warp-7"), 429)
    assert pool.pick_sticky(session).id != "warp-7"

    # The session map cannot grow without bound -- session ids are unbounded and
    # this process is long-lived.
    small = ProxyPool.from_list(["socks5://127.0.0.1:1", "socks5://127.0.0.1:2"])
    for i in range(pp._MAX_SESSIONS + 500):
        small.pick_sticky(f"s{i}")
    assert len(small._sessions) <= pp._MAX_SESSIONS, \
        f"session map grew to {len(small._sessions)}"


def test_unit_blocking_upstream_calls_run_off_the_event_loop():
    """The chat handler must never call sync upstream code on the event loop.

    `chat_completions` is `async def`, but the executor, the dispatcher and the
    provider under them all use a synchronous `httpx.Client`. Awaiting them
    directly froze the single event-loop thread for the whole upstream call --
    one slow model reply made /api/health and every other request hang until it
    finished (measured: a 4s upstream stalled health by 3.2s). Every blocking
    call must go through `run_in_threadpool`.
    """
    import inspect
    import app as app_mod

    src = inspect.getsource(app_mod.chat_completions)

    # `_reopen` is a nested helper that runs *inside* the StreamingResponse
    # iterator, which Starlette already drives in a worker thread -- it may call
    # the executor synchronously. Only the handler body proper must be clean, so
    # check the source up to that helper.
    body_src, _, reopen_src = src.partition("def _reopen()")
    assert reopen_src, "the mid-stream recovery helper disappeared"

    # The three blocking entry points, each of which must be wrapped.
    for call in ("executor.execute_nonstream", "executor.execute_stream", "_run_dispatcher"):
        assert call in src, f"{call} disappeared from the handler"

    # No bare `= executor.execute_...(` / `= _run_dispatcher(` calls remain in the
    # handler body: every occurrence must be an argument to run_in_threadpool.
    for bare in ("= executor.execute_nonstream(",
                 "= executor.execute_stream(",
                 "= _run_dispatcher("):
        assert bare not in body_src, (
            f"blocking call `{bare.strip('= ')}` is invoked directly on the event loop")

    # Two of the four blocking calls now go through `_execute_with_egress_wait`,
    # which parks an exhausted request on the event loop and threadpools the
    # executor itself. Either wrapper satisfies the invariant, so count both --
    # and check the parking wrapper really does hand off to a worker, otherwise
    # this test would bless a helper that blocks the loop.
    wrapped = (body_src.count("run_in_threadpool")
               + body_src.count("_execute_with_egress_wait("))
    assert wrapped >= 4, (
        "expected the dispatcher, both non-stream calls and the stream open to be "
        f"threadpooled, found {wrapped}")

    park_src = inspect.getsource(app_mod._execute_with_egress_wait)
    assert park_src.count("run_in_threadpool") == 2, (
        "the egress-wait wrapper must threadpool both the first attempt and the retry")
    assert "await parking.wait_for_egress" in park_src, (
        "the wait must be awaited on the event loop, never slept in a worker thread")
    assert app_mod.run_in_threadpool is not None


def test_unit_empty_catalog_is_not_refetched_on_every_request():
    """An empty catalog must back off, not re-fetch upstream per request.

    The TTL guard was `self._logical and age < TTL`. When a fetch failed,
    `_logical` stayed empty, the guard short-circuited falsy, and the *next*
    request re-ran the upstream call. Under real concurrency that stacked:
    three callers behind one 2s failing fetch measured 2s, 4s and 6s. A separate
    attempt clock (CATALOG_RETRY_SECONDS) fixes it while still recovering fast.
    """
    import threading
    from models.catalog import UnifiedCatalog
    from providers.base import Provider
    from providers.key_pool import KeyPool

    class FailingProvider(Provider):
        id = "opencode"
        display_name = "Failing"
        priority = 10

        def __init__(self):
            super().__init__(KeyPool([]))
            self.calls = 0

        def requires_key(self):
            return False

        def fetch_model_ids(self):
            self.calls += 1
            raise RuntimeError("upstream /models down")

        def is_model_free(self, model_id, meta):
            return True

    prov = FailingProvider()
    cat = UnifiedCatalog({"opencode": prov})

    # Three concurrent readers must share a single upstream attempt.
    threads = [threading.Thread(target=cat.free) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert prov.calls == 1, f"empty catalog re-fetched {prov.calls}x instead of backing off"

    # Subsequent reads inside the retry window must not fetch again either.
    cat.free()
    cat.free()
    assert prov.calls == 1, "retry window did not hold"

    # force=True must always bypass the backoff (that is what Refetch catalog does).
    cat.refresh(force=True)
    assert prov.calls == 2, "force=True must bypass the retry window"

    # And the window expiring lets it try again.
    cat._attempted_at = 0.0  # noqa: SLF001 - simulate the window elapsing
    cat.free()
    assert prov.calls == 3, "catalog never retries after the window elapses"



def _fake_warp(count):
    """A WarpManager stand-in: real instance shape, no wgcf, no subprocesses."""
    class Inst:
        def __init__(self, index, port):
            self.index = index
            self.port = port
            self.proxy_url = f"socks5://127.0.0.1:{port}"
            self.private_key = "k"
            self.address_v4 = "172.16.0.2/32"
            self.identity_dir = None
            self.process = None

    class Warp:
        def __init__(self, n):
            self.instances = [Inst(i + 1, 51001 + i) for i in range(n)]
            self.regenerated = []
            self.rerolled = []

        def regenerate_instance(self, inst, log=None):
            self.regenerated.append(inst.index)
            return True

        def restart_instance(self, inst, log=None):
            return True

        def re_roll_tunnel(self, inst, attempt=0, log=None):
            self.rerolled.append(inst.index)
            return "162.159.192.1"

    return Warp(count)


def _daemon(warp, pool, min_healthy=None):
    """Build a WarpHealthDaemon without starting its thread."""
    from warp.health import WarpHealthDaemon

    daemon = WarpHealthDaemon(warp, pool, min_healthy=min_healthy, log=lambda *a, **k: None)
    return daemon


def test_unit_a_dumped_identity_rejoins_the_pool_in_the_same_cycle():
    """A recycled exit must be back in the pool before the cycle ends.

    The burn lives on the tunnel's exit IP, not the identity, so the dump
    re-rolls the tunnel (endpoint rotation + restart) rather than spending a
    Cloudflare registration. Three things must hold when it fires: the slot's
    tunnel is re-established, its stale burn counters are zeroed (they
    describe an address it no longer uses, and would push it straight back
    over the dump threshold), and the proxy never leaves the pool -- the old
    remove-then-regenerate handoff could strand a fixed exit outside the
    routing pool until a later cycle re-measured it.
    """
    from warp import health as health_mod

    warp = _fake_warp(3)
    pool = ProxyPool.from_list([])
    # Floor of 1 so `_ensure_min_healthy` stays out of the way -- it re-measures
    # everything and would mask the handoff this test is about.
    daemon = _daemon(warp, pool, min_healthy=1)

    # #1 is pooled and burned past the dump threshold.
    daemon._health_check = lambda inst: {"healthy": True, "index": inst.index}  # noqa: SLF001
    daemon._sync_pool([{"healthy": True}] * 3)
    px = pool.get_by_id("warp-1")
    for _ in range(health_mod.MAX_429_TOTAL):
        pool.mark_failure(px, 429)
    assert px.total_429 >= health_mod.MAX_429_TOTAL

    daemon._heal_instance = lambda inst: None  # noqa: SLF001
    daemon._health_check = lambda inst: {"healthy": True, "index": inst.index}  # noqa: SLF001

    result = daemon.check_and_heal()

    assert 1 in warp.rerolled, warp.rerolled
    assert result["dumped"] == 1, result
    # The re-rolled exit stayed in the pool the whole time.
    after = pool.get_by_id("warp-1")
    assert after is not None, "a re-rolled exit was dropped from the pool"
    # Its burn counters were reset with the new exit IP.
    assert after.total_429 == 0 and after.consecutive_failures == 0
    # And the gauge the dashboard draws agrees with the identity count.
    assert result["pool"]["total"] == 3, result["pool"]
    assert result["pool"]["available"] == 3, result["pool"]


def test_unit_an_auto_routed_stream_reroutes_to_another_model_mid_flight():
    """A stalled auto-routed stream must retry on a *different* model.

    `stream_guard` gets one retry when a stream dies after bytes are on the wire.
    That retry reopened on the same model, and the usual reason a stream dies
    mid-flight is that model stalling -- so the single attempt was spent on the
    thing that had just failed. `lingling-auto` delegated the choice of model, so
    the retry is entitled to re-decide; a request that named a model explicitly is
    not, and must stay on it.
    """
    import app as app_mod
    import json

    seen_models = []

    def fake_execute_stream(messages, model_id, providers, **kwargs):
        seen_models.append(model_id)

        def gen():
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}'
            if len(seen_models) == 1:
                raise RuntimeError("tunnel died mid-answer")
            yield b'data: {"choices":[{"delta":{"content":" done"},"finish_reason":"stop"}]}'

        class _P:
            id = "opencode"

        return gen(), _P(), None, []

    original = app_mod.executor.execute_stream
    app_mod.executor.execute_stream = fake_execute_stream
    try:
        # The module-level client, not a fresh `with TestClient(...)`: entering
        # one runs the lifespan hook, which registers WARP identities.
        r = client.post("/v1/chat/completions", json={
            "model": "lingling-auto",
            "messages": [{"role": "user", "content": "Refactor this component"}],
            "stream": True,
        })
        body = b"".join(r.iter_bytes()).decode("utf-8")

        assert len(seen_models) == 2, f"expected one retry, got {seen_models}"
        assert seen_models[0] != seen_models[1], \
            f"the retry reopened on the model that just died: {seen_models}"
        # The client is told to discard the partial answer before the new one,
        # and the frame names the model it moved to -- the dashboard renders one
        # of two different notices off that field.
        assert "lingling_reset" in body, body[:400]
        reset = next(
            json.loads(ln[len("data: "):])
            for ln in body.splitlines()
            if ln.startswith("data: ") and "lingling_reset" in ln
        )["lingling_reset"]
        assert reset.get("model") == seen_models[1], reset
    finally:
        app_mod.executor.execute_stream = original


def test_unit_an_explicit_model_is_never_swapped_mid_stream():
    """Only `lingling-auto` delegates the choice; a named model is a contract.

    A client that asked for a specific model gets that model on the retry too
    -- silently answering from a different one would make the response disagree
    with the request, and callers key cost and behaviour off the model they
    named. The named id is picked from the live catalog (``catalog.free``) so
    the handler's "Unknown model" gate accepts it: a hardcoded id would red the
    moment OpenCode retires the model upstream (exactly how this test first
    broke against the retired ``ling-3.0-flash-free``).
    """
    import app as app_mod

    # The handler's pre-check (catalog.providers_for(target)) rejects unknown
    # ids with a 400 before the mock runs, so the named id must be one the live
    # catalog actually serves -- pick it from there, never as a literal string.
    ids = sorted({m.id for m in catalog.free()})
    if not ids:
        raise SkipTest(
            "live catalog unavailable; the named-model rerun path needs an "
            "id the handler's gate will accept"
        )
    mid = ids[0]

    seen_models = []

    def fake_execute_stream(messages, model_id, providers, **kwargs):
        seen_models.append(model_id)

        def gen():
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}'
            if len(seen_models) == 1:
                raise RuntimeError("tunnel died mid-answer")
            yield b'data: {"choices":[{"delta":{"content":" done"},"finish_reason":"stop"}]}'

        class _P:
            id = "opencode"

        return gen(), _P(), None, []

    original = app_mod.executor.execute_stream
    app_mod.executor.execute_stream = fake_execute_stream
    try:
        r = client.post("/v1/chat/completions", json={
            "model": mid,
            "messages": [{"role": "user", "content": "Refactor this component"}],
            "stream": True,
        })
        b"".join(r.iter_bytes())

        assert seen_models == [mid] * 2, seen_models
    finally:
        app_mod.executor.execute_stream = original


def test_unit_text_only_fallback_strips_images():
    """A text-only retry target receives placeholders, not raw image parts.

    ``dispatcher.fallback_model`` answers an image request from a text-only
    model when every vision-capable one is down, and OpenCode answers HTTP 400
    to a text-only model fed image parts -- so every fallback site must replace
    the images with a placeholder first. This pins the helper every site uses.
    """
    import app as app_mod

    class _Vision:
        vision = True

    class _NoVision:
        vision = False

    original = app_mod.catalog.by_id
    app_mod.catalog.by_id = (
        lambda mid: _Vision() if mid == "mimo-v2.5-free" else _NoVision()
    )
    try:
        image_msg = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "what is in this screenshot"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }]
        stripped = app_mod._messages_for_model(image_msg, "deepseek-v4-flash-free", True)
        assert stripped[0]["content"] == (
            "what is in this screenshot\n[image was attached here]"
        ), stripped
        # The vision model keeps the attachment; a text-only request never strips.
        assert app_mod._messages_for_model(image_msg, "mimo-v2.5-free", True) is image_msg
        assert app_mod._messages_for_model(image_msg, "deepseek-v4-flash-free", False) is image_msg
    finally:
        app_mod.catalog.by_id = original



def test_unit_pool_ids_stay_unique_across_removals():
    """A generated id must never collide with a live one.

    Both pools derived ids from `len(list) + 1`, so removing a middle entry and
    adding another reused an id: remove `proxy-2` of three and the next add() is
    `proxy-3` again. `get_by_id`/`remove` return the first match, so the WARP
    health daemon could heal, repoint or dump the wrong exit.
    """
    pool = ProxyPool.from_list([f"socks5://127.0.0.1:{i}" for i in (1, 2, 3)])
    pool.remove("proxy-2")
    pool.add("socks5://127.0.0.1:9")
    ids = [p.id for p in pool.get_all_proxies()]
    assert len(ids) == len(set(ids)), ids

    # Repeated churn stays unique, and an explicitly-named id is not shadowed by
    # a later generated one.
    for _ in range(5):
        pool.remove(pool.get_all_proxies()[0].id)
        pool.add("socks5://127.0.0.1:8")
    pool.add("socks5://127.0.0.1:7", proxy_id="proxy-99")
    pool.add("socks5://127.0.0.1:6")
    ids = [p.id for p in pool.get_all_proxies()]
    assert len(ids) == len(set(ids)), ids

    keys = KeyPool.from_list(["s1", "s2", "s3"])
    keys.remove("key-2")
    keys.add("s9")
    kids = [k.id for k in keys.keys]
    assert len(kids) == len(set(kids)), kids


def test_unit_warp_port_allocation_never_steals_a_sibling():
    """A regenerating identity must not take another identity's assigned port.

    `_find_free_port` only checked whether a port was *listening*, but siblings
    are frequently down at exactly the moment the health daemon regenerates one,
    so their ports scanned as free. Both configs then wrote the same BindAddress
    and whichever wireproxy started second failed to bind, silently losing an exit.
    """
    from warp.manager import _find_free_port, _port_is_open

    # Anchored on a window this machine actually has free, not on BASE_PORT.
    # Every assertion below is about what the allocator does with *unoccupied*
    # ports, and a developer running Lingling has real wireproxy processes
    # listening across BASE_PORT.. -- so hardcoding it made the test's outcome
    # depend on whether the gateway happened to be up.
    base = next(
        (
            c for c in range(52000, 52900, 10)
            if not any(_port_is_open("127.0.0.1", c + i) for i in range(6))
        ),
        None,
    )
    if base is None:
        raise SkipTest("no free six-port window available for the allocator test")

    siblings = {base + i for i in range(0, 5)}

    # Reserved ports are skipped even though nothing is listening on them, which
    # is precisely the state a stopped sibling is in.
    got = _find_free_port(base, reserved=siblings)
    assert got not in siblings, f"{got} belongs to a sibling identity"
    assert got >= base + 5, got

    # Without the reservation the scan hands back the first port that is merely
    # not listening -- how one identity ended up on another's port.
    assert _find_free_port(base) in siblings

    # A reservation that leaves nothing free in range is an error, not a silent
    # duplicate: two identities sharing a BindAddress is the failure being fixed.
    try:
        _find_free_port(base, max_offset=3,
                        reserved={base, base + 1, base + 2})
    except RuntimeError:
        pass
    else:
        raise AssertionError("exhausting the range must raise, not reuse a port")


def test_unit_warp_reload_repairs_duplicate_ports():
    """Two identities must never load believing they own the same port.

    A pre-fix `regenerate` could hand one identity a sibling's port, leaving both
    `wireproxy.conf` files holding the same BindAddress. Reloading trusted the
    files, so `_pid_on_port`/`_kill_pid` acted on each other's process and the
    proxy pool wrote one URL under two different ids.
    """
    from pathlib import Path

    from warp.manager import WarpManager

    root = Path(tempfile.mkdtemp(prefix="lingling-warp-"))
    identities = root / "identities"
    # Three identities, two of them wrongly claiming the same port.
    for idx, port in ((1, 51043), (2, 51043), (3, 51045)):
        d = identities / f"warp-{idx}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "wireproxy.conf").write_text(
            f"[Interface]\nBindAddress = 127.0.0.1:{port}\n", encoding="utf-8")
        (d / "wgcf-profile.conf").write_text(
            "[Interface]\nPrivateKey = k\nAddress = 172.16.0.2/32\n", encoding="utf-8")

    mgr = WarpManager(root_dir=root, count=3)
    ports = [i.port for i in mgr.instances]
    assert len(ports) == len(set(ports)), f"duplicate ports survived the reload: {ports}"
    # The first claimant keeps the port it had; only the collider moves.
    assert mgr.instances[0].port == 51043, ports
    assert mgr.instances[2].port == 51045, ports
    # And every proxy_url matches its own port, so the pool cannot alias two ids.
    for inst in mgr.instances:
        assert inst.proxy_url.endswith(f":{inst.port}"), inst.proxy_url


def test_unit_warp_burn_history_survives_a_flapping_exit():
    """A proxy that flaps must still accumulate towards being dumped.

    `_sync_pool` removes an unhealthy proxy and re-adds it on recovery, and
    `ProxyPool.add` builds a fresh Proxy with zeroed counters. So a flapping exit
    reset its 429 tally every cycle and could never reach MAX_429_TOTAL --
    `_dump_burned_identities` never fired for exactly the proxies that most needed
    recycling, which is the whole point of the daemon.
    """
    from warp import health as health_mod

    warp = _fake_warp(3)
    pool = ProxyPool.from_list([])
    daemon = _daemon(warp, pool)

    healthy = [{"healthy": True}] * 3
    daemon._sync_pool(healthy)
    px = pool.get_by_id("warp-1")
    for _ in range(health_mod.MAX_429_TOTAL - 1):
        pool.mark_failure(px, 429)
    burned = px.total_429

    # It drops out and comes back, several times.
    for _ in range(3):
        daemon._sync_pool([{"healthy": False}, {"healthy": True}, {"healthy": True}])
        daemon._sync_pool(healthy)

    after = pool.get_by_id("warp-1")
    assert after.total_429 == burned, \
        f"burn history reset across the flap: {burned} -> {after.total_429}"

    # One more 429 crosses the threshold, and the tunnel is re-rolled onto a
    # fresh exit (the burn is on the exit IP, not the identity). The proxy
    # stays in the pool — only the tunnel behind it changed — and its burn
    # counters restart from zero because they describe an address it no
    # longer egresses from.
    pool.mark_failure(after, 429)
    assert daemon._dump_burned_identities() == 1, "a burned identity must be dumped"
    assert warp.rerolled == [1], warp.rerolled
    recycled = pool.get_by_id("warp-1")
    assert recycled is not None, "a re-rolled exit must stay in the pool"
    assert recycled.total_429 == 0 and recycled.consecutive_failures == 0


def test_unit_warp_minimum_healthy_never_exceeds_the_identity_count():
    """`min_healthy` above the identity count is an infinite regeneration loop.

    MIN_HEALTHY_PROXIES was a fixed 6. With LINGLING_WARP_COUNT=3 the daemon could
    never reach it, so `_ensure_min_healthy` re-registered every identity on every
    60s cycle -- against Cloudflare's rate-limited account-creation endpoint.
    """
    pool = ProxyPool.from_list([])

    small = _daemon(_fake_warp(3), pool)
    assert small.min_healthy <= 3, small.min_healthy

    warp = _fake_warp(3)
    daemon = _daemon(warp, pool)
    # Everything unhealthy: it may regenerate at most what exists, then stop.
    regenerated = daemon._ensure_min_healthy([{"healthy": False}] * 3)
    assert regenerated <= 3, regenerated
    assert len(warp.regenerated) <= 3, warp.regenerated

    # The normal 10-identity install still asks for the documented floor.
    big = _daemon(_fake_warp(10), pool)
    from warp.health import MIN_HEALTHY_PROXIES
    assert big.min_healthy == MIN_HEALTHY_PROXIES, big.min_healthy


def test_unit_warp_health_daemon_warmup_skips_probe():
    """The health daemon's first cycle must not probe or remove proxies.

    The bootstrap thread needs time to register identities and start wireproxy.
    If the daemon's first cycle immediately probes and removes, it undoes the
    bootstrap's work: proxies are yanked from the pool before their WARP tunnel
    has had time to establish, creating a restart loop and an empty pool.
    """
    pool = ProxyPool.from_list([])
    from pathlib import Path
    from warp.manager import WarpManager

    root = Path(tempfile.mkdtemp(prefix="lingling-warmup-"))
    mgr = WarpManager(root_dir=root, count=1)
    daemon = _daemon(mgr, pool)
    assert daemon._warmup is True, "daemon must start in warmup"

    # Run a full cycle with no instances configured — nothing to probe, nothing
    # to remove, but the cycle MUST clear _warmup for subsequent cycles.
    daemon._check_and_heal()
    assert daemon._warmup is False, (
        "_warmup must be False after the first _check_and_heal cycle")


def test_unit_warp_never_pools_an_unverified_tunnel():
    """A listening port is not proof the WARP tunnel works.

    `_health_check` only ran the SOCKS5 probe when the proxy was *already* in the
    pool, so a fresh instance was admitted on "config exists + port listening"
    alone. A bound port with a dead tunnel therefore carried real traffic for a
    full 60s cycle before its first probe ever ran.
    """
    import socket

    from pathlib import Path

    from warp.manager import WarpManager, _port_is_open

    root = Path(tempfile.mkdtemp(prefix="lingling-warpprobe-"))
    mgr = WarpManager(root_dir=root, count=1)
    inst = mgr.instances[0]
    # Move the instance onto a port nothing is using. `WarpManager` defaults to
    # BASE_PORT, which on a machine actually running Lingling is a live wireproxy
    # -- and the first assertion here is that an *unbound* port reads as
    # unhealthy, so the running gateway made this test contradict itself.
    free = next(
        (c for c in range(52900, 53400) if not _port_is_open("127.0.0.1", c)),
        None,
    )
    if free is None:
        raise SkipTest("no free port available for the tunnel-probe test")
    inst.port = free
    inst.proxy_url = f"socks5://127.0.0.1:{free}"
    inst.identity_dir.mkdir(parents=True, exist_ok=True)
    (inst.identity_dir / "wgcf-profile.conf").write_text(
        "[Interface]\nPrivateKey = abc\nAddress = 172.16.0.2/32\n", encoding="utf-8")
    (inst.identity_dir / "wireproxy.conf").write_text(
        f"[Interface]\nBindAddress = 127.0.0.1:{inst.port}\n", encoding="utf-8")
    inst.private_key = "abc"
    inst.address_v4 = "172.16.0.2/32"

    pool = ProxyPool.from_list([])
    daemon = _daemon(mgr, pool)
    # The daemon starts in warmup (first cycle). This test validates the
    # HTTP probe which only runs post-warmup, so disable it.
    daemon._warmup = False

    # Nothing listening: unhealthy, and no probe to run.
    closed = daemon._health_check(inst)
    assert closed["healthy"] is False, closed
    assert closed["in_pool"] is False

    # A plain TCP listener answers the port check but speaks no SOCKS5, so the
    # tunnel probe must run *and* fail even though the proxy is not pooled yet.
    fake = socket.socket()
    fake.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        fake.bind(("127.0.0.1", inst.port))
        fake.listen(1)
    except OSError:
        fake.close()
        raise SkipTest(f"port {inst.port} unavailable on this machine")
    try:
        result = daemon._health_check(inst)
        assert result["port_open"] is True, result
        assert result["http_probe_ok"] is False, \
            "the tunnel must be probed before the proxy is ever pooled"
        assert result["healthy"] is False, result

        # And the sync therefore refuses to admit it.
        daemon._sync_pool([result])
        assert pool.get_by_id("warp-1") is None, \
            "an unverified tunnel must not enter the routing pool"
    finally:
        fake.close()



def test_unit_responses_bridge_forwards_streamed_reasoning():
    """Thinking must reach a Codex client, not be dropped on the floor.

    `stream_events` read only `delta.content`, but every reasoning model on
    OpenCode streams its thinking under a different key -- deepseek and ling use
    `reasoning_content`, nemotron uses `reasoning`. Measured live: 139-434 chars
    of reasoning per turn, none of it forwarded, while the ledger billed the
    tokens. The non-streaming path already coped via `_assistant_text`, so the
    same model gave different output depending on `stream`.
    """
    from routing import responses_bridge

    for key in ("reasoning_content", "reasoning", "thinking"):
        chat = iter([
            ('data: {"choices":[{"delta":{"%s":"weighing it up"}}]}' % key).encode(),
            b'data: {"choices":[{"delta":{"content":"91 = 7 x 13"},"finish_reason":"stop"}]}',
        ])
        body = b"".join(responses_bridge.stream_events(chat, "m")).decode()
        assert "response.reasoning_summary_text.delta" in body, f"{key}: no reasoning event"
        assert "weighing it up" in body, f"{key}: reasoning text lost"
        assert "91 = 7 x 13" in body, f"{key}: answer lost"

        # Reasoning is its own output item, and the two must not share an index.
        added = [f for f in body.split("event: ") if f.startswith("response.output_item.added")]
        assert len(added) == 2, added
        assert '"type":"reasoning"' in added[0] and '"output_index":0' in added[0]
        assert '"type":"message"' in added[1] and '"output_index":1' in added[1]

    # A model that answers without thinking keeps its message at index 0, so the
    # common case is unchanged for clients that key on the index.
    plain = iter([b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}'])
    body = b"".join(responses_bridge.stream_events(plain, "m")).decode()
    assert "response.reasoning_summary_text" not in body
    assert '"output_index":0' in body and '"type":"message"' in body


def test_unit_responses_bridge_answers_when_a_model_only_reasons():
    """nemotron streams its whole answer as reasoning; the turn must not be empty.

    Verified live: `nemotron-3-ultra-free` streamed 77 chars under `reasoning`
    and left `content` empty in every delta, so a Codex client received a message
    item with no text -- a blank reply for a model that answered fine
    non-streaming. The thinking is promoted to the answer rather than lost.
    """
    from routing import responses_bridge

    chat = iter([
        b'data: {"choices":[{"delta":{"role":"assistant","content":"","reasoning":"The user wants ok."}}]}',
        b'data: {"choices":[{"delta":{"content":"","reasoning":" So: ok."}}]}',
    ])
    body = b"".join(responses_bridge.stream_events(chat, "m")).decode()
    assert "response.output_text.delta" in body, "no message item was emitted at all"
    assert "The user wants ok. So: ok." in body
    # Both items are present and distinctly indexed.
    assert '"type":"reasoning"' in body and '"type":"message"' in body


def test_unit_responses_bridge_promotes_reasoning_when_the_turn_completes():
    """A reasoning-only model that reports finish_reason must not go blank.

    Regression for the gate that read ``and not finished``: nemotron streams its
    whole answer as reasoning *and* ends with an explicit ``finish_reason``, so
    the promotion above was skipped exactly when the turn was complete. The
    client then got only a ``reasoning`` item and an empty answer.
    """
    from routing import responses_bridge

    chat = iter([
        b'data: {"choices":[{"delta":{"role":"assistant","content":"","reasoning":"The answer is 42. "}}]}',
        b'data: {"choices":[{"delta":{"content":"","reasoning":" Plain english follows. "}}]}',
        b'data: {"choices":[{"delta":{"content":""},"finish_reason":"stop"}]}',
    ])
    body = b"".join(responses_bridge.stream_events(chat, "m")).decode()
    # The completed, reasoning-only answer is surfaced as text...
    assert "response.output_text.delta" in body
    assert "The answer is 42." in body and "Plain english follows." in body
    # ...and lands in a real message item alongside the reasoning item.
    assert '"type":"reasoning"' in body and '"type":"message"' in body


def test_unit_responses_usage_keeps_the_reasoning_count():
    """`reasoning_tokens` must survive the hop, where the Responses spec puts it.

    Lingling's ledger recorded thinking correctly while the API response dropped
    it, so the dashboard and the client disagreed about the same request.
    """
    from routing import responses_bridge

    mapped = responses_bridge._usage({
        "prompt_tokens": 100, "completion_tokens": 250, "total_tokens": 350,
        "completion_tokens_details": {"reasoning_tokens": 200},
    })
    assert mapped["input_tokens"] == 100
    assert mapped["output_tokens"] == 250
    assert mapped["output_tokens_details"] == {"reasoning_tokens": 200}

    # Absent or malformed details must not invent a key or raise.
    assert "output_tokens_details" not in responses_bridge._usage({"prompt_tokens": 1})
    assert "output_tokens_details" not in responses_bridge._usage(
        {"completion_tokens_details": {"reasoning_tokens": "lots"}})


def test_unit_request_body_fields_lingling_manages_never_reach_the_executor():
    """A body key that names an executor argument must not become a 500.

    `_passthrough_params` forwards every unrecognised field, and the result is
    splatted into `run_in_threadpool(executor.execute_*, ...)`. Any key matching a
    positional or keyword name collided there and raised TypeError -> HTTP 500.
    `timeout` was the realistic one: a plausible field for a client to send, and
    it killed the streaming path while non-streaming survived.

    The same collision happens one level deeper: the executor calls
    `prov.stream_chat(...)` / `prov.chat_completions(...)` with explicit
    `proxy_url=`/`proxy_id=` keywords, so a client-supplied value for either
    name collided there and raised TypeError even though the executor's own
    signatures were clean. Those method signatures are inspected here too.
    """
    import inspect

    import app as app_mod
    from starlette.concurrency import run_in_threadpool
    from providers.base import OpenAICompatibleProvider

    reserved = set()
    for fn in (executor.execute_nonstream, executor.execute_stream, run_in_threadpool,
               OpenAICompatibleProvider.stream_chat, OpenAICompatibleProvider.chat_completions):
        for name, param in inspect.signature(fn).parameters.items():
            if param.kind is not param.VAR_KEYWORD:
                reserved.add(name)

    # Every name either belongs to Lingling (dropped) or is not a collision risk.
    leaked = reserved - app_mod._PASSTHROUGH_EXCLUDE - {"messages", "kwargs", "args"}
    assert not leaked, f"these executor argument names would collide: {sorted(leaked)}"

    # And the forwarder actually drops them.
    body = {"model": "m", "messages": [], "timeout": 5, "model_id": "x",
            "providers": ["y"], "proxy_pool": None, "func": "z", "temperature": 0.5,
            "proxy_url": "socks5://127.0.0.1:51001", "proxy_id": "warp-1"}
    params = app_mod._passthrough_params(body)
    assert params == {"temperature": 0.5}, params


def test_unit_a_wrongly_shaped_body_is_a_client_error_not_a_crash():
    """Well-formed JSON of the wrong shape must answer 400, not 500.

    Every field read in the handlers assumed a mapping with string values, so a
    bare list, a numeric `model`, or a `messages` list of strings raised
    AttributeError/TypeError inside the handler and surfaced as a raw ASGI 500
    with no ledger row. Malformed *bytes* were already handled; this is the
    parses-fine-but-is-not-a-request case.
    """
    for body in (
        ["not", "an", "object"],
        "a bare string",
        None,
        42,
        {"model": 42, "messages": [{"role": "user", "content": "hi"}]},
        {"model": ["a"], "messages": [{"role": "user", "content": "hi"}]},
        {"messages": [{"role": "user", "content": "hi"}]},          # no model
        {"model": "m", "messages": ["a string, not a message"]},
        {"model": "m", "messages": []},
        {"model": "m"},                                              # no messages
    ):
        r = client.post("/v1/chat/completions", json=body)
        assert r.status_code == 400, f"{body!r} -> {r.status_code}"

    r = client.post("/v1/responses", json=["not an object"])
    assert r.status_code == 400, r.status_code

    # A non-string session_id must degrade, not be hashed or crash.
    r = client.post("/v1/chat/completions", json={
        "model": "m", "messages": [{"role": "user", "content": "hi"}],
        "session_id": {"nested": "dict"},
    })
    assert r.status_code in (400, 503), r.status_code   # 400 unknown model, never 500


def test_unit_a_recovered_stream_counts_as_delivered():
    """`ok_recovered` means the user got a complete answer, so it is not a failure.

    `stream_guard` files a mid-flight retry that succeeded as `ok_recovered`, but
    every success test was `status IN ('ok','ok_stream')` -- in four separate SQL
    queries plus three places in the dashboard. A working recovery therefore
    showed up as an outage in success_rate, the Outcomes block and the activity
    chart's `failed` series.
    """
    from pathlib import Path

    from usage.store import UsageStore

    tmp_dir = tempfile.mkdtemp(prefix="lingling-status-")
    store = UsageStore(str(Path(tmp_dir) / "usage.db"))
    try:
        for status in ("ok", "ok_stream", "ok_recovered", "exhausted", "stream_broken"):
            store.log("m", "m", "user", status=status, latency_ms=100.0)

        summary = store.summary()
        totals = summary["totals"]
        assert totals["requests"] == 5
        assert totals["ok"] == 3, f"ok/ok_stream/ok_recovered are all delivered: {totals}"
        assert totals["failed"] == 2, totals
        assert totals["success_rate"] == 60.0, totals

        # The same definition must hold in the per-model, daily and bucket series,
        # or the dashboard's charts disagree with its own totals.
        assert summary["per_model"][0]["failed"] == 2, summary["per_model"]
        assert sum(d["failed"] for d in store.daily(2)) == 2
        assert sum(b["failed"] for b in store.buckets(60, 60)) == 2
    finally:
        store.close()


def test_unit_open_routes_answer_with_or_without_a_trailing_slash():
    """The auth gate runs before FastAPI's slash redirect, so it must normalise.

    `_OPEN_PATHS` was compared against the raw path, so a monitoring probe on
    `/api/health/` got 401 -- the middleware saw a guarded path and the redirect
    that would have stripped the slash never ran.
    """
    for path in ("/api/health", "/api/health/", "/v1/models", "/v1/models/"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"


def test_unit_dispatcher_survives_a_non_object_json_reply():
    """A model answering `[]` is a parse miss, not an outage.

    `parse_decision` called `.get` on whatever json.loads returned, so a JSON
    array raised AttributeError. The caller's blanket except turned that into
    "dispatcher unavailable: 'list' object has no attribute 'get'", which reads
    like the gateway is down when the model simply replied in the wrong shape.
    """
    from routing import dispatcher

    for reply in ("[]", "42", '"a string"', "null", "[1, 2]"):
        model, reason = dispatcher.parse_decision(reply)
        assert model is None, f"{reply!r} -> {model!r}"
        assert "could not parse" in reason or "empty" in reason, reason

    # A decision wrapped in an array is still understood -- the regex fallback
    # finds the embedded object. What matters is that it does not raise.
    model, _ = dispatcher.parse_decision('[{"model": "m", "reason": "r"}]')
    assert model == "m"

    # A real decision still parses, including one wrapped in fences.
    model, _ = dispatcher.parse_decision('{"model": "ling-3.0-flash-free", "reason": "fast"}')
    assert model == "ling-3.0-flash-free"
    model, _ = dispatcher.parse_decision('```json\n{"model": "m", "reason": "r"}\n```')
    assert model == "m"


def test_unit_capability_table_does_not_report_a_real_model_as_zero_context():
    """Integer division rendered a sub-1K window as `0K` to the routing brain."""
    from routing import dispatcher

    class M:
        def __init__(self, ctx):
            self.id = "m"
            self.vision = False
            self.reasoning = True
            self.context_length = ctx
            self.capabilities = {"desc": "d"}
            self.provider_ids = ["opencode"]

    table = dispatcher.build_capability_table([M(500)])
    assert "context=1K" in table, table
    assert "context=0K" not in table, table
    # Unknown stays unknown rather than becoming a fabricated number.
    assert "context=?" in dispatcher.build_capability_table([M(None)])
    assert "context=200K" in dispatcher.build_capability_table([M(200_000)])


def test_unit_effort_is_reresolved_when_failover_changes_the_model():
    """Each model honours its own values, so a fallback must re-clamp, not inherit.

    The chat path clamped effort for the primary model and then handed the *same*
    params dict to the fallback: ``max`` resolved to a model that publishes a
    ``max`` rung, then travelled unchanged onto a model that implements only a
    lower rung (laguna-s-2.1, ling-3.0). OpenCode answers 200 for a value it
    ignores, so it looked like it worked while changing nothing.
    ``_resolve_effort(previous=...)`` re-resolves from the client's original
    label.

    The primary/fallback pair is picked from the live catalog by capability
    rather than hardcoded: any free model whose effort set resolves ``max`` to
    itself is a valid primary, any model that does NOT publish ``max`` but
    resolves ``max`` to a lower rung is a valid fallback. A pinned pair
    (deepseek/laguna) reds the moment OpenCode retires one of the two -- the
    catalog is the real ground truth here.
    """
    import app as app_mod
    from routing import effort

    primary = None
    primary_effs = None
    fallback = None
    fallback_effs = None
    expected_fallback = None
    for lm in catalog.free():
        effs = (lm.capabilities or {}).get("effort") or []
        if not effs:
            continue
        # Primary: publishes `max` so "max" resolves to itself.
        if primary is None and "max" in effs and effort.resolve("max", effs) == "max":
            primary, primary_effs = lm.id, effs
        # Fallback: does NOT publish `max` but resolves it to a lower rung
        # -- exactly the case where the pre-fix carried `max` would be ignored.
        elif fallback is None and "max" not in effs:
            downgrade = effort.resolve("max", effs)
            if downgrade is not None:
                fallback, fallback_effs = lm.id, effs
                expected_fallback = downgrade
        if primary and fallback:
            break

    if not primary or not fallback:
        raise SkipTest(
            "live catalog has no `max`-rung primary or no downgrading fallback; "
            "the effort re-resolve path could not be exercised"
        )

    # What the two models actually honour, so the expectations below stay
    # grounded in the catalog's claims rather than literal predictions.
    assert effort.resolve("max", primary_effs) == "max", primary_effs
    assert effort.resolve("max", fallback_effs) == expected_fallback, fallback_effs

    params = {"reasoning_effort": "max", "temperature": 0.2}
    original = params["reasoning_effort"]
    sent = app_mod._resolve_effort(params, primary)
    if sent is None:
        raise SkipTest("live catalog unavailable; effort could not be resolved")
    assert sent == "max", sent

    # The failover path copies params and re-resolves from the original label.
    retry = dict(params)
    again = app_mod._resolve_effort(retry, fallback, previous=original)
    assert again == expected_fallback, \
        f"{fallback} should downgrade 'max' to {expected_fallback!r}, got {again!r}"
    assert retry["reasoning_effort"] == expected_fallback
    assert retry["temperature"] == 0.2, "other params must survive the retry"

    # Without `previous` the already-clamped value would be re-clamped as if the
    # client had asked for it -- which is how the bug produced an illegal value.
    naive = dict(params)
    app_mod._resolve_effort(naive, fallback)
    assert naive.get("reasoning_effort") == expected_fallback, \
        f"re-clamping 'max' against {fallback} must land on {expected_fallback!r}"


def test_unit_pool_url_updates_go_through_the_lock():
    """`url` is read by the executor mid-request, so it must not be written raw.

    The health daemon assigned `px.url` directly on the object `get_by_id`
    returned -- outside `ProxyPool._lock`, and `url` is the one field the executor
    reads while building an httpx client. A port migration could therefore land
    between the read and the connect, sending a request through a stale port.
    """
    import inspect

    from warp import health as health_mod
    import app as app_mod

    pool = ProxyPool.from_list([{"id": "warp-1", "url": "socks5://127.0.0.1:51001"}])
    assert pool.set_url("warp-1", "socks5://127.0.0.1:51043") is True
    assert pool.get_by_id("warp-1").url == "socks5://127.0.0.1:51043"
    assert pool.set_url("nope", "socks5://127.0.0.1:1") is False, \
        "an unknown id must report failure rather than inventing a proxy"

    # Neither writer may reach in and assign the field itself.
    for src in (inspect.getsource(health_mod.WarpHealthDaemon._sync_pool),
                inspect.getsource(app_mod._sync_warp_to_pool)):
        assert ".url = " not in src, "proxy URLs must be written via pool.set_url()"
        assert "set_url" in src



def test_unit_codex_catalog_lists_every_free_model():
    """A model with no effort dial must still appear in Codex's picker.

    Codex only lists models present in its catalog with a non-empty level list, so
    `mimo`, `nemotron` and `big-pickle` -- which publish no `reasoning_options` at
    all -- were invisible in `/model` even though they route fine. They get a
    single `default` rung instead.

    `default` is deliberately a word `routing.effort` has no rank for, so
    selecting it resolves to "send no effort parameter" and the model runs on its
    own default. That is the only thing such a model can do, and it is what the
    dashboard's effort table already documents for them.
    """
    from models import codex_catalog
    from routing import effort

    # The contract that makes this safe: the word never reaches a provider.
    assert effort.normalize("default") is None
    assert effort.resolve("default", ["high", "max"]) is None
    assert effort.resolve("default", []) is None

    catalog_json = codex_catalog.build(_codex_template(), [
        _lingling_model("deepseek-v4-flash-free", ["high", "max"]),
        _lingling_model("mimo-v2.5-free", [], vision=True),
        _lingling_model("nemotron-3-ultra-free", []),
        _lingling_model("big-pickle", []),
    ])
    entries = {m["slug"]: m for m in catalog_json["models"]}
    assert set(entries) == {"deepseek-v4-flash-free", "mimo-v2.5-free",
                            "nemotron-3-ultra-free", "big-pickle"}, sorted(entries)

    # A model with real options gets them; one without gets exactly one rung.
    assert [lv["effort"] for lv in entries["deepseek-v4-flash-free"]["supported_reasoning_levels"]] \
        == ["high", "max"]
    for slug in ("mimo-v2.5-free", "nemotron-3-ultra-free", "big-pickle"):
        levels = [lv["effort"] for lv in entries[slug]["supported_reasoning_levels"]]
        assert levels == ["default"], f"{slug}: {levels}"
        assert entries[slug]["default_reasoning_level"] == "default"
        assert entries[slug]["supported_reasoning_levels"][0]["description"], \
            "Codex's parser rejects a level with no description"


def test_unit_codex_catalog_tracks_models_dev_without_a_code_change():
    """A model that starts publishing effort levels must appear on the next run.

    `mimo`, `nemotron` and `big-pickle` publish no `reasoning_options` today, so
    they are deliberately absent: Codex nulls the effort field for an entry whose
    level list is empty, making such an entry identical to no entry while adding
    ~40 KB each. They still work -- they just run on their own default, which is
    all they can do.

    The set is read live from `capabilities["effort"]` on every run, so if
    models.dev ever publishes options for one of them it is picked up with no code
    change. This asserts that, rather than leaving it as a claim in prose.
    """
    from models import codex_catalog

    template = _codex_template()
    today = [
        _lingling_model("deepseek-v4-flash-free", ["high", "max"]),
        _lingling_model("mimo-v2.5-free", [], vision=True),
        _lingling_model("nemotron-3-ultra-free", []),
        _lingling_model("big-pickle", []),
    ]
    entries = {m["slug"]: m for m in codex_catalog.build(template, today)["models"]}
    # Everything is listed; the ones with no published options carry the stand-in.
    assert [lv["effort"] for lv in entries["mimo-v2.5-free"]["supported_reasoning_levels"]] \
        == ["default"]

    # Same code, same call: models.dev now publishes options for two of them.
    tomorrow = [
        _lingling_model("deepseek-v4-flash-free", ["high", "max"]),
        _lingling_model("mimo-v2.5-free", ["low", "high"], vision=True),
        _lingling_model("nemotron-3-ultra-free", ["medium"]),
        _lingling_model("big-pickle", []),
    ]
    entries = {m["slug"]: m for m in codex_catalog.build(template, tomorrow)["models"]}
    assert [lv["effort"] for lv in entries["mimo-v2.5-free"]["supported_reasoning_levels"]] \
        == ["low", "high"], "published options must replace the stand-in"
    assert [lv["effort"] for lv in entries["nemotron-3-ultra-free"]["supported_reasoning_levels"]] \
        == ["medium"]
    # The one still publishing nothing keeps the stand-in.
    assert [lv["effort"] for lv in entries["big-pickle"]["supported_reasoning_levels"]] \
        == ["default"]
    # A vision model keeps its image modality in the entry Codex reads.
    assert "image" in entries["mimo-v2.5-free"]["input_modalities"]


def test_unit_an_exhausted_pool_waits_for_an_exit_instead_of_failing():
    """A request that finds every exit cooling must wait, not answer 503.

    This is the whole point of parking: OpenCode's free tier meters per IP, so
    when all ten WARP exits are in cooldown the pool is not broken, it is busy.
    Failing there ends a coding agent's entire task, and the exits come back
    seconds later. The request holds until one does.
    """
    import asyncio

    from routing import parking

    pool = ProxyPool.from_list([
        {"id": "warp-1", "url": "socks5://127.0.0.1:51001"},
        {"id": "warp-2", "url": "socks5://127.0.0.1:51002"},
    ])
    # Burn both exits. warp-2 is cooled twice so it stays down longer -- the
    # waiter must quote the *soonest* exit, not the last one it touched.
    pool.mark_failure(pool.get_by_id("warp-1"), 429)
    pool.mark_failure(pool.get_by_id("warp-2"), 429)
    pool.mark_failure(pool.get_by_id("warp-2"), 429)

    soonest = pool.time_until_available()
    assert soonest > 0, "both exits should be cooling"

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    waited = asyncio.run(
        parking.wait_for_egress(pool, budget_s=120.0, log=_QuietLog(), sleep=fake_sleep)
    )
    assert waited > 0, "an exhausted pool must be waited out, not failed"
    # It waits for warp-1 (one failure, the BLOCKED base) rather than
    # warp-2 (two, twice that): the waiter quotes the soonest exit, not the
    # last one it touched.
    assert slept and abs(slept[0] - soonest) < 0.05, (slept, soonest)
    assert slept[0] < pool.get_by_id("warp-2").cooldown_remaining()


def test_unit_waiting_is_skipped_when_it_cannot_help():
    """Parking must stay out of the way of every failure that is not exhaustion.

    Each of these cases used to be -- and must remain -- an immediate failure.
    Waiting on a healthy pool would add latency to a genuine upstream error, and
    waiting without a pool is pure delay: the retry would leave from the same
    burned IP that just failed.
    """
    import asyncio

    from routing import parking

    def wait(pool, budget=120.0):
        async def boom(_seconds):
            raise AssertionError("slept when waiting could not help")
        return asyncio.run(
            parking.wait_for_egress(pool, budget_s=budget, log=_QuietLog(), sleep=boom)
        )

    # A healthy exit is available: the failure was the upstream's, not the pool's.
    healthy = ProxyPool.from_list([{"id": "warp-1", "url": "socks5://127.0.0.1:51001"}])
    assert wait(healthy) == 0.0

    # No pool at all -- every request already egresses from the real IP.
    assert wait(None) == 0.0
    assert wait(ProxyPool.from_list([])) == 0.0

    # Cooling, but the caller allows no wait: the old 503 behaviour, opt-out via
    # LINGLING_EGRESS_WAIT_BUDGET=0.
    burned = ProxyPool.from_list([{"id": "warp-1", "url": "socks5://127.0.0.1:51001"}])
    burned.mark_failure(burned.get_by_id("warp-1"), 429)
    assert wait(burned, budget=0.0) == 0.0

    # Cooling for longer than the budget: holding the client that long is worse
    # than telling it the truth now.
    for _ in range(8):
        burned.mark_failure(burned.get_by_id("warp-1"), 429)
    assert burned.time_until_available() > 5.0
    assert wait(burned, budget=5.0) == 0.0


def test_unit_a_parked_request_answers_instead_of_503ing():
    """End to end through the real handler helper: no 503, a real answer.

    The executor is the real one and the first pass genuinely exhausts -- the only
    exit 429s and the pool cools it. Before parking, that raised AllFailedError
    and the handler turned it into HTTP 503 with an `exhausted` ledger row. Now
    `_execute_with_egress_wait` holds the request and the second pass succeeds.
    """
    import asyncio

    import app as app_mod

    pool = ProxyPool.from_list([{"id": "warp-1", "url": "socks5://127.0.0.1:51001"}])
    state = {"calls": 0}

    def flaky(prov, messages, model, secret):
        state["calls"] += 1
        if state["calls"] == 1:
            raise UpstreamError(429, "rate limited", "flaky")
        return CANNED

    prov = FakeProvider("flaky", flaky, use_proxy=True)

    # The helper reads the module-level pool and budget, so swap both for the
    # duration and let the cooldown be real (1s base) rather than mocked away.
    saved_pool, saved_budget = app_mod.proxy_pool, config.EGRESS_WAIT_BUDGET
    app_mod.proxy_pool = pool
    config.EGRESS_WAIT_BUDGET = 30.0
    try:
        resp, _prov, _key, _attempts = asyncio.run(
            app_mod._execute_with_egress_wait(
                executor.execute_nonstream,
                [{"role": "user", "content": "hi"}], "m", [prov], proxy_pool=pool,
            )
        )
    finally:
        app_mod.proxy_pool = saved_pool
        config.EGRESS_WAIT_BUDGET = saved_budget

    assert resp is CANNED, "the parked request must deliver a real answer"
    assert state["calls"] == 2, f"expected one retry after the wait, got {state['calls']}"


def test_unit_a_failure_the_wait_cannot_fix_still_raises():
    """Parking must not convert a real error into a delay and then a delay again.

    A non-retryable status never cools an exit, so there is nothing to wait for
    and the caller must see AllFailedError immediately -- that is what preserves
    the model-fallback path and the 400/503 the client is owed.
    """
    import asyncio

    import app as app_mod

    pool = ProxyPool.from_list([{"id": "warp-1", "url": "socks5://127.0.0.1:51001"}])
    prov = FakeProvider("broken", UpstreamError(400, "bad request", "broken"), use_proxy=True)

    saved_pool, saved_budget = app_mod.proxy_pool, config.EGRESS_WAIT_BUDGET
    app_mod.proxy_pool = pool
    config.EGRESS_WAIT_BUDGET = 30.0
    try:
        try:
            asyncio.run(
                app_mod._execute_with_egress_wait(
                    executor.execute_nonstream,
                    [{"role": "user", "content": "hi"}], "m", [prov], proxy_pool=pool,
                )
            )
        except executor.AllFailedError as exc:
            assert exc.attempts[0]["status"] == 400
        else:
            raise AssertionError("a 400 must not be parked and retried")
    finally:
        app_mod.proxy_pool = saved_pool
        config.EGRESS_WAIT_BUDGET = saved_budget


def test_unit_the_egress_wait_never_blocks_the_event_loop():
    """The wait must be an await, not a sleep in a threadpool worker.

    A parked request holds its coroutine for up to two minutes. If that wait
    happened inside `run_in_threadpool`, a burned pool plus a busy agent would
    consume every worker slot -- starving the requests that could still go out,
    including the retry this feature depends on.
    """
    import asyncio
    import inspect

    from routing import parking

    src = inspect.getsource(parking.wait_for_egress)
    assert "time.sleep" not in src, "a blocking sleep would hold a worker thread"
    assert "await sleep(" in src, "the wait must yield to the event loop"
    assert inspect.iscoroutinefunction(parking.wait_for_egress)

    # And the default really is asyncio.sleep, not something injected only in tests.
    default = inspect.signature(parking.wait_for_egress).parameters["sleep"].default
    assert default is asyncio.sleep, default


def test_unit_a_mid_stream_retry_waits_for_an_exit_too():
    """The one mid-stream retry must not be spent on an exit that has to refuse.

    `guarded_stream` gets a single attempt when a stream dies after bytes are on
    the wire. It used to reopen instantly, so if every exit happened to be cooling
    at that moment the retry hit a burned IP, failed, and the user lost the rest
    of the answer -- the pre-first-token wait did not cover this path because
    recovery happens inside an already-running SSE response.
    """
    from routing import parking
    from routing import stream_guard

    pool = ProxyPool.from_list([{"id": "warp-1", "url": "socks5://127.0.0.1:51001"}])
    pool.mark_failure(pool.get_by_id("warp-1"), 429)
    assert pool.time_until_available() > 0, "the only exit should be cooling"

    slept = []
    opened = []

    def hold():
        yield from parking.hold_stream_for_egress(
            pool, budget_s=30.0, log=_QuietLog(), sleep=slept.append,
        )

    def dies_after_one_chunk():
        yield b'data: {"choices":[{"delta":{"content":"half an ans"}}]}'
        raise RuntimeError("tunnel dropped")

    def reopen():
        opened.append(pool.time_until_available())
        return iter([
            b'data: {"choices":[{"delta":{"content":"the rest"},"finish_reason":"stop"}]}',
            b"data: [DONE]",
        ])

    outcome = stream_guard.StreamOutcome()
    frames = list(stream_guard.guarded_stream(
        open_stream=reopen, first=dies_after_one_chunk(), outcome=outcome,
        on_chunk=lambda raw: None, log=_QuietLog(), hold=hold,
    ))

    assert slept, "the retry reopened without waiting for a free exit"
    assert opened, "the stream was never retried at all"
    assert outcome.recovered and outcome.completed

    # The client is told to discard the partial answer and then gets the retry.
    body = b"".join(frames)
    assert stream_guard.RESET_KEY.encode() in body
    assert b"the rest" in body

    # A held connection must not look hung: keepalives go out during the wait,
    # and they are SSE comments so no client parses them as content.
    keepalives = [f for f in frames if f.startswith(b":")]
    assert keepalives, "a silent hold reads as a dead connection to the client"
    for frame in keepalives:
        assert b"data:" not in frame, frame


def test_unit_the_stream_hold_releases_the_worker_and_skips_usage():
    """Two properties the mid-stream hold must have, both invisible in the output.

    A StreamingResponse is driven by a threadpool worker, one `next()` at a time.
    Sleeping the whole wait in a single `next()` would pin that worker for up to
    two minutes; slicing it hands the thread back between keepalives. And the
    keepalives must never reach usage harvesting -- no upstream produced them, so
    counting them would corrupt the ledger's token totals.
    """
    from routing import parking

    pool = ProxyPool.from_list([{"id": "warp-1", "url": "socks5://127.0.0.1:51001"}])
    # Cool it well past one slice so the hold has to loop.
    for _ in range(4):
        pool.mark_failure(pool.get_by_id("warp-1"), 429)
    total = pool.time_until_available()
    assert total > parking._SLICE_S, total

    slept = []
    frames = list(parking.hold_stream_for_egress(
        pool, budget_s=60.0, log=_QuietLog(), sleep=slept.append,
    ))

    assert len(slept) > 1, f"the wait was not sliced: {slept}"
    assert all(s <= parking._SLICE_S for s in slept), slept
    assert abs(sum(slept) - total) < 0.05, (sum(slept), total)
    assert len(frames) == len(slept), "one keepalive per slice"

    # Harvesting must ignore them: they are comments, not data frames.
    seen = {}
    import app as app_mod
    for frame in frames:
        app_mod._harvest_stream_usage(frame, seen)
    assert seen == {}, seen


def test_unit_per_reason_cooldown_parks_blocked_exits_longer_than_transient():
    """A 429/401/403 bars the *exit IP* (rate window or region/blocklist); a 5xx
    or contract-shape failure is a transient hiccup. mark_failure used to apply
    one uniform base for every retryable status, so a rate-limited lane cooled
    for the same 1s as a server hiccup and was re-selected next turn -- re-burned
    it and the failover budget, the warp-7/warp-9 back-to-back 429 run. The
    per-reason table parks a barred exit under the longer BLOCKED base so pick()
    pivots onto a fresh lane, while a transient failure keeps the short base and
    retries sooner. Both still escalate to the shared cap.
    """
    pool = ProxyPool.from_list([
        {"id": "rate", "url": "socks5://127.0.0.1:51001"},
        {"id": "auth", "url": "socks5://127.0.0.1:51002"},
        {"id": "forbid", "url": "socks5://127.0.0.1:51003"},
        {"id": "transient", "url": "socks5://127.0.0.1:51004"},
    ])
    blocked = config.PROXY_COOLDOWN_BLOCKED_BASE_MS / 1000.0
    base = config.PROXY_COOLDOWN_BASE_MS / 1000.0
    cap = config.PROXY_COOLDOWN_MAX_MS / 1000.0

    # First failure per lane: each reason picks its own base (factor 2^0 = 1).
    delay_429 = pool.mark_failure(pool.get_by_id("rate"), 429)
    delay_401 = pool.mark_failure(pool.get_by_id("auth"), 401)
    delay_403 = pool.mark_failure(pool.get_by_id("forbid"), 403)
    delay_504 = pool.mark_failure(pool.get_by_id("transient"), 504)
    assert delay_429 == blocked and delay_401 == blocked and delay_403 == blocked
    assert delay_504 == base
    assert blocked > base, "blocked base must exceed the transient base"

    # A barred exit is actually cooling longer than a transient one.
    assert pool.get_by_id("rate").in_cooldown()
    assert pool.get_by_id("transient").in_cooldown()
    assert pool.get_by_id("rate").cooldown_remaining() > \
        pool.get_by_id("transient").cooldown_remaining()

    # Escalation doubles under the BLOCKED base, not the short base.
    delay_429_again = pool.mark_failure(pool.get_by_id("rate"), 429)
    assert delay_429_again == min(cap, blocked * 2)

    # The cap still bounds a lane that keeps burning (10, 20, 40, 60, ...).
    px = pool.get_by_id("rate")
    for _ in range(4):
        pool.mark_failure(px, 429)
    assert pool.mark_failure(px, 429) == cap

    # reset_counters clears a re-rolled exit's slate regardless of reason.
    pool.reset_counters("rate")
    after = pool.get_by_id("rate")
    assert after.consecutive_failures == 0 and after.cooldown_until == 0.0
    assert not after.in_cooldown()


def test_unit_cooldown_transitions_are_logged():
    """Each cooldown transition emits a greppable log line.

    The three transitions an operator tails to debug the "429 prison" and a
    slow-to-heal lane -- park (mark_failure on a retryable status), extend
    (extend_cooldown, the upstream-Retry-After + probe-verdict path) and heal
    (mark_success of a previously-parked lane) -- each log under the
    ``providers.proxy_pool`` logger. A non-retryable status cools nothing and
    stays silent, and a steady-state success (nothing was parked) does not
    re-heal-log, so the log names only real state changes.
    """
    import logging
    from providers.proxy_pool import ProxyPool

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records: list[str] = []

        def emit(self, rec):
            self.records.append(rec.getMessage())

    handler = _Capture()
    pl = logging.getLogger("providers.proxy_pool")
    pl.addHandler(handler)
    prev_level, prev_prop = pl.level, pl.propagate
    pl.setLevel(logging.INFO)
    pl.propagate = False
    try:
        pool = ProxyPool.from_list([{"id": "warp-1", "url": "socks5://127.0.0.1:51001"}])
        px = pool.get_by_id("warp-1")

        # A non-retryable status cools nothing and logs nothing.
        pool.mark_failure(px, 404)
        assert handler.records == [], handler.records

        # Park: a 429 logs the blocked park with the status and the streak.
        pool.mark_failure(px, 429)
        assert any(
            "warp-1" in m and "parked" in m and "429" in m and "blocked" in m
            for m in handler.records
        ), handler.records

        # Extend (the Retry-After / probe-verdict path) logs the extension and
        # the remaining park.
        pool.extend_cooldown(px, 30.0)
        assert any(
            "warp-1" in m and "extended" in m for m in handler.records
        ), handler.records

        # Heal: a success of a parked lane logs the recovery.
        pool.mark_success(px)
        assert any(
            "warp-1" in m and "healed" in m for m in handler.records
        ), handler.records

        # A follow-up success on the now-clean lane must not re-log a heal.
        healed = [m for m in handler.records if "healed" in m]
        pool.mark_success(px)
        assert [m for m in handler.records if "healed" in m] == healed, (
            "a steady-state success must not re-log a heal"
        )
    finally:
        pl.removeHandler(handler)
        pl.setLevel(prev_level)
        pl.propagate = prev_prop


def test_unit_pick_keeps_per_ip_balance_on_an_idle_pool():
    """The tie-break deliberately does NOT alternate egress families on a tied
    (idle) pool. A flat cursor gives every proxy an equal turn -- per-IP
    balanced, so each exit burns at the same rate; a 50/50 family alternation
    would hand the minority family (e.g. 3 Tor beside 10 WARP) ~half the
    traffic and burn each Tor IP ~3x faster, the opposite imbalance. The case
    that actually needs Tor -- a model cooked on the WARP exits -- is already
    handled by ``_pick_proxy``'s ``pick_kind("tor")`` bias on an empty sampler
    set, and the decayed-load balancer dynamically routes to the lighter
    family under traffic. Lock the per-IP balance so a later "kind-aware
    cursor" does not silently regress it.
    """
    from collections import Counter
    pool = ProxyPool.from_list([
        {"id": "warp-1", "url": "socks5://127.0.0.1:51001"},
        {"id": "warp-2", "url": "socks5://127.0.0.1:51002"},
        {"id": "tor-1", "url": "socks5://127.0.0.1:52001"},
    ])
    picks = Counter(pool.pick().id for _ in range(3 * 20))
    counts = sorted(picks.values())
    assert counts[-1] - counts[0] <= 1, picks          # every proxy, an equal turn
    assert set(picks) == {"warp-1", "warp-2", "tor-1"}, picks


def test_unit_pick_favors_tor_when_warp_carries_more_load():
    """The real Tor-protection is the decayed-load balancer, not a kind cursor:
    the moment the WARP exits carry more recent load than Tor, pick() lands on
    Tor even though WARP outnumbers it. A flat cursor biases by COUNT only on a
    fully-tied (idle) pool -- under divergent load it routes to the lighter
    family. Lock this too, so Tor is reached exactly when WARP is the hotter
    family."""
    pool = ProxyPool.from_list([
        {"id": "warp-1", "url": "socks5://127.0.0.1:51001"},
        {"id": "warp-2", "url": "socks5://127.0.0.1:51002"},
        {"id": "tor-1", "url": "socks5://127.0.0.1:52001"},
    ])
    # Burden both WARP exits (mark_success bumps the decayed load); Tor stays at
    # zero, so the least-loaded available proxy is the Tor lane.
    for pid in ("warp-1", "warp-2"):
        px = pool.get_by_id(pid)
        for _ in range(5):
            pool.mark_success(px)
    assert pool.pick().id == "tor-1"
    # And once Tor carries the load too, the tie-break spreads across all three
    # again rather than pinning Tor.
    for _ in range(5):
        pool.mark_success(pool.get_by_id("tor-1"))
    picks = {pool.pick().id for _ in range(6)}
    assert picks == {"warp-1", "warp-2", "tor-1"}, picks


def test_unit_a_healthy_pool_does_not_delay_a_mid_stream_retry():
    """Recovery must stay instant for every failure that is not exhaustion.

    A dropped tunnel with nine healthy exits left is the common case, and the
    answer there is to reopen immediately. Waiting would add a second of dead air
    to a stream that could have resumed at once.
    """
    from routing import parking

    def wait(pool, budget=30.0):
        def boom(_seconds):
            raise AssertionError("slept when a free exit was available")
        return list(parking.hold_stream_for_egress(
            pool, budget_s=budget, log=_QuietLog(), sleep=boom,
        ))

    healthy = ProxyPool.from_list([{"id": "warp-1", "url": "socks5://127.0.0.1:51001"}])
    assert wait(healthy) == []
    assert wait(None) == []
    assert wait(ProxyPool.from_list([])) == []

    burned = ProxyPool.from_list([{"id": "warp-1", "url": "socks5://127.0.0.1:51001"}])
    burned.mark_failure(burned.get_by_id("warp-1"), 429)
    assert wait(burned, budget=0.0) == [], "budget 0 must restore the old behaviour"
    for _ in range(8):
        burned.mark_failure(burned.get_by_id("warp-1"), 429)
    assert wait(burned, budget=5.0) == [], "a wait past the budget must not happen"


def test_unit_warp_kill_job_arms_and_reports():
    """The kill-on-close job reports real results instead of silent no-ops.

    The first implementation returned nothing, and the ctypes defaults
    truncated 64-bit handles, so every OS call silently failed -- children
    were never added to the job and outlived the gateway. This pins the
    public contract: ``ensure_kill_job()``/``assign()`` return booleans and
    ``job_active()`` reflects whether the job is really armed. On POSIX they
    are honest no-ops.
    """
    import os

    import warp.job as job_mod

    if os.name != "nt":
        assert job_mod.ensure_kill_job() is False
        assert job_mod.assign(4242) is False
        assert job_mod.job_active() is False
        return

    assert job_mod.ensure_kill_job() is True, "job must arm on Windows"
    assert job_mod.job_active() is True
    # Our own process is a legitimate member once the job is armed.
    assert job_mod.assign(os.getpid()) is True


def test_unit_warp_spawn_sites_join_the_kill_job():
    """Every wireproxy spawn must be put in the kill-on-close job.

    Regression for the orphan bug. The spawn sites called a bare
    ``ensure_kill_job()`` that was never imported into the manager's
    namespace. The resulting NameError was swallowed by the per-instance
    try/except *after* ``Popen`` had already started wireproxy, so every proxy
    came up looking healthy but never joined the job -- and survived the
    gateway's death, holding its SOCKS5 port forever. Both spawn paths must
    call ``job.ensure_kill_job()`` and ``job.assign(pid)``.
    """
    from pathlib import Path
    from unittest import mock

    from warp.manager import WarpManager

    root = Path(tempfile.mkdtemp(prefix="lingling-warpjob-"))
    ident = root / "identities" / "warp-1"
    ident.mkdir(parents=True, exist_ok=True)
    (ident / "wireproxy.conf").write_text(
        "[Interface]\nPrivateKey = k\nAddress = 172.16.0.2/32\n"
        "[Socks5]\nBindAddress = 127.0.0.1:51111\n", encoding="utf-8")
    (ident / "wgcf-profile.conf").write_text(
        "[Interface]\nPrivateKey = k\nAddress = 172.16.0.2/32\n", encoding="utf-8")

    quiet = lambda *a, **k: None  # noqa: E731
    fake_proc = mock.Mock()
    fake_proc.pid = 4242
    fake_proc.poll.return_value = None

    # --- restart_instance path -------------------------------------------
    mgr = WarpManager(root_dir=root, count=1)
    with mock.patch("warp.manager.job") as fake_job, \
            mock.patch("warp.manager.subprocess.Popen", return_value=fake_proc) as popen, \
            mock.patch("warp.manager._port_is_open", side_effect=[True, True]), \
            mock.patch("warp.manager._pid_on_port", return_value=None), \
            mock.patch("warp.manager.time.sleep"):
        ok = mgr.restart_instance(mgr.instances[0], log=quiet)

    assert popen.called, "restart_instance did not spawn wireproxy"
    fake_job.ensure_kill_job.assert_called_once_with()
    fake_job.assign.assert_called_once_with(4242)
    assert ok is True, "restart_instance must report the port as open"

    # --- start_all path ----------------------------------------------------
    # A fresh manager so no stale process handle marks the instance as running.
    mgr2 = WarpManager(root_dir=root, count=1)
    with mock.patch("warp.manager.job") as fake_job2, \
            mock.patch.object(WarpManager, "ensure_tools", return_value={}), \
            mock.patch("warp.manager.subprocess.Popen", return_value=fake_proc) as popen2, \
            mock.patch("warp.manager._port_is_open", side_effect=[False, True, True]), \
            mock.patch("warp.manager.time.sleep"):
        mgr2.start_all(log=quiet)

    assert popen2.called, "start_all did not spawn wireproxy"
    fake_job2.ensure_kill_job.assert_called()
    assert any(c.args == (4242,) for c in fake_job2.assign.call_args_list), \
        "start_all must assign the spawned pid to the kill job"


def test_unit_shutdown_stops_the_warp_proxies():
    """Graceful shutdown must stop the WARP proxies on the way out.

    Closing the backend window is a hard kill covered by the kill-on-close
    job; this is the polite path (and the only one on POSIX), so a stopped
    gateway never leaves a SOCKS5 port busy. The lifespan's ``finally`` must
    stop the health daemon, call ``warp_manager.stop_all()`` and close the
    usage store.
    """
    import asyncio
    from unittest import mock

    import app as app_mod

    async def _enter_and_exit():
        with mock.patch.object(app_mod, "_startup_sync_warp", return_value=None), \
                mock.patch.object(app_mod, "warp_health_daemon") as hd, \
                mock.patch.object(app_mod, "warp_manager") as wm, \
                mock.patch.object(app_mod, "usage_store") as us:
            async with app_mod.lifespan(None):
                pass
        return hd, wm, us

    hd, wm, us = asyncio.run(_enter_and_exit())
    wm.stop_all.assert_called_once_with()
    hd.stop.assert_called_once_with()
    us.close.assert_called_once_with()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
_TESTS = [
    # hermetic unit tests first (fast, no network)
    test_unit_keyless_single_attempt,
    test_unit_keyless_failover_to_keyed,
    test_unit_retry_codes_failover,
    test_unit_non_retryable_stops,
    test_unit_all_fail,
    test_unit_stream_first_chunk_failover,
    test_unit_fast_model_bypasses_proxy_pool,
    test_unit_session_token_signing,
    test_unit_secfetch_header_no_longer_grants_access,
    test_unit_session_cookie_authenticates,
    test_unit_usage_finalize_and_prune,
    test_unit_stream_guard_recovers_broken_stream,
    test_unit_stream_guard_no_retry_after_completion,
    test_unit_stream_guard_gives_up_after_one_retry,
    test_unit_stream_guard_respects_opt_out,
    test_unit_stream_guard_forwards_every_chunk_verbatim,
    test_unit_stream_guard_client_disconnect_does_not_retry,
    test_unit_responses_bridge_request_maps_codex_shape_to_chat,
    test_unit_responses_bridge_stream_events_for_text_and_tool_calls,
    test_unit_responses_bridge_keeps_images_and_indexes_items,
    test_unit_responses_bridge_reports_a_truncated_stream,
    test_unit_effort_maps_harness_labels_by_rank,
    test_unit_effort_clamps_to_each_models_published_values,
    test_unit_effort_sets_come_from_models_dev_not_a_hardcoded_table,
    test_unit_codex_reasoning_block_becomes_chat_reasoning_effort,
    test_unit_codex_catalog_declares_effort_so_codex_stops_nulling_it,
    test_unit_codex_catalog_entries_clone_a_real_codex_model,
    test_unit_codex_catalog_auto_entry_spans_every_routable_model,
    test_unit_api_key_store_survives_a_truncated_write,
    test_unit_usage_row_limit_is_capped,
    test_unit_stream_headers_survive_nonlatin1_reason,
    test_unit_catalog_serves_last_good_list_through_a_fetch_failure,
    test_unit_hot_models_route_through_the_proxy_pool,
    test_unit_codex_catalog_lists_every_free_model,
    test_unit_codex_catalog_tracks_models_dev_without_a_code_change,
    test_unit_responses_bridge_forwards_streamed_reasoning,
    test_unit_responses_bridge_answers_when_a_model_only_reasons,
    test_unit_responses_usage_keeps_the_reasoning_count,
    test_unit_request_body_fields_lingling_manages_never_reach_the_executor,
    test_unit_a_wrongly_shaped_body_is_a_client_error_not_a_crash,
    test_unit_a_recovered_stream_counts_as_delivered,
    test_unit_open_routes_answer_with_or_without_a_trailing_slash,
    test_unit_dispatcher_survives_a_non_object_json_reply,
    test_unit_capability_table_does_not_report_a_real_model_as_zero_context,
    test_unit_the_fallback_routes_on_the_request_not_on_the_router_model,
    test_unit_a_reason_with_camelcase_words_is_not_mangled,
    test_unit_text_only_fallback_strips_images,
    test_unit_effort_is_reresolved_when_failover_changes_the_model,
    test_unit_pool_url_updates_go_through_the_lock,
    test_unit_pool_ids_stay_unique_across_removals,
    test_unit_warp_port_allocation_never_steals_a_sibling,
    test_unit_warp_reload_repairs_duplicate_ports,
    test_unit_warp_burn_history_survives_a_flapping_exit,
    test_unit_warp_minimum_healthy_never_exceeds_the_identity_count,
    test_unit_warp_never_pools_an_unverified_tunnel,
    test_unit_warp_health_daemon_warmup_skips_probe,
    test_unit_a_dumped_identity_rejoins_the_pool_in_the_same_cycle,
    test_unit_one_session_still_spreads_across_every_exit,
    test_unit_sticky_sessions_assign_by_load_not_by_hash,
    test_unit_blocking_upstream_calls_run_off_the_event_loop,
    test_unit_empty_catalog_is_not_refetched_on_every_request,
    test_unit_an_exhausted_pool_waits_for_an_exit_instead_of_failing,
    test_unit_waiting_is_skipped_when_it_cannot_help,
    test_unit_a_parked_request_answers_instead_of_503ing,
    test_unit_a_failure_the_wait_cannot_fix_still_raises,
    test_unit_the_egress_wait_never_blocks_the_event_loop,
    test_unit_a_mid_stream_retry_waits_for_an_exit_too,
    test_unit_the_stream_hold_releases_the_worker_and_skips_usage,
    test_unit_a_healthy_pool_does_not_delay_a_mid_stream_retry,
    test_unit_an_auto_routed_stream_reroutes_to_another_model_mid_flight,
    test_unit_an_explicit_model_is_never_swapped_mid_stream,
    test_unit_warp_kill_job_arms_and_reports,
    test_unit_warp_spawn_sites_join_the_kill_job,
    test_unit_shutdown_stops_the_warp_proxies,
    # live integration (real OpenCode Zen, keyless)
    test_live_health,
    test_live_models,
    test_live_v1_models_openai_compatible,
    test_live_responses_nonstream_keyless,
    test_live_free_chat_direct,
    test_live_multimodel_routing,
    test_live_streaming_keyless,
    test_live_premium_rejected,
    test_live_usage_recorded,
    test_live_apikey_gate,
]


def main():
    passed = failed = skipped = 0
    failures = []
    print("Lingling real test suite")
    print("=" * 70)
    for fn in _TESTS:
        t0 = time.time()
        try:
            fn()
        except SkipTest as exc:
            skipped += 1
            print(f"  SKIP  {fn.__name__:<40} {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            failures.append((fn.__name__, exc))
            print(f"  FAIL  {fn.__name__:<40} {time.time() - t0:5.1f}s  {exc!r}")
        else:
            passed += 1
            print(f"  PASS  {fn.__name__:<40} {time.time() - t0:5.1f}s")
    print("=" * 70)
    print(f"{passed} passed, {failed} failed, {skipped} skipped")
    if failures:
        print("\nFailures:")
        for name, exc in failures:
            print(f"  - {name}: {exc!r}")
        sys.exit(1)


if __name__ == "__main__":
    main()


def _fetch_provider_factory():
    """Build a controllable FetchProvider + helpers for catalog self-heal tests."""
    from models.catalog import UnifiedCatalog  # noqa: F401 (-imported by callers)
    from providers.base import Provider, ProviderModel
    from providers.key_pool import KeyPool

    class FetchProvider(Provider):
        id = "opencode"
        display_name = "Fetch"
        priority = 10

        def __init__(self):
            super().__init__(KeyPool([]))
            self._ids = []
            self._fail_next = False     # set True to make the next fetch raise

        def requires_key(self):
            return False

        def fetch_model_ids(self):
            if self._fail_next:
                self._fail_next = False
                raise RuntimeError("upstream /models blip")
            return self._ids

        def is_model_free(self, model_id, meta):
            return model_id.endswith("-free")

        def build_model(self, model_id):
            return ProviderModel(
                id=model_id, provider_id=self.id, name=model_id, free=True,
                vision=False, reasoning=True, context_length=1000, max_output=100,
            )

    return FetchProvider, UnifiedCatalog


def test_unit_a_runtime_retirement_stays_parked_through_refresh(tmp_path, monkeypatch):
    """A model retired at runtime (its free-tier chat 400'd 'unavailable')
    STAYS parked across subsequent catalog refreshes -- even when OpenCode
    keeps advertising it in /models the whole time (the chronic case).

    The auto-resurrect self-heal path used to pop such an entry on the very
    next non-stale refresh; that proved wrong for the chronic case (still
    listed in /models but the upstream refuses to serve chat) so it's gone.
    Parked entries are now sticky until the operator clears
    retired_models.json OR the LINGLING_RETIRED_MODEL_TTL_DAYS age filter in
    _load_unavailable drops the entry on the next gateway start."""
    from core import config as core_config

    FetchProvider, UnifiedCatalog = _fetch_provider_factory()

    monkeypatch.setattr(core_config, "RETIRED_MODELS_FILE", tmp_path / "retired.json")
    monkeypatch.setattr(core_config, "RETIRED_MODELS_SEED", "")
    monkeypatch.setattr(core_config, "RETIRED_MODEL_TTL_DAYS", 7)

    prov = FetchProvider()
    cat = UnifiedCatalog({"opencode": prov})

    # Live free section lists both; retire one via the runtime path
    # (mark_unavailable is what _flag_model_retired calls after a 400).
    prov._ids = ["good-free", "dead-free"]
    cat.refresh(force=True)
    assert sorted(m.id for m in cat.free()) == ["dead-free", "good-free"]
    cat.mark_unavailable("dead-free")
    assert "dead-free" not in [m.id for m in cat.free()]
    assert cat.by_id("dead-free") is None
    assert cat.is_unavailable("dead-free") is True
    assert "dead-free" in cat.meta()["retired_models"]

    # OpenCode keeps advertising it across live non-stale refreshes. The OLD
    # design would have resurrected it here; the new one keeps it parked.
    for _ in range(3):
        prov._ids = ["good-free", "dead-free"]
        cat.refresh(force=True)
        assert "dead-free" not in [m.id for m in cat.free()]
        assert cat.is_unavailable("dead-free") is True

    # Genuinely removed (OpenCode dropped it from the free section): absent
    # from the next live fetch. The parked entry still holds; nothing
    # opportunistically un-retires it either way.
    prov._ids = ["good-free"]
    cat.refresh(force=True)
    assert "dead-free" not in [m.id for m in cat.free()]
    assert cat.is_unavailable("dead-free") is True

    # A freshly-built catalog (the Codex generator is a separate process)
    # loads the persisted state and keeps dead-free parked across reload too.
    prov2 = FetchProvider()
    prov2._ids = ["good-free", "dead-free"]
    cat2 = UnifiedCatalog({"opencode": prov2})
    cat2.refresh(force=True)
    assert "dead-free" not in [m.id for m in cat2.free()]
    assert cat2.is_unavailable("dead-free") is True


def test_unit_runtime_retirement_not_resurrected_from_stale_fetch(tmp_path, monkeypatch):
    """With the self-heal auto-resurrect path removed, a retired model is
    trivially kept retired on a stale /models fetch (the catalog fell back
    to last-good, which still lists the model) -- there's no pop mechanism to
    fire in the first place. Kept as a regression guard against any future
    re-addition of opportunistic resurrection based on stale data."""
    from core import config as core_config

    FetchProvider, UnifiedCatalog = _fetch_provider_factory()

    monkeypatch.setattr(core_config, "RETIRED_MODELS_FILE", tmp_path / "retired.json")
    monkeypatch.setattr(core_config, "RETIRED_MODELS_SEED", "")
    monkeypatch.setattr(core_config, "RETIRED_MODEL_TTL_DAYS", 7)

    prov = FetchProvider()
    cat = UnifiedCatalog({"opencode": prov})

    # Prime the last-good cache with a list that includes dead-free, then retire.
    prov._ids = ["good-free", "dead-free"]
    cat.refresh(force=True)
    cat.mark_unavailable("dead-free")
    assert cat.is_unavailable("dead-free") is True

    # The next fetch FAILS: the catalog falls back to last-good (which still
    # lists dead-free) under stale=True. No pop happens either way (live or
    # stale), so dead-free stays retired -- with the auto-resurrect path gone
    # this is the trivial outcome, but it stays tested to catch any future
    # re-addition of /models-cache-driven resurrection.
    prov._fail_next = True
    cat.refresh(force=True)
    assert "dead-free" not in [m.id for m in cat.free()]
    assert cat.is_unavailable("dead-free") is True


def test_unit_seeded_retirement_survives_re_listing(tmp_path, monkeypatch):
    """An operator-curated seed is honored verbatim: the seed exists precisely
    because /models keeps advertising a model the operator knows is dead, so a
    live re-appearance is NOT authoritative for a seeded id -- it stays hidden
    until the operator un-seeds it (LINGLING_RETIRED_MODELS='')."""
    from core import config as core_config

    FetchProvider, UnifiedCatalog = _fetch_provider_factory()

    monkeypatch.setattr(core_config, "RETIRED_MODELS_FILE", tmp_path / "retired.json")
    monkeypatch.setattr(core_config, "RETIRED_MODELS_SEED", "seeded-free")
    monkeypatch.setattr(core_config, "RETIRED_MODEL_TTL_DAYS", 7)

    prov = FetchProvider()
    prov._ids = ["good-free", "seeded-free"]
    cat = UnifiedCatalog({"opencode": prov})
    cat.refresh(force=True)

    # Seeded id is hidden straight away, even though /models lists it free.
    assert "seeded-free" not in [m.id for m in cat.free()]
    assert cat.is_unavailable("seeded-free") is True
    # The self-heal leaves seeded ids alone across a live refresh.
    cat.refresh(force=True)
    assert "seeded-free" not in [m.id for m in cat.free()]
    assert cat.is_unavailable("seeded-free") is True


def test_unit_app_flags_model_retired_on_unavailable_400_still_listed_or_not(monkeypatch):
    """The recycler retires a -free model on a 'Model is unavailable' 400 even
    when OpenCode keeps advertising it in /models -- the chronic
    deepseek/musespark case ('still listed, but the backend refuses chat').
    by_id is no longer consulted as a retirement gate; the probation-aware
    self-heal in refresh() is what resurrects such a model later if OpenCode
    genuinely restores the chat. Other 400s and 429s don't even mark."""
    import app as app_mod
    from providers.base import UpstreamError
    from routing import executor as exec_mod

    # Reset the per-model recheck cooldown so this test starts clean.
    monkeypatch.setattr(app_mod, "_retire_recheck_at", {})

    class FakeCat:
        def __init__(self):
            self.marked = []
            self._gone = set()  # ids whose /models membership has been dropped

        def by_id(self, mid):  # consulted only for log flavor; not a gate.
            return None if mid in self._gone else object()

        def mark_unavailable(self, mid):
            self.marked.append(mid)

        def is_unavailable(self, mid):
            return mid in self.marked

    cat = FakeCat()
    monkeypatch.setattr(app_mod, "catalog", cat)

    def unavail(detail):
        return exec_mod.AllFailedError(UpstreamError(400, detail), [])

    # Chronic case: model STILL listed in /models AND chat 400'd 'unavailable'.
    # The OLD design left it routable here; the engine-fix parks it outright --
    # the 400 is the retirement signal, /models membership is not consulted.
    assert app_mod._flag_model_retired(
        unavail("This model is unavailable for free. use inclusionai/ling-3.0-flash"),
        "ling-3.0-flash-free",
    ) is True
    assert cat.marked == ["ling-3.0-flash-free"]

    # Genuinely dropped (by_id now None) -> identical retirement behavior: the
    # gate is the 400 itself, not the /models membership.
    cat._gone.add("muse-free")
    app_mod._retire_recheck_at.clear()
    assert app_mod._flag_model_retired(
        unavail("This model is unavailable for free."),
        "muse-free",
    ) is True
    assert cat.marked == ["ling-3.0-flash-free", "muse-free"]

    # A non-retirement 400 (no 'unavailable' in detail) -> not retired.
    app_mod._retire_recheck_at.clear()
    assert app_mod._flag_model_retired(
        exec_mod.AllFailedError(UpstreamError(400, "bad request shape"), []),
        "x-free",
    ) is False
    assert cat.marked == ["ling-3.0-flash-free", "muse-free"]

    # A 429 is not retirement either.
    assert app_mod._flag_model_retired(
        exec_mod.AllFailedError(UpstreamError(429, "rate limited"), []),
        "x-free",
    ) is False
    assert cat.marked == ["ling-3.0-flash-free", "muse-free"]


def test_unit_app_flags_model_retired_recheck_cooldown(monkeypatch):
    """A transient 'unavailable' burst (huge upstream traffic) fires the
    retirement attempt on many concurrent requests at once. The per-model
    cooldown bounds it to one retirement decision per window: a second call
    for the same model within the cooldown returns False WITHOUT calling
    mark_unavailable again, so a burst can't churn the lock / persisted
    retired set."""
    import app as app_mod
    from providers.base import UpstreamError
    from routing import executor as exec_mod

    monkeypatch.setattr(app_mod, "_retire_recheck_at", {})

    class FakeCat:
        def __init__(self):
            self.mark_calls = 0

        def mark_unavailable(self, mid):
            self.mark_calls += 1

        def is_unavailable(self, mid):
            return False  # not yet parked before the first call fires

    cat = FakeCat()
    monkeypatch.setattr(app_mod, "catalog", cat)

    exc = exec_mod.AllFailedError(
        UpstreamError(400, "This model is unavailable for free"), []
    )
    # First call parks the model; an immediate second call is gated by the
    # cooldown and MUST NOT call mark_unavailable again.
    assert app_mod._flag_model_retired(exc, "muse-free") is True
    assert cat.mark_calls == 1
    assert app_mod._flag_model_retired(exc, "muse-free") is False
    assert cat.mark_calls == 1


def test_unit_a_runtime_retirement_survives_restart_via_persisted_state(tmp_path, monkeypatch):
    """A model parked by the recycler, persisted to retired_models.json, is
    reloaded on every gateway restart and STAYS parked across the restart --
    even when its persisted timestamp is hours old (well past any probation
    window the OLD design used to honour) AND /models keeps advertising it.

    _load_unavailable re-stamps the loaded entry to `now`, so the entry is
    treated as freshly parked again and there's no auto-resurrect pop to undo
    the retirement on the next refresh. This is the bug the operator saw: a
    parked deepseek that came back everywhere after a backend restart."""
    from core import config as core_config

    FetchProvider, UnifiedCatalog = _fetch_provider_factory()

    monkeypatch.setattr(core_config, "RETIRED_MODELS_FILE", tmp_path / "retired.json")
    monkeypatch.setattr(core_config, "RETIRED_MODELS_SEED", "")
    monkeypatch.setattr(core_config, "RETIRED_MODEL_TTL_DAYS", 7)

    prov = FetchProvider()
    cat = UnifiedCatalog({"opencode": prov})

    prov._ids = ["good-free", "dead-free"]
    cat.refresh(force=True)
    cat.mark_unavailable("dead-free")
    assert cat.is_unavailable("dead-free") is True

    # Back-date the persisted timestamp an hour so its original parked_at is
    # well past the 5-minute probation window the OLD design used to gate
    # auto-resurrect. Save back; a fresh catalog (simulating a backend
    # restart) loads it, re-stamps to `now`, and SHOULD keep dead-free
    # parked across the restart.
    cat._unavailable["dead-free"] = time.time() - 3600.0
    cat._save_unavailable()

    prov2 = FetchProvider()
    prov2._ids = ["good-free", "dead-free"]
    cat2 = UnifiedCatalog({"opencode": prov2})
    cat2.refresh(force=True)
    assert "dead-free" not in [m.id for m in cat2.free()]
    assert cat2.is_unavailable("dead-free") is True


def test_unit_a_runtime_retirement_expires_via_ttl_on_reload(tmp_path, monkeypatch):
    """TTL on LINGLING_RETIRED_MODEL_TTL_DAYS is the only auto-recover path
    now that auto-resurrect is gone. A parked entry older than the TTL is
    filtered out by _load_unavailable on the next gateway start, so when
    /models still lists the model the catalog offers it again -- giving
    OpenCode a week to resume serving it without an operator clearing
    retired_models.json."""
    from core import config as core_config
    import json

    FetchProvider, UnifiedCatalog = _fetch_provider_factory()

    monkeypatch.setattr(core_config, "RETIRED_MODELS_FILE", tmp_path / "retired.json")
    monkeypatch.setattr(core_config, "RETIRED_MODELS_SEED", "")
    monkeypatch.setattr(core_config, "RETIRED_MODEL_TTL_DAYS", 7)

    prov = FetchProvider()
    cat = UnifiedCatalog({"opencode": prov})

    prov._ids = ["good-free", "dead-free"]
    cat.refresh(force=True)
    cat.mark_unavailable("dead-free")
    assert cat.is_unavailable("dead-free") is True

    # Fast-forward the persisted clock past the 7-day TTL: write a timestamp
    # 8 days old straight into retired_models.json.
    path = tmp_path / "retired.json"
    path.write_text(json.dumps({"dead-free": time.time() - (8 * 86400)}),
                    encoding="utf-8")

    # A fresh gateway start drops the past-TTL entry, so the next refresh
    # that re-lists dead-free resurrects it on the catalog.
    prov2 = FetchProvider()
    prov2._ids = ["good-free", "dead-free"]
    cat2 = UnifiedCatalog({"opencode": prov2})
    cat2.refresh(force=True)
    assert "dead-free" in [m.id for m in cat2.free()]
    assert cat2.is_unavailable("dead-free") is False


def test_unit_responses_bridge_flushes_in_flight_on_reset_marker():
    """A ``lingling_reset`` chunk flushes in-flight items as ``incomplete``.

    The chat path retries a broken stream by reopening on a fresh exit IP and
    emitting a ``lingling_reset`` chat SSE frame telling the client to discard
    the partial. The Responses wire has no equivalent, so the bridge closes
    any in-flight reasoning/text item with status ``incomplete`` and lets the
    retry's deltas open a fresh ``output_item`` at the next output_index.
    Without this flush the two attempts concatenate into one message whose
    text is "partial" + "retry" with status ``completed`` -- a silently
    corrupt answer. See ``routing/responses_bridge._emit_in_flight_done``.
    """
    from routing import responses_bridge
    from routing.stream_guard import RESET_KEY

    # The exact retry marker ``stream_guard.reset_frame`` would produce.
    reset_frame = (
        b'data: {"' + RESET_KEY.encode()
        + b'":{"reason":"tunnel died","attempt":2},"choices":[]}'
    )
    chat = iter([
        # First attempt: a tiny content delta, then it breaks mid-flight.
        b'data: {"choices":[{"delta":{"content":"partial"}}]}',
        # ``guarded_stream`` emits this marker when it reopens upstream.
        reset_frame,
        # Retry: a fresh content delta, finish_reason, [DONE].
        b'data: {"choices":[{"delta":{"content":"retry answer"}}]}',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        b"data: [DONE]",
    ])
    body = b"".join(responses_bridge.stream_events(chat, "m")).decode()

    # Two ``output_item.added`` events: the partial at index 0, retry at 1.
    added = [
        int(part.split('"output_index":')[1].split(",")[0])
        for part in body.split("\n\n") if "response.output_item.added" in part
    ]
    assert added == [0, 1], added
    # The partial must close out as ``incomplete`` BEFORE the retry renders
    # anything; the first completed status belongs to the retry (or to the
    # final ``response.completed`` event).
    assert body.index('"status":"incomplete"') < body.index('"status":"completed"'), body
    assert body.index('"status":"incomplete"') < body.index('"text":"retry answer"'), body
    # Both halves survived the wire with their own text -- never concatenated
    # into one ``output_item``.
    assert '"text":"partial"' in body, body
    assert '"text":"retry answer"' in body, body
    # The bridge emitted a clean terminal event with the retry's summary status.
    assert '"status":"completed"' in body, body


def test_live_responses_stream_retries_on_a_fresh_exit_ip():
    """The ``/v1/responses`` streaming path retries a broken stream once.

    Mirrors the chat path's mid-flight recovery contract: a stream that dies
    after HTTP 200 is reopened on a fresh exit IP via the executor, with the
    bridge translating the chat path's ``lingling_reset`` marker into the
    Responses wire by closing the partial as ``incomplete`` and opening a
    fresh ``output_item`` for the retry. Before this was wired, the responses
    path had no retry -- a stream that broke was terminal ``stream_broken``
    while the chat path recovered its (the dashboard counted 80 broken
    muse-spark turns in one hour on ``/v1/responses``, ~51% break rate).

    Picks the model from ``catalog.free`` (NOT ``_live_free_ids``):
    ``catalog.free`` subtracts the runtime retirements persisted to
    ``retired_models.json`` during a live suite, while ``_live_free_ids`` only
    subtracts the seed retired set -- so an id a previous test parked mid-suite
    still appears in ``_live_free_ids`` but the handler's "Unknown model" gate
    rejects it before the retry branch this test exercises runs. OpenCode also
    retires free models without notice, so a hardcoded id reds against a model
    nobody serves anymore (the chat-path mirror
    ``test_unit_an_explicit_model_is_never_swapped_mid_stream`` first broke
    against the retired ``ling-3.0-flash-free`` -- it now picks its id from
    ``catalog.free`` too, so neither test carries a stale name any more).
    """
    import app as app_mod

    # Use ``app_mod.catalog`` -- the live module the handler resolves its own
    # ``catalog`` global against -- NOT the test_routing-py module-level
    # ``catalog`` bound at import time. ``test_live_apikey_gate`` reloads
    # ``app`` with ``importlib``, which rebinds ``sys.modules['app'].catalog``
    # but leaves our module-level name pointing at the OLD catalog instance; a
    # mid picked from the stale instance is rejected by the handler's "Unknown
    # model" gate before the retry branch this test exercises runs.
    ids = sorted({m.id for m in app_mod.catalog.free()})
    assert ids, "expected at least one live free model"
    mid = config.DISPATCHER_MODEL if config.DISPATCHER_MODEL in ids else ids[0]

    seen = []

    def fake_execute_stream(messages, model_id, providers, **kwargs):
        seen.append(model_id)
        def gen():
            if len(seen) == 1:
                # First attempt: emit a partial answer, then break mid-stream.
                yield b'data: {"choices":[{"delta":{"content":"partial"}}]}'
                raise RuntimeError("tunnel died mid-answer")
            # Retry: a fresh, complete answer -- the bridge's close-out text is
            # this verbatim, which only holds when the retry does not re-emit the
            # partial the first attempt was cut short on (the chat-path mirror
            # test happens to escape this by asserting on ``reset.model`` not text).
            yield b'data: {"choices":[{"delta":{"content":"retry"},"finish_reason":"stop"}]}'
        class _P:
            id = "opencode"
        return gen(), _P(), None, []

    original = app_mod.executor.execute_stream
    app_mod.executor.execute_stream = fake_execute_stream
    try:
        # The module-level client, not a fresh ``with TestClient(...)``:
        # entering one runs the lifespan hook that registers WARP identities.
        r = client.post("/v1/responses", json={
            "model": mid,
            "input": "Refactor this component",
            "stream": True,
        })
        assert r.status_code == 200, r.status_code
        body = b"".join(r.iter_bytes()).decode("utf-8")

        # Exactly one mid-flight retry on a fresh exit IP. The same model: the
        # request named it (not lingling-auto), so rerouting does not apply.
        assert len(seen) == 2, f"expected exactly one retry, got {seen}"
        assert seen == [mid, mid], seen
        # ``guarded_stream`` wrote a ``lingling_reset`` chat SSE frame; the bridge
        # consumes it rather than echoing -- on the Responses wire there is no
        # such field, so the marker must NOT appear downstream. Its presence
        # upstream is verified by the flush it triggered (the ``incomplete``
        # close-out below) and the fresh ``output_item.added`` slot the retry
        # opened afterwards.
        assert "lingling_reset" not in body, body[:600]
        assert '"status":"incomplete"' in body, body[:600]
        assert body.index('"status":"incomplete"') < body.index('"status":"completed"'), body
        # The two halves survived on separate output_item slots; the partial
        # closed at output_index 0 and the retry reopened fresh at 1.
        added = [
            int(part.split('"output_index":')[1].split(",")[0])
            for part in body.split("\n\n") if "response.output_item.added" in part
        ]
        assert added == [0, 1], added
        # Each half has its own text on the wire (never concatenated).
        assert '"text":"partial"' in body, body
        assert '"text":"retry"' in body, body
    finally:
        app_mod.executor.execute_stream = original


def test_live_responses_stream_falls_back_to_another_model_when_primary_exhausts():
    """``/v1/responses`` streaming falls back to another model when the primary
    exhausts *before* HTTP 200 -- the chat non-stream path's contract that
    ``/v1/responses`` was missing.

    ``execute_stream`` raises ``AllFailedError`` from inside the wait for the
    FIRST chunk, so no bytes have hit the wire when the streaming catch fires
    -- exactly the window the chat non-stream handler already swapped the model
    in. The streaming catch here was a terminal 503 ("No upstream stream started
    within X s"), indistinguishable from a 429 IP burn, so a Codex-shaped turn
    (tools / long context / reasoning payload) that OpenCode rejects with a
    non-retryable 400 -- while a one-line curl through ``/v1/chat/completions``
    non-stream recovered -- came back as a session-killing 503 to Codex 0.146.
    The streaming catch now mirrors the chat non-stream path: pick a
    sampler-cleared fallback, re-resolve effort, retry on the streaming path.
    If the fallback opens cleanly, the rest of the handler streams against the
    model that actually answered -- the wire headers and the usage ledger name
    it, not the one that died (a 400 on the named model must not poison the
    dashboard count for the model that rescued the turn).

    Picks ``primary`` and ``fallback`` from the live catalog (``catalog.free``),
    not from ``_live_free_ids``. The latter subtracts only the *seed* retired
    list; runtime retirements land in the data dir's ``retired_models.json``
    and are NOT subtracted, so an id a previous test in this suite parked
    mid-run still shows up in ``_live_free_ids`` but with no providers in the
    catalog. The ``/v1/responses`` handler's "Unknown model" gate at the top
    rejects it BEFORE the fallback branch we exercise runs, and this test
    reds through no fault of the fix -- exactly how the sibling
    ``test_live_responses_stream_retries_on_a_fresh_exit_ip`` reds elsewhere
    in the suite. ``catalog.free`` already subtracts the retired set, so the
    picked pair always has providers to dispatch against.
    """
    import app as app_mod
    from providers.base import UpstreamError as _UpstreamError
    from routing import executor as exec_mod

    # ``app_mod.catalog`` matches the handler's ``catalog`` global even after
    # ``test_live_apikey_gate`` reloads ``app`` via importlib -- the test_routing
    # module-level ``catalog`` would be left bound to the stale instance while
    # the handler resolves the fresh module's catalog. See the sibling
    # ``test_live_responses_stream_retries_on_a_fresh_exit_ip`` for the full
    # divergence explanation; the same fix applies here.
    ids = sorted({m.id for m in app_mod.catalog.free()})
    assert len(ids) >= 2, "expected at least two routable free models for a fallback"
    primary, fallback = ids[0], ids[1]

    seen = []

    def fake_execute_stream(messages, model_id, providers, **kwargs):
        seen.append(model_id)
        if model_id == primary:
            # Non-retryable upstream rejection (e.g. a Codex-shaped tool-calls
            # payload the upstream 400s over). The executor is mocked so no real
            # network round-trip happens, and no half-written answer is on the
            # wire -- exactly the pre-HTTP-200 window this test exercises.
            raise exec_mod.AllFailedError(
                _UpstreamError(400, "bad request shape", "opencode"), [],
            )

        def gen():
            yield b'data: {"choices":[{"delta":{"content":"hello"}}]}'
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'

        class _P:
            id = "opencode"

        return gen(), _P(), None, []

    orig_exec = app_mod.executor.execute_stream
    orig_fb = app_mod.dispatcher.fallback_model
    app_mod.executor.execute_stream = fake_execute_stream
    # The response is deterministically rerouted onto our picked fallback so
    # the assertion on its id is stable; the live catalog supplies the real
    # ``providers_for(fallback)`` the retry is dispatched against.
    app_mod.dispatcher.fallback_model = (
        lambda catalog, has_images, exclude=None, messages=None: fallback
    )
    try:
        # The module-level client, not a fresh ``with TestClient(...)``:
        # entering one runs the lifespan hook that registers WARP identities.
        r = client.post("/v1/responses", json={
            "model": primary,
            "input": "Refactor this component",
            "stream": True,
        })
        assert r.status_code == 200, r.status_code
        body = b"".join(r.iter_bytes()).decode("utf-8")

        # The primary exhausted pre-stream and the handler rerouted onto the
        # fallback, which streamed cleanly. Both models were attempted, in
        # order -- the primary first (it failed), then the fallback.
        assert seen == [primary, fallback], f"expected reroute, got {seen}"
        # Routing metadata on the wire names the model that actually answered,
        # not the one that died -- the dashboard and Codex's session UI read
        # these headers, and the usage ledger keys off the routed model.
        assert r.headers["X-Lingling-Routed-Model"] == fallback, (
            r.headers.get("X-Lingling-Routed-Model"))
        assert r.headers["X-Lingling-Routed-By"] == "fallback", (
            r.headers.get("X-Lingling-Routed-By"))
        # The bridge translated the chat SSE into Responses events; the
        # fallback's answer text survived on the wire and close-out is the
        # Responses ``status: completed`` marker, not ``incomplete`` (no
        # mid-flight retry fired -- the primary never got bytes out).
        assert '"text":"hello"' in body, body[:600]
        assert '"status":"completed"' in body, body[:600]
        assert '"status":"incomplete"' not in body, body[:600]
    finally:
        app_mod.executor.execute_stream = orig_exec
        app_mod.dispatcher.fallback_model = orig_fb


def test_live_chat_stream_falls_back_to_another_model_when_primary_exhausts():
    """``/v1/chat/completions`` streaming falls back to another model when the
    primary exhausts *before* HTTP 200 -- the parity the responses stream path
    and the chat non-stream path already had, which chat streaming was missing
    (a bare 503 "No upstream stream started within X s" while a non-stream curl
    of the same cooked model recovered -- the streaming-vs-non-stream asymmetry).

    Same mock as the sibling responses test: ``execute_stream`` raises
    ``AllFailedError`` for the primary (pre-200, no bytes on the wire) and
    streams cleanly for the fallback. Asserts both models were attempted in
    order, the wire headers name the fallback (not the model that died), and the
    fallback's raw chat SSE survived the passthrough.
    """
    import app as app_mod
    from providers.base import UpstreamError as _UpstreamError
    from routing import executor as exec_mod

    ids = sorted({m.id for m in app_mod.catalog.free()})
    assert len(ids) >= 2, "expected at least two routable free models for a fallback"
    primary, fallback = ids[0], ids[1]

    seen = []

    def fake_execute_stream(messages, model_id, providers, **kwargs):
        seen.append(model_id)
        if model_id == primary:
            raise exec_mod.AllFailedError(
                _UpstreamError(400, "bad request shape", "opencode"), [],
            )

        def gen():
            yield b'data: {"choices":[{"delta":{"content":"hello"}}]}'
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'

        class _P:
            id = "opencode"

        return gen(), _P(), None, []

    orig_exec = app_mod.executor.execute_stream
    orig_fb = app_mod.dispatcher.fallback_model
    app_mod.executor.execute_stream = fake_execute_stream
    app_mod.dispatcher.fallback_model = (
        lambda catalog, has_images, exclude=None, messages=None: fallback
    )
    try:
        r = client.post("/v1/chat/completions", json={
            "model": primary,
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        })
        assert r.status_code == 200, r.status_code
        body = b"".join(r.iter_bytes()).decode("utf-8")
        assert seen == [primary, fallback], f"expected reroute, got {seen}"
        assert r.headers["X-Lingling-Routed-Model"] == fallback, (
            r.headers.get("X-Lingling-Routed-Model"))
        assert r.headers["X-Lingling-Routed-By"] == "fallback", (
            r.headers.get("X-Lingling-Routed-By"))
        assert '"content":"hello"' in body, body[:600]
    finally:
        app_mod.executor.execute_stream = orig_exec
        app_mod.dispatcher.fallback_model = orig_fb


def test_live_messages_stream_falls_back_to_another_model_when_primary_exhausts():
    """``/v1/messages`` streaming falls back to another model when the primary
    exhausts *before* HTTP 200 -- the parity the chat/responses stream paths and
    this handler's own non-stream branch already had, which ``/v1/messages``
    streaming was missing (a bare 503 while a non-stream request recovered).

    Same mock as the sibling responses/chat tests. The Anthropic ``model`` field
    is a plain free id, which ``model_map.resolve`` returns unchanged (rule 1: a
    real served id is already an answer), so routing lands on it directly without
    the dispatcher. Asserts both models were attempted in order, the wire headers
    name the fallback, and the messages bridge translated the fallback's chat SSE
    into Anthropic events with the answer text surviving.
    """
    import app as app_mod
    from providers.base import UpstreamError as _UpstreamError
    from routing import executor as exec_mod

    ids = sorted({m.id for m in app_mod.catalog.free()})
    assert len(ids) >= 2, "expected at least two routable free models for a fallback"
    primary, fallback = ids[0], ids[1]

    seen = []

    def fake_execute_stream(messages, model_id, providers, **kwargs):
        seen.append(model_id)
        if model_id == primary:
            raise exec_mod.AllFailedError(
                _UpstreamError(400, "bad request shape", "opencode"), [],
            )

        def gen():
            yield b'data: {"choices":[{"delta":{"content":"hello"}}]}'
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'

        class _P:
            id = "opencode"

        return gen(), _P(), None, []

    orig_exec = app_mod.executor.execute_stream
    orig_fb = app_mod.dispatcher.fallback_model
    app_mod.executor.execute_stream = fake_execute_stream
    app_mod.dispatcher.fallback_model = (
        lambda catalog, has_images, exclude=None, messages=None: fallback
    )
    try:
        r = client.post("/v1/messages", json={
            "model": primary,
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 64,
            "stream": True,
        })
        assert r.status_code == 200, r.status_code
        body = b"".join(r.iter_bytes()).decode("utf-8")
        assert seen == [primary, fallback], f"expected reroute, got {seen}"
        assert r.headers["X-Lingling-Routed-Model"] == fallback, (
            r.headers.get("X-Lingling-Routed-Model"))
        assert r.headers["X-Lingling-Routed-By"] == "fallback", (
            r.headers.get("X-Lingling-Routed-By"))
        assert '"text":"hello"' in body, body[:600]
    finally:
        app_mod.executor.execute_stream = orig_exec
        app_mod.dispatcher.fallback_model = orig_fb
