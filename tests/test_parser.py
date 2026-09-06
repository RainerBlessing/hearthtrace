from pathlib import Path

import pytest

from hs_tracker.parser import NoGameFoundError, parse_log

FIXTURE = Path(__file__).parent / "fixtures" / "sample_match.power.log"


def test_parse_log_extracts_own_class_and_deck_size() -> None:
    game = parse_log(FIXTURE)

    assert game.own_class == "SHAMAN"
    # The real fixture is a short conceded match: only cards that were
    # actually drawn/revealed by the end are known, not the full 30.
    assert len(game.starting_deck) == 15


def test_parse_log_extracts_result() -> None:
    game = parse_log(FIXTURE)

    assert game.result == "WON"


def test_parse_log_extracts_first_play_event() -> None:
    game = parse_log(FIXTURE)

    assert len(game.turn_log) > 0
    first = game.turn_log[0]
    # Real Hearthstone turn numbering counts each player's turn separately
    # (turn 1 = the first player's opening turn, turn 2 = the second
    # player's opening turn, ...). Nobody played anything on turn 1 in
    # this match; the opponent's Elven Archer on their opening turn is
    # the very first PLAY block in the fixture.
    assert first.turn == 2
    assert first.player_name == "Gastwirt"
    assert first.card_name == "Elven Archer"


def test_parse_log_raises_no_game_found_error_when_log_has_no_create_game(
    tmp_path: Path,
) -> None:
    log_without_a_game = tmp_path / "Power.log"
    # A couple of harmless lines that never include a CREATE_GAME block --
    # e.g. a log captured before any match started, or a truncated capture.
    log_without_a_game.write_text(
        "D 12:00:00.0000000 GameState.DebugPrintPower() - "
        "TAG_CHANGE Entity=GameEntity tag=STATE value=RUNNING\n"
    )

    with pytest.raises(NoGameFoundError):
        parse_log(log_without_a_game)
