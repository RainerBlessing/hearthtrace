from pathlib import Path

from hs_tracker.log_reader import LogWatcher, find_latest_power_log


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


def test_log_watcher_yields_new_lines_as_they_are_appended(tmp_path: Path) -> None:
    logs_dir = tmp_path / "Logs"
    session = logs_dir / "Hearthstone_2026_09_06_18_30_00"
    session.mkdir(parents=True)
    power_log = session / "Power.log"
    power_log.write_text("line1\n")

    watcher = LogWatcher(logs_dir)
    first_batch = list(watcher.poll())
    assert first_batch == ["line1\n"]

    with power_log.open("a") as f:
        f.write("line2\n")
    second_batch = list(watcher.poll())
    assert second_batch == ["line2\n"]


def test_log_watcher_switches_to_a_new_session_folder(tmp_path: Path) -> None:
    logs_dir = tmp_path / "Logs"
    session1 = logs_dir / "Hearthstone_2026_09_06_18_30_00"
    session1.mkdir(parents=True)
    (session1 / "Power.log").write_text("game one\n")

    watcher = LogWatcher(logs_dir)
    assert list(watcher.poll()) == ["game one\n"]

    session2 = logs_dir / "Hearthstone_2026_09_06_20_00_00"
    session2.mkdir(parents=True)
    (session2 / "Power.log").write_text("game two\n")

    assert list(watcher.poll()) == ["game two\n"]
