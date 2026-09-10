from pathlib import Path

from hearthtrace.log_setup import (
    LogSetupCheck,
    _upsert_ini_value,
    check_log_setup,
    disable_log_size_limit,
)

_GOOD_LOG_CONFIG = "[Power]\nLogLevel=1\nFilePrinting=True\nVerbose=True\n"


def test_check_log_setup_reports_everything_ok(tmp_path: Path) -> None:
    (tmp_path / "log.config").write_text(_GOOD_LOG_CONFIG)
    (tmp_path / "client.config").write_text("[Log]\nFileSizeLimit.Int=-1\n")

    check = check_log_setup(tmp_path)

    assert check == LogSetupCheck(
        power_log_enabled=True, verbose_enabled=True, size_limit_disabled=True
    )
    assert check.all_ok is True


def test_check_log_setup_flags_a_missing_client_config(tmp_path: Path) -> None:
    # The real, observed cause of a lost match: no client.config at all
    # means Hearthstone's default 10MB size limit is in effect.
    (tmp_path / "log.config").write_text(_GOOD_LOG_CONFIG)

    check = check_log_setup(tmp_path)

    assert check.size_limit_disabled is False
    assert check.all_ok is False


def test_check_log_setup_flags_a_client_config_with_the_limit_still_set(tmp_path: Path) -> None:
    (tmp_path / "log.config").write_text(_GOOD_LOG_CONFIG)
    (tmp_path / "client.config").write_text("[Log]\nFileSizeLimit.Int=10000\n")

    check = check_log_setup(tmp_path)

    assert check.size_limit_disabled is False


def test_check_log_setup_flags_missing_power_logging(tmp_path: Path) -> None:
    # Not this module's job to fix (see the module docstring) -- but must
    # still be reported, since nothing else in the tracker will work
    # either.
    (tmp_path / "log.config").write_text("[Power]\nFilePrinting=False\nVerbose=False\n")

    check = check_log_setup(tmp_path)

    assert check.power_log_enabled is False
    assert check.verbose_enabled is False


def test_check_log_setup_handles_a_missing_hearthstone_dir_entirely(tmp_path: Path) -> None:
    check = check_log_setup(tmp_path / "does-not-exist")

    assert check == LogSetupCheck(
        power_log_enabled=False, verbose_enabled=False, size_limit_disabled=False
    )


def test_upsert_ini_value_creates_a_missing_section() -> None:
    result = _upsert_ini_value("", "Log", "FileSizeLimit.Int", "-1")

    assert result == "[Log]\nFileSizeLimit.Int=-1\n"


def test_upsert_ini_value_appends_a_missing_section_after_existing_content() -> None:
    result = _upsert_ini_value("[Other]\nSomeKey=1\n", "Log", "FileSizeLimit.Int", "-1")

    assert result == "[Other]\nSomeKey=1\n\n[Log]\nFileSizeLimit.Int=-1\n"


def test_upsert_ini_value_replaces_an_existing_key_in_place() -> None:
    result = _upsert_ini_value(
        "[Log]\nFileSizeLimit.Int=10000\nOtherKey=keep-me\n", "Log", "FileSizeLimit.Int", "-1"
    )

    assert result == "[Log]\nFileSizeLimit.Int=-1\nOtherKey=keep-me\n"


def test_upsert_ini_value_adds_a_missing_key_to_an_existing_section_without_disturbing_others() -> (
    None
):
    result = _upsert_ini_value(
        "[Log]\nOtherKey=keep-me\n\n[Unrelated]\nFileSizeLimit.Int=should-not-be-touched\n",
        "Log",
        "FileSizeLimit.Int",
        "-1",
    )

    assert result == (
        "[Log]\nOtherKey=keep-me\n\nFileSizeLimit.Int=-1\n"
        "[Unrelated]\nFileSizeLimit.Int=should-not-be-touched\n"
    )


def test_disable_log_size_limit_creates_a_fresh_client_config(tmp_path: Path) -> None:
    disable_log_size_limit(tmp_path)

    assert (tmp_path / "client.config").read_text() == "[Log]\nFileSizeLimit.Int=-1\n"
    assert not (tmp_path / "client.config.bak").exists()


def test_disable_log_size_limit_backs_up_and_preserves_other_settings(tmp_path: Path) -> None:
    original = "[Log]\nFileSizeLimit.Int=10000\n\n[Graphics]\nFullscreen=True\n"
    (tmp_path / "client.config").write_text(original)

    disable_log_size_limit(tmp_path)

    assert (tmp_path / "client.config.bak").read_text() == original
    updated = (tmp_path / "client.config").read_text()
    assert "FileSizeLimit.Int=-1" in updated
    assert "[Graphics]\nFullscreen=True" in updated


def test_disable_log_size_limit_result_passes_check_log_setup(tmp_path: Path) -> None:
    (tmp_path / "log.config").write_text(_GOOD_LOG_CONFIG)
    (tmp_path / "client.config").write_text("[Log]\nFileSizeLimit.Int=10000\n")

    disable_log_size_limit(tmp_path)

    assert check_log_setup(tmp_path).all_ok is True
