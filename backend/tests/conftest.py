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

