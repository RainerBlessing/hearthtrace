from pathlib import Path

import pytest

from hs_tracker.config import ConfigError, load_config


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
