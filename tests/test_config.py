from pathlib import Path

import pytest

from hearthtrace.config import ConfigError, load_config


def test_load_config_reads_valid_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'logs_dir = "/mnt/F/Programme/Hearthstone/Logs"\n'
        'export_dir = "/home/rainer/HearthstoneAnalysis"\n'
    )

    config = load_config(config_path)

    assert config.logs_dir == Path("/mnt/F/Programme/Hearthstone/Logs")
    assert config.export_dir == Path("/home/rainer/HearthstoneAnalysis")


def test_load_config_creates_template_when_missing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"

    with pytest.raises(ConfigError):
        load_config(config_path)

    assert config_path.exists()


def test_load_config_wraps_invalid_toml_syntax_as_config_error(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("this is not = valid [ toml")

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_load_config_wraps_missing_logs_dir_key_as_config_error(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('export_dir = "/home/rainer/HearthstoneAnalysis"\n')

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_load_config_wraps_wrong_typed_value_as_config_error(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    # A syntactically valid TOML file, but `export_dir` is a bare TOML date
    # literal rather than a string -- Path(...) would otherwise raise an
    # unhandled TypeError instead of a clean, user-facing ConfigError.
    config_path.write_text(
        'logs_dir = "/mnt/F/Programme/Hearthstone/Logs"\n'
        "export_dir = 2026-09-06\n"
    )

    with pytest.raises(ConfigError):
        load_config(config_path)
