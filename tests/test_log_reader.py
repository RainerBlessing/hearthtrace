from pathlib import Path

from hs_tracker.log_reader import find_latest_power_log


def test_find_latest_power_log_picks_newest_folder(tmp_path: Path) -> None:
    logs_dir = tmp_path / "Logs"
    older = logs_dir / "Hearthstone_2026_09_01_10_00_00"
    newer = logs_dir / "Hearthstone_2026_09_06_18_30_00"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    (older / "Power.log").write_text("old\n")
    (newer / "Power.log").write_text("new\n")

    result = find_latest_power_log(logs_dir)

    assert result == newer / "Power.log"


def test_find_latest_power_log_returns_none_when_no_folders(tmp_path: Path) -> None:
    logs_dir = tmp_path / "Logs"
    logs_dir.mkdir()

    assert find_latest_power_log(logs_dir) is None
