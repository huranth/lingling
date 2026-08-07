"""Hermetic tests for the Codex API-key setup window (backend/codex/setup_gui.py).

The window mirrors claudecode/setup_gui.py but writes ``~/.codex/config.toml``
and persists ``LINGLING_API_KEY``. These tests exercise the config wiring
against a temp CONFIG_PATH; ``persist=False`` keeps ``setx`` out of the picture.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ["LINGLING_REQUIRE_KEY"] = "0"

from codex import setup_gui  # noqa: E402


def test_unit_top_level_keys_land_above_the_first_section(tmp_path):
    setup_gui.CONFIG_PATH = tmp_path / "config.toml"
    text = setup_gui._ensure_top_level(
        "[projects.x]\ntrust=1\n", "model_provider", "lingling"
    )
    assert text == 'model_provider = "lingling"\n[projects.x]\ntrust=1\n'


def test_unit_top_level_keeps_an_existing_key(tmp_path):
    setup_gui.CONFIG_PATH = tmp_path / "config.toml"
    text = setup_gui._ensure_top_level(
        'model_provider = "other"\n[projects.x]\ntrust=1\n',
        "model_provider", "lingling",
    )
    assert 'model_provider = "other"' in text
    assert text.count("model_provider") == 1


def test_unit_codex_base_url_is_normalised_to_v1(tmp_path):
    # Codex appends /responses to base_url without adding /v1, so the stored
    # base_url must carry the /v1 prefix (this was the 404 regression).
    assert setup_gui.codex_base_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000/v1"
    assert setup_gui.codex_base_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000/v1"
    assert setup_gui.codex_base_url("http://127.0.0.1:8000/v1") == "http://127.0.0.1:8000/v1"
    assert setup_gui.codex_base_url("") == "http://127.0.0.1:8000/v1"


def test_unit_provider_section_is_added_for_a_fresh_config(tmp_path):
    setup_gui.CONFIG_PATH = tmp_path / "config.toml"
    out = setup_gui._provider_section("", "http://127.0.0.1:8000", "ll_abc")
    assert "[model_providers.lingling]" in out
    assert 'env_key = "LINGLING_API_KEY"' in out
    assert 'wire_api = "responses"' in out
    assert 'base_url = "http://127.0.0.1:8000/v1"' in out


def test_unit_provider_section_merge_preserves_unrelated_keys(tmp_path):
    setup_gui.CONFIG_PATH = tmp_path / "config.toml"
    text = (
        'model_provider = "lingling"\n'
        "[model_providers.lingling]\n"
        'name = "Lingling"\n'
        'base_url = "http://old:8000/v1"\n'
        'extra = "keep me"\n'
        "\n"
        "[projects.x]\ntrust=1\n"
    )
    out = setup_gui._provider_section(text, "http://127.0.0.1:8000", "ll_abc")
    assert 'extra = "keep me"' in out
    assert 'base_url = "http://127.0.0.1:8000/v1"' in out
    assert 'env_key = "LINGLING_API_KEY"' in out
    assert "[projects.x]" in out
    assert out.count("[model_providers.lingling]") == 1


def test_unit_keyless_config_omits_env_key(tmp_path):
    setup_gui.CONFIG_PATH = tmp_path / "config.toml"
    out = setup_gui._provider_section("", "http://127.0.0.1:8000", "")
    assert "env_key" not in out
    assert "[model_providers.lingling]" in out


def test_unit_apply_wires_the_key_without_setx(tmp_path):
    setup_gui.CONFIG_PATH = tmp_path / "config.toml"
    result = setup_gui.apply("http://127.0.0.1:8000", "ll_abc123", persist=False)
    assert result["keyless"] is False
    assert result["persisted"] is False
    text = setup_gui.CONFIG_PATH.read_text(encoding="utf-8")
    assert 'model_provider = "lingling"' in text
    assert 'env_key = "LINGLING_API_KEY"' in text
    # The token itself never lives in config.toml -- Codex reads it from the env.
    assert "ll_abc123" not in text


def test_unit_apply_keyless_writes_no_env_key(tmp_path):
    setup_gui.CONFIG_PATH = tmp_path / "config.toml"
    result = setup_gui.apply("http://127.0.0.1:8000", "", persist=False)
    assert result["keyless"] is True
    text = setup_gui.CONFIG_PATH.read_text(encoding="utf-8")
    assert "env_key" not in text