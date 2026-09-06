from pathlib import Path

import pytest
from hearthstone.entities import Card, Game, Player
from hearthstone.enums import GameTag, Zone

from hs_tracker.parser import NoGameFoundError, _deck_status, parse_log

FIXTURE = Path(__file__).parent / "fixtures" / "sample_match.power.log"


def _make_game_with_players() -> tuple[Game, Player, Player]:
    game = Game(id=1)
    game.create({})
    friendly = Player(id=2, player_id=1, hi=0, lo=1, name="Friendly")
    opponent = Player(id=3, player_id=2, hi=0, lo=1, name="Opponent")
    game.register_entity(friendly)
    game.register_entity(opponent)
    return game, friendly, opponent


def _register_card(
    game: Game, entity_id: int, card_id: str, controller: Player, zone: Zone = Zone.DECK
) -> Card:
    card = Card(id=entity_id, card_id=card_id)
    card.tag_change(GameTag.ZONE, zone)
    card.tag_change(GameTag.CONTROLLER, controller.player_id)
    game.register_entity(card)
    return card


def test_parse_log_extracts_own_class_and_deck_size() -> None:
    game = parse_log(FIXTURE)

    assert game.own_class == "SHAMAN"
    # The real fixture is a short conceded match: only cards that were
    # actually drawn/revealed by the end are known, not the full 30.
    assert len(game.starting_deck) == 15


def test_parse_log_extracts_result() -> None:
    game = parse_log(FIXTURE)

    assert game.result == "WON"


def test_parse_log_extracts_game_index() -> None:
    # The real fixture contains exactly one CREATE_GAME block, so the game
    # returned (the last -- and here, only -- one in the file) is the 1st.
    game = parse_log(FIXTURE)

    assert game.game_index == 1


def test_parse_log_extracts_first_play_event() -> None:
    game = parse_log(FIXTURE)

    assert len(game.turn_log) > 0
    first = game.turn_log[0]
    # Real Hearthstone turn numbering counts each player's turn separately
    # (turn 1 = the first player's opening turn, turn 2 = the second
    # player's opening turn, ...). Nobody played anything on turn 1 in
    # this match; the opponent's Elven Archer on their opening turn is
    # the very first PLAY block in the fixture. `player_name` is "Du"/
    # "Gegner" (never the raw Battle.net account name) so match summaries
    # can be pasted without leaking the user's real BattleTag.
    assert first.turn == 2
    assert first.player_name == "Gegner"
    assert first.card_name == "Elven Archer"


def test_parse_log_extracts_not_in_deck_card_ids() -> None:
    game = parse_log(FIXTURE)

    # Card ids from the friendly player's (Friendly#1000) starting deck
    # that are NOT in Zone.DECK by the end of the match -- a final-state
    # snapshot, not a history of every draw. Of the 15 known starting-deck
    # cards, 14 ended the match somewhere other than the deck (hand,
    # graveyard, battlefield, ...) and exactly one ("CORE_EX1_238") ended
    # back in Zone.DECK.
    assert len(game.drawn_card_ids) == 14
    assert "CORE_WC_042" in game.drawn_card_ids
    assert "CORE_EX1_238" not in game.drawn_card_ids
    # Witch's Apprentice ("CORE_GIL_531") had both of its copies leave the
    # deck (one to the graveyard, one onto the battlefield) and neither
    # ended the match back in the deck.
    assert game.drawn_card_ids.count("CORE_GIL_531") == 2


def test_deck_status_marks_a_card_not_ending_the_match_in_the_deck() -> None:
    # A card mulliganed away and then redrawn later (or simply drawn and
    # held/played) ends the match somewhere other than Zone.DECK -- it must
    # be reported as not-in-deck exactly once, not double-counted for
    # having left the deck twice.
    game, friendly, _opponent = _make_game_with_players()
    _register_card(game, entity_id=10, card_id="CS2_022", controller=friendly)
    card = game.find_entity_by_id(10)
    assert card is not None
    card.tag_change(GameTag.ZONE, Zone.HAND)  # mulliganed into opening hand
    card.tag_change(GameTag.ZONE, Zone.DECK)  # mulliganed back
    card.tag_change(GameTag.ZONE, Zone.HAND)  # drawn again later; ends here

    remaining, not_in_deck = _deck_status(friendly)

    assert remaining == []
    assert not_in_deck == ["CS2_022"]


def test_deck_status_keeps_a_mulliganed_and_never_redrawn_card_as_remaining() -> None:
    # Regression test: a card dealt into the opening hand and mulliganed
    # back, and never drawn again, ends the match back in Zone.DECK -- it
    # is still physically in the deck and must count as remaining, not be
    # permanently marked as drawn just because it once left the deck.
    game, friendly, _opponent = _make_game_with_players()
    _register_card(game, entity_id=11, card_id="CS2_023", controller=friendly)
    card = game.find_entity_by_id(11)
    assert card is not None
    card.tag_change(GameTag.ZONE, Zone.HAND)  # dealt into opening hand
    card.tag_change(GameTag.ZONE, Zone.DECK)  # mulliganed back, never redrawn

    remaining, not_in_deck = _deck_status(friendly)

    assert remaining == ["CS2_023"]
    assert not_in_deck == []


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
