from __future__ import annotations

from pathlib import Path

import pytest

from codex_history.config import (
    default_codex_homes,
    default_data_home,
    load_config,
    resolve_summarization,
    write_initial_config,
)


def test_platform_data_homes():
    assert default_data_home(
        platform="win32", env={"LOCALAPPDATA": "C:/Users/A/AppData/Local"}, home=Path("C:/Users/A")
    ) == Path("C:/Users/A/AppData/Local/codex-history")
    assert default_data_home(platform="darwin", env={}, home=Path("/Users/a")) == Path(
        "/Users/a/Library/Application Support/codex-history"
    )
    assert default_data_home(platform="linux", env={}, home=Path("/home/a")) == Path(
        "/home/a/.local/share/codex-history"
    )
    assert default_data_home(
        platform="linux", env={"XDG_DATA_HOME": "/data"}, home=Path("/home/a")
    ) == Path("/data/codex-history")


def test_codex_home_override_is_first():
    homes = default_codex_homes(
        platform="linux",
        env={"CODEX_HOME": "/custom/codex"},
        home=Path("/home/a"),
    )
    assert homes[0] == Path("/custom/codex")
    assert Path("/home/a/.codex") in homes


def test_wsl_discovers_linux_and_windows_homes():
    homes = default_codex_homes(
        platform="linux",
        env={"WSL_DISTRO_NAME": "Ubuntu", "WIN_USERNAME": "Alice"},
        home=Path("/home/alice"),
    )
    assert Path("/home/alice/.codex") in homes
    assert Path("/mnt/c/Users/Alice/.codex") in homes


def test_initial_config_is_model_first_with_safe_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    write_initial_config(
        tmp_path,
        profile="default",
        source_roots=[tmp_path / "codex"],
    )
    config = load_config(tmp_path)
    assert config.runtime_python == ""
    assert config.summary_mode == "auto"
    assert config.summary_model == "deepseek-v4-flash"
    assert config.summary_input_price_cny == 1.0
    assert config.summary_cached_input_price_cny == 0.2
    assert config.summary_output_price_cny == 2.0
    resolution = resolve_summarization(config)
    assert resolution["effective_mode"] == "extractive"
    assert resolution["fallback"] is True

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only")
    resolution = resolve_summarization(config)
    assert resolution["effective_mode"] == "openai-compatible"
    assert resolution["api_key_available"] is True


def test_invalid_estimation_ratio_is_rejected(tmp_path):
    path = write_initial_config(
        tmp_path,
        profile="default",
        source_roots=[tmp_path / "codex"],
    )
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "cached_input_ratio = 0.0", "cached_input_ratio = 1.2"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cached_input_ratio"):
        load_config(tmp_path)
