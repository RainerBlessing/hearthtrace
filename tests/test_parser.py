from pathlib import Path

import pytest
from hearthstone.entities import Card, Game, Player
from hearthstone.enums import GameTag, Zone
from hslog import packets as hslog_packets

from hs_tracker.parser import NoGameFoundError, _TurnLogWalker, parse_log

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


def _zone_change(entity_id: int, zone: Zone) -> hslog_packets.TagChange:
    return hslog_packets.TagChange(ts=None, entity=entity_id, tag=GameTag.ZONE, value=zone)


def _controller_change(entity_id: int, controller: Player) -> hslog_packets.TagChange:
    return hslog_packets.TagChange(
        ts=None, entity=entity_id, tag=GameTag.CONTROLLER, value=controller.player_id
    )


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


def test_parse_log_extracts_drawn_card_ids() -> None:
    game = parse_log(FIXTURE)

    # Cards that left Zone.DECK for the friendly player (Friendly#1000)
    # during the match, in draw order, duplicates allowed.
    assert len(game.drawn_card_ids) == 9
    assert game.drawn_card_ids[0] == "CORE_WC_042"
    # Witch's Apprentice ("CORE_GIL_531") was drawn twice in this match.
    assert game.drawn_card_ids.count("CORE_GIL_531") == 2


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


def test_turn_log_walker_does_not_double_count_a_mulliganed_and_redrawn_card() -> None:
    # A card dealt into the opening hand (DECK->HAND, draw #1), mulliganed
    # back (HAND->DECK, not a draw), then drawn again naturally
    # (DECK->HAND) is only ONE physical card leaving the deck -- it must
    # only be recorded once, not twice.
    game, friendly, _opponent = _make_game_with_players()
    _register_card(game, entity_id=10, card_id="CS2_022", controller=friendly)

    walker = _TurnLogWalker(game, card_db=None, friendly_player=friendly)
    walker.walk(
        [
            _zone_change(10, Zone.HAND),
            _zone_change(10, Zone.DECK),
            _zone_change(10, Zone.HAND),
        ]
    )

    assert walker.drawn_card_ids == ["CS2_022"]


def test_turn_log_walker_attributes_draws_using_controller_at_time_of_event() -> None:
    # The entity's *final* controller (friendly) differs from who
    # controlled it when it actually left the deck (opponent). This
    # mirrors Death Knight "Plague" cards, which shuffle a copy into the
    # opponent's deck mid-match. The draw happened under the opponent's
    # control and must not be attributed to the friendly player just
    # because `entity.controller` reflects the end-of-match state.
    game, friendly, opponent = _make_game_with_players()
    card = _register_card(game, entity_id=30, card_id="CORE_DK_100", controller=opponent)
    # Simulate EntityTreeExporter.export() having already applied every
    # packet -- including a later CONTROLLER change -- by the time the
    # walker runs, so `entity.tags` (and thus `entity.controller`) only
    # ever reflects this final state.
    card.tag_change(GameTag.CONTROLLER, friendly.player_id)

    walker = _TurnLogWalker(game, card_db=None, friendly_player=friendly)
    walker.walk(
        [
            _zone_change(30, Zone.HAND),  # drawn while still opponent-controlled
            _controller_change(30, friendly),  # control changes only afterwards
        ]
    )

    assert walker.drawn_card_ids == []


def test_turn_log_walker_attributes_earlier_draw_to_the_controller_at_that_time() -> None:
    # Mirror image of the above: the entity's *final* controller is the
    # opponent (e.g. it was later given away or shuffled elsewhere), but
    # it was drawn by the friendly player earlier, while still under their
    # control. That earlier draw must still be counted for the friendly
    # player, not silently dropped because of what happens to it later.
    game, friendly, opponent = _make_game_with_players()
    card = _register_card(game, entity_id=40, card_id="CORE_DK_101", controller=friendly)
    card.tag_change(GameTag.CONTROLLER, opponent.player_id)

    walker = _TurnLogWalker(game, card_db=None, friendly_player=friendly)
    walker.walk(
        [
            _zone_change(40, Zone.HAND),  # drawn while still friendly-controlled
            _controller_change(40, opponent),  # control changes only afterwards
        ]
    )

    assert walker.drawn_card_ids == ["CORE_DK_101"]
