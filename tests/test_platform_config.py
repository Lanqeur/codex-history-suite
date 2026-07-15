from __future__ import annotations

from pathlib import Path

from codex_history.config import (
    default_codex_homes,
    default_data_home,
    load_config,
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


def test_initial_config_has_portable_runtime_override(tmp_path):
    write_initial_config(
        tmp_path,
        profile="default",
        source_roots=[tmp_path / "codex"],
    )
    config = load_config(tmp_path)
    assert config.runtime_python == ""
