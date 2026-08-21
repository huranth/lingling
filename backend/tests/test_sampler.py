"""Hermetic tests for the post-heal multi-model sampler.

The probe primitive (``warp.probe._probe_single``) is monkeypatched so no real
network leaves the process; the canary summary, pool, and catalog are faked.
This exercises the attribution (ok / partial / cooked / rate_limited / unknown),
the request-path accessors (ok_exits semantics, TTL), the skip paths, the pool's
``pick_from`` restriction, and the executor's routing + fail-fast consumption.
"""
from __future__ import annotations

import time

import pytest

from core import config
from providers.base import UpstreamError
from providers.proxy_pool import ProxyPool
from routing import executor, sampler
from warp import probe as warp_probe


# --- fakes -----------------------------------------------------------------


class _Model:
    def __init__(self, mid, reasoning=False):
        self.id = mid
        self.reasoning = reasoning


class _Cat:
    """Minimal catalog: by_id -> _Model or None."""

    def __init__(self, known):
        self._known = known

    def by_id(self, m):
        return self._known.get(m)


class _OpencodeProv:
    """A keyless OpenCode provider whose chat_completions always fails."""

    id = "opencode"
    base_url = config.OPENCODE_BASE_URL

    def is_configured(self):
        return True

    def requires_key(self):
        return False

    def needs_proxy(self):
        return True

    def prefer_direct(self, model_id):
        return False

    def chat_completions(self, *a, **k):
        raise UpstreamError(429, "rate limit", self.id)


class _OtherProv:
    id = "openai"
    base_url = "https://api.openai.com/v1"

    def is_configured(self):
        return True

    def requires_key(self):
        return False

    def needs_proxy(self):
        return True

    def prefer_direct(self, model_id):
        return False

    def chat_completions(self, *a, **k):
        raise UpstreamError(429, "rate limit", self.id)


def _make_pool(ids):
    pool = ProxyPool()
    for i, pid in enumerate(ids):
        pool.add(f"socks5://127.0.0.1:{9000 + i}", proxy_id=pid)
    return pool


def _canary_summary(ok_ids, extra=()):
    """A ProbeSummary where ``ok_ids`` are green and ``extra`` is (id, status)."""
    results = [
        warp_probe.ProbeResult(proxy_id=pid, status="ok", latency_ms=10.0,
                               probed_at=time.time())
        for pid in ok_ids
    ]
    for pid, st in extra:
        results.append(warp_probe.ProbeResult(proxy_id=pid, status=st, probed_at=time.time()))
    return warp_probe.ProbeSummary(
        total=len(results), healthy=len(ok_ids),
        results=results, completed_at=time.time(),
    )


def _patch_probe(monkeypatch, table, sink=None):
    """Patch _probe_single to return ``table[(proxy_id, model)]`` (default ok).

    Records every (proxy_id, model) it was asked about into ``sink``.
    """
    def fake(proxy_url, proxy_id, model, base_url, timeout, max_tokens=None):
        if sink is not None:
            sink.append(proxy_id)
        st = table.get((proxy_id, model), "ok")
        return warp_probe.ProbeResult(proxy_id=proxy_id, status=st,
                                      latency_ms=10.0, probed_at=time.time())
    monkeypatch.setattr(warp_probe, "_probe_single", fake)
    sampler.reset_for_test()  # clear the memoized reference so the fake is used


def _quiet(*a, **k):
    pass


DEEP = "deepseek-v4-flash-free"
MUSE = "muse-spark-1.2-contributor-free"


@pytest.fixture
def enabled():
    config.SAMPLER_ENABLED = True
    config.SAMPLER_TTL_S = 600
    config.SAMPLER_INTERVAL_S = 300
    yield


def test_sampler_probe_budget_reasoning_vs_fast():
    """A reasoning or long-thinking model needs a bigger probe max_tokens, or
    opencode 400s the tiny liveness probe (muse-spark at max_tokens=5) and the
    sampler false-cooks a healthy model. Fast models stay cheap."""
    cat = _Cat({
        DEEP: _Model(DEEP, reasoning=True),
        MUSE: _Model(MUSE, reasoning=True),
        "fast-free": _Model("fast-free", reasoning=False),
    })
    assert sampler._sampler_probe_budget(DEEP, cat) == config.PROBE_REASONING_MAX_TOKENS
    assert sampler._sampler_probe_budget(MUSE, cat) == config.PROBE_REASONING_MAX_TOKENS
    assert sampler._sampler_probe_budget("fast-free", cat) == config.PROBE_MAX_TOKENS
    # A model in LONG_THINKING_MODELS gets the reasoning budget even absent a
    # catalog entry (the live muse-spark case rode this branch).
    saved = config.LONG_THINKING_MODELS
    try:
        config.LONG_THINKING_MODELS = frozenset({"unlisted-reasoner-free"})
        assert sampler._sampler_probe_budget("unlisted-reasoner-free", _Cat({})) == config.PROBE_REASONING_MAX_TOKENS
        assert sampler._sampler_probe_budget("unlisted-reasoner-free", None) == config.PROBE_REASONING_MAX_TOKENS
    finally:
        config.LONG_THINKING_MODELS = saved


# --- accessors with no data -------------------------------------------------


def test_no_data_returns_none():
    sampler.reset_for_test()
    assert sampler.ok_exits(DEEP) is None
    assert sampler.is_cooked(DEEP) is False
    assert sampler.latest() is None


# --- attribution -----------------------------------------------------------


def test_sample_all_ok(monkeypatch, enabled):
    ids = ["warp-1", "warp-2", "warp-3"]
    pool = _make_pool(ids)
    canary = _canary_summary(ids)
    cat = _Cat({DEEP: _Model(DEEP, reasoning=True)})
    _patch_probe(monkeypatch, {})
    res = sampler.sample_models(pool, cat, canary, log=_quiet, models=[DEEP])

    ms = res.models[0]
    assert ms.status == "ok"
    assert set(ms.ok_exits) == set(ids)
    assert sampler.ok_exits(DEEP) == frozenset(ids)
    assert sampler.is_cooked(DEEP) is False


def test_sample_partial_routes_to_ok_subset(monkeypatch, enabled):
    ids = ["warp-1", "warp-2", "warp-3"]
    pool = _make_pool(ids)
    canary = _canary_summary(ids)
    cat = _Cat({DEEP: _Model(DEEP, reasoning=True)})
    table = {("warp-3", DEEP): "rate_limited"}
    _patch_probe(monkeypatch, table)
    res = sampler.sample_models(pool, cat, canary, log=_quiet, models=[DEEP])

    ms = res.models[0]
    assert ms.status == "partial"
    assert set(ms.ok_exits) == {"warp-1", "warp-2"}
    assert sampler.ok_exits(DEEP) == frozenset({"warp-1", "warp-2"})
    assert sampler.is_cooked(DEEP) is False


def test_sample_cooked_model_side_fail_fast(monkeypatch, enabled):
    ids = ["warp-1", "warp-2", "warp-3"]
    pool = _make_pool(ids)
    canary = _canary_summary(ids)
    cat = _Cat({MUSE: _Model(MUSE, reasoning=True)})
    table = {(p, MUSE): "probe_error" for p in ids}  # OpenCode refused the model
    _patch_probe(monkeypatch, table)
    res = sampler.sample_models(pool, cat, canary, log=_quiet, models=[MUSE])

    ms = res.models[0]
    assert ms.status == "cooked"
    assert ms.ok_exits == []
    assert sampler.ok_exits(MUSE) == frozenset()
    assert sampler.is_cooked(MUSE) is True


def test_sample_all_rate_limited_is_fail_fast(monkeypatch, enabled):
    ids = ["warp-1", "warp-2", "warp-3"]
    pool = _make_pool(ids)
    canary = _canary_summary(ids)
    cat = _Cat({DEEP: _Model(DEEP, reasoning=True)})
    table = {(p, DEEP): "rate_limited" for p in ids}
    _patch_probe(monkeypatch, table)
    res = sampler.sample_models(pool, cat, canary, log=_quiet, models=[DEEP])

    ms = res.models[0]
    assert ms.status == "rate_limited"
    assert ms.ok_exits == []
    assert sampler.ok_exits(DEEP) == frozenset()
    assert sampler.is_cooked(DEEP) is True  # no usable exit -> fail fast


def test_sample_all_dead_is_unknown(monkeypatch, enabled):
    ids = ["warp-1", "warp-2", "warp-3"]
    pool = _make_pool(ids)
    canary = _canary_summary(ids)
    cat = _Cat({DEEP: _Model(DEEP, reasoning=True)})
    table = {(p, DEEP): "dead" for p in ids}
    _patch_probe(monkeypatch, table)
    res = sampler.sample_models(pool, cat, canary, log=_quiet, models=[DEEP])

    ms = res.models[0]
    # All dead + no model-side refusal: cannot attribute an OpenCode-side
    # outage, so stay neutral (request path untouched).
    assert ms.status == "unknown"
    assert sampler.ok_exits(DEEP) is None
    assert sampler.is_cooked(DEEP) is False


def test_model_not_in_catalog_is_unknown(monkeypatch, enabled):
    ids = ["warp-1", "warp-2"]
    pool = _make_pool(ids)
    canary = _canary_summary(ids)
    cat = _Cat({})  # nothing in catalog
    seen = []
    _patch_probe(monkeypatch, {}, sink=seen)
    res = sampler.sample_models(pool, cat, canary, log=_quiet, models=["ghost-model"])

    ms = res.models[0]
    assert ms.status == "unknown"
    assert sampler.ok_exits("ghost-model") is None
    assert seen == []  # not probed (would 400 on every exit otherwise)


# --- skip paths ------------------------------------------------------------


def test_canary_no_green_skips(monkeypatch, enabled):
    pool = _make_pool(["warp-1", "warp-2"])
    canary = _canary_summary([], extra=[("warp-1", "dead"), ("warp-2", "rate_limited")])
    cat = _Cat({DEEP: _Model(DEEP)})
    seen = []
    _patch_probe(monkeypatch, {}, sink=seen)
    res = sampler.sample_models(pool, cat, canary, log=_quiet, models=[DEEP])

    assert res.skipped_reason == "no_canary_green"
    assert res.models == []
    assert sampler.ok_exits(DEEP) is None
    assert seen == []  # nothing sampled


def test_green_left_pool_skips(monkeypatch, enabled):
    pool = _make_pool(["warp-9"])  # canary said warp-1 was green, but it's gone
    canary = _canary_summary(["warp-1"])
    cat = _Cat({DEEP: _Model(DEEP)})
    _patch_probe(monkeypatch, {})
    res = sampler.sample_models(pool, cat, canary, log=_quiet, models=[DEEP])

    assert res.skipped_reason == "pool_empty"
    assert res.models == []


def test_disabled_returns_none(monkeypatch):
    config.SAMPLER_ENABLED = False
    sampler.reset_for_test()
    pool = _make_pool(["warp-1"])
    canary = _canary_summary(["warp-1"])
    res = sampler.sample_models(pool, _Cat({}), canary, log=_quiet, models=[DEEP])
    assert res is None
    snap = sampler.latest()
    assert snap["enabled"] is False
    assert sampler.ok_exits(DEEP) is None
    assert sampler.should_run() is False


def test_no_models_skips(monkeypatch, enabled):
    pool = _make_pool(["warp-1"])
    canary = _canary_summary(["warp-1"])
    _patch_probe(monkeypatch, {})
    res = sampler.sample_models(pool, _Cat({}), canary, log=_quiet, models=[])
    assert res.skipped_reason == "no_models"


# --- TTL + should_run ------------------------------------------------------


def test_ttl_ages_out(monkeypatch, enabled):
    config.SAMPLER_TTL_S = 0.1
    ids = ["warp-1", "warp-2"]
    pool = _make_pool(ids)
    canary = _canary_summary(ids)
    _patch_probe(monkeypatch, {})
    sampler.sample_models(pool, _Cat({DEEP: _Model(DEEP)}), canary,
                          log=_quiet, models=[DEEP])
    assert sampler.ok_exits(DEEP) is not None  # fresh
    time.sleep(0.15)
    assert sampler.ok_exits(DEEP) is None  # aged out -> request path untouched


def test_should_run_gating(enabled):
    config.SAMPLER_INTERVAL_S = 1000
    sampler.reset_for_test()
    assert sampler.should_run() is True  # never sampled -> first pass allowed
    sampler._store(sampler.SamplerResult(sampled_at=time.time(), enabled=True))
    assert sampler.should_run() is False  # within the interval
    assert sampler.should_run(now=time.time() + 2000) is True


# --- only green exits are probed ------------------------------------------


def test_only_green_exits_probed(monkeypatch, enabled):
    pool_ids = ["warp-1", "warp-2", "warp-3", "warp-4", "warp-5"]
    pool = _make_pool(pool_ids)
    canary = _canary_summary(
        ["warp-1", "warp-2", "warp-3"],
        extra=[("warp-4", "dead"), ("warp-5", "rate_limited")],
    )
    seen = []
    _patch_probe(monkeypatch, {}, sink=seen)
    sampler.sample_models(pool, _Cat({DEEP: _Model(DEEP)}), canary,
                          log=_quiet, models=[DEEP])
    assert set(seen) == {"warp-1", "warp-2", "warp-3"}


# --- ProxyPool.pick_from ---------------------------------------------------


def test_pick_from_restricts_to_candidates():
    pool = _make_pool(["warp-1", "warp-2", "warp-3"])

    # A single candidate is returned even though others are equally idle.
    assert pool.pick_from({"warp-2"}).id == "warp-2"

    # A candidate not in the pool falls back to a normal pick.
    assert pool.pick_from({"warp-99"}).id in {"warp-1", "warp-2", "warp-3"}

    # Empty candidate set falls back to a normal pick (no-op restriction).
    assert pool.pick_from(set()).id in {"warp-1", "warp-2", "warp-3"}

    # A candidate in cooldown is still the soonest, so it is returned (the one
    # the pool would otherwise park on).
    target = pool.get_by_id("warp-1")
    pool.mark_failure(target, 429)
    assert target.in_cooldown()
    assert pool.pick_from({"warp-1"}).id == "warp-1"


def test_pick_from_sticky_reuses_candidate(enabled):
    saved = config.PROXY_STICKY_SESSIONS
    config.PROXY_STICKY_SESSIONS = True
    try:
        pool = _make_pool(["warp-1", "warp-2", "warp-3"])
        # First call pins the session to a candidate.
        first = pool.pick_from({"warp-1", "warp-2"}, session_id="sess-1")
        assert first.id in {"warp-1", "warp-2"}
        # A later call for the same session reuses it as long as it is a candidate.
        again = pool.pick_from({"warp-1", "warp-2"}, session_id="sess-1")
        assert again.id == first.id
        # The sticky pin is ignored when it is no longer a candidate.
        other = pool.pick_from({"warp-3"}, session_id="sess-1")
        assert other.id == "warp-3"
    finally:
        config.PROXY_STICKY_SESSIONS = saved


# --- cooked_models accessor -------------------------------------------------


def test_cooked_models_excludes_ok_partial_unknown(monkeypatch, enabled):
    # Only "cooked" and "rate_limited" verdicts belong in the fallback-exclusion
    # set the dispatcher consumes; ok/partial/unknown models stay fallback targets.
    NEMO = "nemotron-3-ultra-free"
    BIG = "big-pickle"
    NORTH = "north-mini-code-free"
    ids = ["warp-1", "warp-2", "warp-3"]
    pool = _make_pool(ids)
    canary = _canary_summary(ids)
    cat = _Cat({DEEP: _Model(DEEP), MUSE: _Model(MUSE),
                NEMO: _Model(NEMO), BIG: _Model(BIG)})  # NORTH absent -> unknown
    table = {
        **{(p, DEEP): "rate_limited" for p in ids},    # rate_limited -> exclude
        **{(p, MUSE): "probe_error" for p in ids},     # cooked -> exclude
        ("warp-1", NEMO): "rate_limited",               # partial -> keep
        ("warp-2", NEMO): "ok",
        ("warp-3", NEMO): "ok",
        **{(p, BIG): "ok" for p in ids},                # ok -> keep
    }
    _patch_probe(monkeypatch, table)
    sampler.sample_models(pool, cat, canary, log=_quiet,
                          models=[DEEP, MUSE, NEMO, BIG, NORTH])

    cooked = sampler.cooked_models()
    assert cooked == frozenset({DEEP, MUSE})
    assert NEMO not in cooked
    assert BIG not in cooked
    assert NORTH not in cooked  # not-in-catalog -> unknown -> not excluded


def test_cooked_models_empty_without_data():
    sampler.reset_for_test()
    assert sampler.cooked_models() == frozenset()


# --- executor consumption --------------------------------------------------


def test_sampler_applies_scopes_to_opencode():
    assert executor._sampler_applies(_OpencodeProv()) is True
    assert executor._sampler_applies(_OtherProv()) is False


def test_pick_proxy_routes_to_ok_exits(monkeypatch, enabled):
    ids = ["warp-1", "warp-2", "warp-3"]
    pool = _make_pool(ids)
    canary = _canary_summary(ids)
    _patch_probe(monkeypatch, {("warp-1", DEEP): "rate_limited",
                               ("warp-3", DEEP): "rate_limited"})
    sampler.sample_models(pool, _Cat({DEEP: _Model(DEEP)}), canary,
                          log=_quiet, models=[DEEP])

    p = executor._pick_proxy(_OpencodeProv(), pool, "sess", DEEP,
                             ok_set=sampler.ok_exits(DEEP))
    assert p.id == "warp-2"  # the only ok exit


def test_pick_proxy_unchanged_without_data():
    pool = _make_pool(["warp-1", "warp-2", "warp-3"])
    sampler.reset_for_test()
    p = executor._pick_proxy(_OpencodeProv(), pool, "", DEEP,
                             ok_set=sampler.ok_exits(DEEP))
    assert p.id in {"warp-1", "warp-2", "warp-3"}  # normal selection


def test_execute_nonstream_fail_fast_when_cooked(monkeypatch, enabled):
    config.PROXY_MAX_ATTEMPTS_PER_REQUEST = 5
    config.SAMPLER_FAIL_FAST_ATTEMPTS = 1
    ids = ["warp-1", "warp-2", "warp-3", "warp-4", "warp-5"]
    pool = _make_pool(ids)
    canary = _canary_summary(ids)
    _patch_probe(monkeypatch, {(p, DEEP): "probe_error" for p in ids})
    sampler.sample_models(pool, _Cat({DEEP: _Model(DEEP)}), canary,
                          log=_quiet, models=[DEEP])
    assert sampler.is_cooked(DEEP)

    prov = _OpencodeProv()
    messages = [{"role": "user", "content": "hi"}]
    with pytest.raises(executor.AllFailedError) as ei:
        executor.execute_nonstream(messages, DEEP, [prov], proxy_pool=pool)
    # Cooked -> exactly one egress attempt, not the full 5.
    assert len(ei.value.attempts) == 1


def test_execute_nonstream_no_data_churns_full_budget():
    config.PROXY_MAX_ATTEMPTS_PER_REQUEST = 5
    config.SAMPLER_FAIL_FAST_ATTEMPTS = 1
    pool = _make_pool(["warp-1", "warp-2", "warp-3", "warp-4", "warp-5"])
    sampler.reset_for_test()  # no sampler data -> ok_exits None -> unchanged
    assert sampler.ok_exits(DEEP) is None

    prov = _OpencodeProv()
    messages = [{"role": "user", "content": "hi"}]
    with pytest.raises(executor.AllFailedError) as ei:
        executor.execute_nonstream(messages, DEEP, [prov], proxy_pool=pool)
    # Without sampler data the executor exhausts its full per-proxy budget.
    assert len(ei.value.attempts) == 5


def test_execute_nonstream_ignores_sampler_for_other_upstream(monkeypatch, enabled):
    # A cooked verdict on OpenCode must NOT fail-fast a different upstream
    # provider (the verdict says nothing about whether it will serve the model).
    config.PROXY_MAX_ATTEMPTS_PER_REQUEST = 5
    config.SAMPLER_FAIL_FAST_ATTEMPTS = 1
    ids = ["warp-1", "warp-2", "warp-3", "warp-4", "warp-5"]
    pool = _make_pool(ids)
    canary = _canary_summary(ids)
    _patch_probe(monkeypatch, {(p, DEEP): "probe_error" for p in ids})
    sampler.sample_models(pool, _Cat({DEEP: _Model(DEEP)}), canary,
                          log=_quiet, models=[DEEP])
    assert sampler.is_cooked(DEEP)

    prov = _OtherProv()  # different upstream -> sampler verdict does not apply
    messages = [{"role": "user", "content": "hi"}]
    with pytest.raises(executor.AllFailedError) as ei:
        executor.execute_nonstream(messages, DEEP, [prov], proxy_pool=pool)
    assert len(ei.value.attempts) == 5  # full budget, no fail-fast
