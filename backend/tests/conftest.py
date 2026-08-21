"""Pytest fixtures for race condition tests."""
import os
import tempfile
import pytest


@pytest.fixture
def usage_db_path():
    """Create a temporary SQLite database for testing usage store."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name

    yield path

    # Cleanup
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _isolated_egress_map():
    """Keep the learned WARP edge->exit map out of every unit test.

    Healing paths consult the map, and which DATA_DIR the test process sees
    depends on which module imported core.config first -- so a map written by
    a live gateway in the repo's data dir could leak into a test run and
    change healer behavior mid-suite. Each test gets a fresh, private map.
    """
    from warp import egress_map
    egress_map.reset_for_tests()
    state = {}
    saved_load, saved_save = egress_map._load, egress_map._save
    egress_map._load = lambda: state
    egress_map._save = lambda data: None
    try:
        yield
    finally:
        egress_map._load, egress_map._save = saved_load, saved_save
        egress_map.reset_for_tests()


@pytest.fixture(autouse=True)
def _isolated_active_streams():
    """Reset the in-flight stream registry around every test.

    The healers read this registry at runtime to decide whether an egress has
    a request streaming through it. A stream_chat test that inc'd a count and
    failed to balance it (an interrupted generator, a leak) would otherwise
    spuriously defer a healer in a later probe test -- a flaky cross-test
    failure. Reset before and after each test so each owns a clean registry.
    """
    from providers import active_streams
    active_streams.reset()
    yield
    active_streams.reset()


@pytest.fixture(autouse=True)
def _isolated_pacing_memory(tmp_path):
    """Keep the runtime-learned reasoning registry private to each test.

    ``mark_reasoning`` persists to ``data/reasoning_models.json``; without
    isolation a test marking a model would write a learned entry into the live
    gateway's data dir, and a stale file could mark models for an unrelated
    run. Each test gets a fresh, in-memory registry pointed at a throwaway file.
    """
    from core import config
    from routing import pacing_memory
    saved_file = config.REASONING_LEARNED_FILE
    config.REASONING_LEARNED_FILE = tmp_path / "reasoning_models.json"
    pacing_memory.reset_for_test()
    try:
        yield
    finally:
        pacing_memory.reset_for_test()
        config.REASONING_LEARNED_FILE = saved_file


@pytest.fixture(autouse=True)
def _isolated_sampler():
    """Keep the post-heal sampler's state and config knobs private per test.

    The executor consults ``routing.sampler.ok_exits`` on every request, and the
    healers/health daemon can populate it, so a sampler result left by one test
    could change another test's executor behaviour (routing to a phantom exit,
    or fail-fast on a stale cooked verdict). Each test gets a freshly cleared
    registry, and any config knob the test set is restored so it cannot leak
    into the next.
    """
    from core import config
    from routing import sampler
    saved = {
        "ENABLED": config.SAMPLER_ENABLED,
        "MODELS": list(config.SAMPLER_MODELS),
        "INTERVAL_S": config.SAMPLER_INTERVAL_S,
        "TTL_S": config.SAMPLER_TTL_S,
        "FAIL_FAST": config.SAMPLER_FAIL_FAST_ATTEMPTS,
        "MAX_ATTEMPTS": config.PROXY_MAX_ATTEMPTS_PER_REQUEST,
    }
    sampler.reset_for_test()
    try:
        yield
    finally:
        sampler.reset_for_test()
        config.SAMPLER_ENABLED = saved["ENABLED"]
        config.SAMPLER_MODELS = saved["MODELS"]
        config.SAMPLER_INTERVAL_S = saved["INTERVAL_S"]
        config.SAMPLER_TTL_S = saved["TTL_S"]
        config.SAMPLER_FAIL_FAST_ATTEMPTS = saved["FAIL_FAST"]
        config.PROXY_MAX_ATTEMPTS_PER_REQUEST = saved["MAX_ATTEMPTS"]

