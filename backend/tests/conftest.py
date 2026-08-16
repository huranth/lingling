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

