import json
from datetime import datetime
from pathlib import Path

from hearthtrace.markdown_export import export_match_summary
from hearthtrace.match_history import (
    MatchHistoryEntry,
    _display_class,
    format_history_row,
    load_match_history,
    record_replay_source,
)
from hearthtrace.parser import (
    BoardState,
    HandState,
    LifeState,
    ManaState,
    ParsedGame,
    Turn,
    TurnSnapshot,
)


def _minimal_snapshot() -> TurnSnapshot:
    return TurnSnapshot(
        mana=ManaState(available=0, maximum=0, locked=0, overload_pending=0),
        life=LifeState(own_health=30, own_armor=0, opponent_health=30, opponent_armor=0),
        hand=HandState(own_cards=[], opponent_count=0),
        board=BoardState(own=[], opponent=[]),
    )


def _minimal_turn(number: int) -> Turn:
    return Turn(
        number=number,
        player_name="Du",
        opening_draws=[],
        start=_minimal_snapshot(),
        actions=[],
        end=_minimal_snapshot(),
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


def test_load_match_history_parses_real_export_match_summary_output(tmp_path: Path) -> None:
    # Round-trips through the actual writer (export_match_summary), not a
    # hand-written approximation of its format -- catches drift if
    # markdown_export.py's wording/heading format ever changes without the
    # regexes in this module being updated to match (the other tests here
    # all build synthetic export text by hand, which wouldn't notice that).
    game = ParsedGame(
        own_class="SHAMAN",
        opponent_class="DEATHKNIGHT",
        starting_deck=[],
        result="WON",
        drawn_card_ids=[],
        game_index=1,
        turns=[_minimal_turn(n) for n in range(1, 23)],
    )
    export_match_summary(game, export_dir=tmp_path, when=datetime(2026, 9, 7, 13, 32, 33))

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


def test_load_match_history_fills_in_the_replay_link_when_recorded(tmp_path: Path) -> None:
    export_dir = tmp_path
    state_dir = tmp_path / "state"
    export_path = _write_export(
        export_dir, "2026-09-07_13-32-33_WON.md", "SHAMAN", "DEATHKNIGHT", [1, 22]
    )
    log_path = tmp_path / "Power.log"
    record_replay_source(state_dir, export_path, log_path, game_index=3)

    entries = load_match_history(export_dir, state_dir=state_dir)

    assert len(entries) == 1
    assert entries[0].log_path == log_path
    assert entries[0].game_index == 3


def test_load_match_history_leaves_replay_link_unset_without_an_index_entry(
    tmp_path: Path,
) -> None:
    export_dir = tmp_path
    state_dir = tmp_path / "state"
    _write_export(export_dir, "2026-09-07_13-32-33_WON.md", "SHAMAN", "DEATHKNIGHT", [1, 22])

    entries = load_match_history(export_dir, state_dir=state_dir)

    assert entries[0].log_path is None
    assert entries[0].game_index is None


def test_load_match_history_ignores_a_missing_state_dir(tmp_path: Path) -> None:
    export_dir = tmp_path
    _write_export(export_dir, "2026-09-07_13-32-33_WON.md", "SHAMAN", "DEATHKNIGHT", [1, 22])

    entries = load_match_history(export_dir, state_dir=tmp_path / "does-not-exist")

    assert entries[0].log_path is None


def test_record_replay_source_preserves_other_entries_already_in_the_index(
    tmp_path: Path,
) -> None:
    export_dir = tmp_path
    state_dir = tmp_path / "state"
    first = _write_export(export_dir, "2026-09-07_12-45-26_WON.md", "SHAMAN", "SHAMAN", [1])
    second = _write_export(
        export_dir, "2026-09-07_13-32-33_WON.md", "SHAMAN", "DEATHKNIGHT", [1]
    )
    log_path = tmp_path / "Power.log"
    record_replay_source(state_dir, first, log_path, game_index=1)
    record_replay_source(state_dir, second, log_path, game_index=2)

    entries = {e.path.name: e for e in load_match_history(export_dir, state_dir=state_dir)}

    assert entries[first.name].game_index == 1
    assert entries[second.name].game_index == 2


def test_record_replay_source_leaves_no_leftover_temp_file(tmp_path: Path) -> None:
    export_dir = tmp_path
    state_dir = tmp_path / "state"
    export_path = _write_export(export_dir, "2026-09-07_13-32-33_WON.md", "SHAMAN", "SHAMAN", [1])

    record_replay_source(state_dir, export_path, tmp_path / "Power.log", game_index=1)

    assert sorted(p.name for p in state_dir.iterdir()) == ["replay_index.json"]


def test_load_match_history_survives_a_malformed_index_entry(tmp_path: Path) -> None:
    # Code-review-caught bug: a partial write, a hand edit, or a future
    # schema change could leave one entry in replay_index.json missing a
    # key -- that must cost only that one row its replay link, not raise
    # and (via `_refresh_history_list`'s broad except) take the entire
    # Verlauf list down with it.
    export_dir = tmp_path
    state_dir = tmp_path / "state"
    good = _write_export(export_dir, "2026-09-07_12-45-26_WON.md", "SHAMAN", "SHAMAN", [1])
    bad = _write_export(export_dir, "2026-09-07_13-32-33_WON.md", "SHAMAN", "SHAMAN", [1])
    log_path = tmp_path / "Power.log"
    record_replay_source(state_dir, good, log_path, game_index=1)
    state_dir.mkdir(parents=True, exist_ok=True)
    index_path = state_dir / "replay_index.json"
    index = json.loads(index_path.read_text())
    index[bad.name] = {"log_path": str(log_path)}  # missing game_index
    index_path.write_text(json.dumps(index))

    entries = {e.path.name: e for e in load_match_history(export_dir, state_dir=state_dir)}

    assert len(entries) == 2
    assert entries[good.name].game_index == 1
    assert entries[bad.name].game_index is None
    assert entries[bad.name].log_path is None


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
