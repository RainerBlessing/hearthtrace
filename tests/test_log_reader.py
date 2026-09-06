from pathlib import Path

import pytest

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


def test_log_watcher_withholds_a_line_not_yet_terminated_by_newline(
    tmp_path: Path,
) -> None:
    logs_dir = tmp_path / "Logs"
    session = logs_dir / "Hearthstone_2026_09_06_18_30_00"
    session.mkdir(parents=True)
    power_log = session / "Power.log"
    # Hearthstone can flush a write before the trailing newline lands.
    power_log.write_text("partial line without a terminator yet")

    watcher = LogWatcher(logs_dir)
    assert list(watcher.poll()) == []

    with power_log.open("a") as f:
        f.write(" - now complete\n")

    assert list(watcher.poll()) == [
        "partial line without a terminator yet - now complete\n"
    ]


def test_log_watcher_resets_offset_when_file_shrinks_below_it(
    tmp_path: Path,
) -> None:
    logs_dir = tmp_path / "Logs"
    session = logs_dir / "Hearthstone_2026_09_06_18_30_00"
    session.mkdir(parents=True)
    power_log = session / "Power.log"
    power_log.write_text("line1\nline2\nline3\n")

    watcher = LogWatcher(logs_dir)
    assert list(watcher.poll()) == ["line1\n", "line2\n", "line3\n"]

    # Simulate external truncation/rewrite of the same path without a
    # session-folder rotation: the file shrinks below the stored offset.
    power_log.write_text("fresh\n")

    assert list(watcher.poll()) == ["fresh\n"]


def test_log_watcher_poll_does_not_raise_when_file_vanishes_between_check_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs_dir = tmp_path / "Logs"
    session = logs_dir / "Hearthstone_2026_09_06_18_30_00"
    session.mkdir(parents=True)
    power_log = session / "Power.log"
    power_log.write_text("line1\n")

    watcher = LogWatcher(logs_dir)

    original_open = Path.open

    def flaky_open(self: Path, *args: object, **kwargs: object) -> object:
        if self == power_log:
            raise FileNotFoundError(power_log)
        return original_open(self, *args, **kwargs)  # type: ignore[arg-type]

    # latest.exists() (checked inside poll()) still returns True: the file
    # only disappears in the window between that check and the open() call,
    # e.g. concurrent session-rotation cleanup.
    monkeypatch.setattr(Path, "open", flaky_open)

    assert list(watcher.poll()) == []


def test_log_watcher_poll_yields_nothing_when_no_session_folder_exists(
    tmp_path: Path,
) -> None:
    logs_dir = tmp_path / "Logs"
    logs_dir.mkdir()

    watcher = LogWatcher(logs_dir)

    assert list(watcher.poll()) == []


def test_log_watcher_poll_yields_nothing_when_logs_dir_does_not_exist(
    tmp_path: Path,
) -> None:
    watcher = LogWatcher(tmp_path / "does-not-exist")

    assert list(watcher.poll()) == []


def test_log_watcher_replaces_invalid_utf8_bytes_instead_of_raising(
    tmp_path: Path,
) -> None:
    logs_dir = tmp_path / "Logs"
    session = logs_dir / "Hearthstone_2026_09_06_18_30_00"
    session.mkdir(parents=True)
    power_log = session / "Power.log"
    # 0xff is not valid UTF-8 anywhere; with the default OS-locale-dependent
    # encoding and strict error handling this would raise UnicodeDecodeError.
    power_log.write_bytes(b"card played: \xff broken byte\n")

    watcher = LogWatcher(logs_dir)
    lines = list(watcher.poll())

    assert len(lines) == 1
    assert lines[0].startswith("card played: ")
    assert "�" in lines[0]
