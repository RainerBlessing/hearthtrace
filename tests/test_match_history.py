from datetime import datetime
from pathlib import Path

from hs_tracker.match_history import (
    MatchHistoryEntry,
    _display_class,
    format_history_row,
    load_match_history,
)


def _write_export(export_dir: Path, filename: str, own_class: str, opponent_class: str,
                   turn_numbers: list[int]) -> Path:
    lines = [
        "# Hearthstone Match – 07.09.2026 13:32",
        "",
        "**Ergebnis:** Sieg",
        f"**Eigene Klasse:** {own_class} | **Gegner-Klasse:** {opponent_class}",
        "**Deck:** ... (30 Karten)",
        "",
    ]
    for number in turn_numbers:
        lines.append(f"## Zug {number} – Du")
    path = export_dir / filename
    path.write_text("\n".join(lines))
    return path


def test_load_match_history_parses_filename_and_content(tmp_path: Path) -> None:
    _write_export(tmp_path, "2026-09-07_13-32-33_WON.md", "SHAMAN", "DEATHKNIGHT", [1, 2, 22])

    entries = load_match_history(tmp_path)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.when == datetime(2026, 9, 7, 13, 32, 33)
    assert entry.result_label == "Sieg"
    assert entry.own_class == "Shaman"
    assert entry.opponent_class == "Death Knight"
    assert entry.turn_count == 22


def test_load_match_history_sorts_newest_first(tmp_path: Path) -> None:
    _write_export(tmp_path, "2026-09-07_12-45-26_WON.md", "SHAMAN", "SHAMAN", [1, 15])
    _write_export(tmp_path, "2026-09-07_13-32-33_WON.md", "SHAMAN", "DEATHKNIGHT", [1, 22])
    _write_export(tmp_path, "2026-09-07_13-11-23_LOST.md", "SHAMAN", "DEATHKNIGHT", [1, 16])

    entries = load_match_history(tmp_path)

    assert [e.when.strftime("%H:%M") for e in entries] == ["13:32", "13:11", "12:45"]


def test_load_match_history_skips_files_with_an_unrelated_name(tmp_path: Path) -> None:
    _write_export(tmp_path, "2026-09-07_13-32-33_WON.md", "SHAMAN", "DEATHKNIGHT", [1])
    (tmp_path / "README.md").write_text("not a match export")

    entries = load_match_history(tmp_path)

    assert len(entries) == 1


def test_load_match_history_skips_a_file_with_no_class_line(tmp_path: Path) -> None:
    # Filename matches the export naming convention, but the content
    # doesn't carry the class line this needs (e.g. a very old export from
    # before that line existed, or a hand-edited file).
    (tmp_path / "2026-09-07_13-32-33_WON.md").write_text("# Hearthstone Match\n\nno class line\n")

    entries = load_match_history(tmp_path)

    assert entries == []


def test_load_match_history_returns_empty_list_when_export_dir_is_missing(tmp_path: Path) -> None:
    entries = load_match_history(tmp_path / "does-not-exist")

    assert entries == []


def test_display_class_spaces_out_the_two_compound_class_names() -> None:
    assert _display_class("DEATHKNIGHT") == "Death Knight"
    assert _display_class("DEMONHUNTER") == "Demon Hunter"
    assert _display_class("SHAMAN") == "Shaman"


def test_format_history_row_matches_the_requested_layout() -> None:
    entry = MatchHistoryEntry(
        when=datetime(2026, 9, 7, 13, 32),
        result_label="Sieg",
        own_class="Shaman",
        opponent_class="Death Knight",
        turn_count=22,
        path=Path("irrelevant.md"),
    )

    assert format_history_row(entry) == "07.09. 13:32   Sieg   Shaman vs. Death Knight   22 Züge"
