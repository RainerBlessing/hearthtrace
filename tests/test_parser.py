from pathlib import Path

import pytest
from hearthstone.cardxml import load as load_cards
from hearthstone.entities import Card, Game, Player
from hearthstone.enums import ChoiceType, GameTag, Zone
from hslog import packets as hslog_packets

from hs_tracker.parser import (
    Action,
    NoGameFoundError,
    _deck_status,
    _extract_discoveries,
    parse_log,
)

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


def test_extract_discoveries_finds_friendly_general_choice() -> None:
    # Built from real hslog/hearthstone packet objects (not fabricated log
    # text) -- same pattern as the `_deck_status` tests below -- since no
    # Discover happens to occur in the real fixture match.
    game, friendly, _opponent = _make_game_with_players()
    _register_card(game, entity_id=10, card_id="CS2_022", controller=friendly)  # Polymorph
    _register_card(game, entity_id=11, card_id="CS2_023", controller=friendly)  # Arcane Intellect

    turn_tag = hslog_packets.TagChange(ts=None, entity=game.id, tag=GameTag.TURN, value=5)
    choice = hslog_packets.Choices(
        ts=None, entity=friendly.id, id=1, tasklist=None, type=ChoiceType.GENERAL, min=1, max=1
    )
    choice.choices = [10, 11]
    chosen_entities = hslog_packets.ChosenEntities(ts=None, entity=friendly.id, id=1)
    chosen_entities.choices = [11]
    packet_tree = [turn_tag, choice, chosen_entities]

    card_db, _ = load_cards()
    picks = _extract_discoveries(packet_tree, game, card_db, friendly)

    assert picks == [
        (
            5,
            Action(
                headline="Du: Discover",
                effects=["Angeboten: Polymorph, Arcane Intellect", "Gewählt: Arcane Intellect"],
            ),
        )
    ]


def test_extract_discoveries_ignores_opponent_choice() -> None:
    game, friendly, opponent = _make_game_with_players()
    _register_card(game, entity_id=10, card_id="CS2_022", controller=opponent)

    choice = hslog_packets.Choices(
        ts=None, entity=opponent.id, id=1, tasklist=None, type=ChoiceType.GENERAL, min=1, max=1
    )
    choice.choices = [10]
    chosen_entities = hslog_packets.ChosenEntities(ts=None, entity=opponent.id, id=1)
    chosen_entities.choices = [10]
    packet_tree = [choice, chosen_entities]

    card_db, _ = load_cards()
    picks = _extract_discoveries(packet_tree, game, card_db, friendly)

    assert picks == []


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


def test_parse_log_builds_one_turn_per_global_turn_number() -> None:
    # Real Hearthstone turn numbering counts each player's turn separately
    # (turn 1 = the first player's opening turn, turn 2 = the second
    # player's opening turn, ...); `player_name` is "Du"/"Gegner" (never
    # the raw Battle.net account name) so match summaries can be pasted
    # without leaking the user's real BattleTag.
    game = parse_log(FIXTURE)

    assert [t.number for t in game.turns] == list(range(1, 16))
    assert game.turns[0].player_name == "Du"
    assert game.turns[1].player_name == "Gegner"


def test_parse_log_records_play_action_with_target_mana_and_battlecry_effect() -> None:
    # The opponent's very first play: Elven Archer, a 1-mana 1/1 whose
    # battlecry deals 1 damage to a target -- here, the friendly hero. The
    # target must be named in the headline (not just visible as a stray
    # hero-health diff line), and the played card's own "beschworen" is
    # suppressed as redundant with "gespielt".
    game = parse_log(FIXTURE)
    turn2 = next(t for t in game.turns if t.number == 2)

    play = next(a for a in turn2.actions if "Elven Archer #1 gespielt" in a.headline)

    assert play.headline == "Gegner: Elven Archer #1 gespielt → Ziel: Dein Held (Mana: 1 → 0)"
    assert play.effects == ["Dein Held: 30 → 29"]


def test_parse_log_records_attack_action_against_hero_on_one_line() -> None:
    # Turn 4: the opponent's Elven Archer attacks the friendly hero. A
    # hero-target attack is folded into a single line (no separate result
    # bullet), per the format the user asked for.
    game = parse_log(FIXTURE)
    turn4 = next(t for t in game.turns if t.number == 4)

    attack = next(
        a for a in turn4.actions if "Elven Archer" in a.headline and "Angriff" in a.headline
    )

    assert attack.headline == "Gegner: Elven Archer #1 (1 Angriff) → Dein Held: 29 → 28"
    assert attack.effects == []


def test_parse_log_infers_draw_action_from_hand_delta() -> None:
    # Nothing else happens between the end of one turn and the ready state
    # of the next besides that turn's own draw -- the friendly player's
    # draw is shown by name (always known); the opponent's only as a
    # generic draw notice (their identity is hidden information).
    game = parse_log(FIXTURE)
    turn2 = next(t for t in game.turns if t.number == 2)
    turn3 = next(t for t in game.turns if t.number == 3)

    assert turn2.actions[0].headline == "Gegner: zieht eine Karte"
    assert turn3.actions[0].headline == "Du: gezogen — Wailing Vapor"


def test_parse_log_numbers_identical_minions_to_tell_them_apart() -> None:
    # Turn 7's Ritual of Power buffs several existing "Soldier of Al'Akir"
    # tokens and summons a new one, ending the turn with two of them alive
    # at different stats -- they must be distinguishable, not collapsed
    # into two identical-looking board entries.
    game = parse_log(FIXTURE)
    turn7 = next(t for t in game.turns if t.number == 7)

    names = [m.name for m in turn7.end.board.own]

    assert "Soldier of Al'Akir #4" in names
    assert "Soldier of Al'Akir #5" in names


def test_parse_log_captures_effect_triggered_draw_mid_action() -> None:
    # Turn 7: attacking the opponent's Acolyte of Pain ("whenever this
    # minion takes damage, draw a card") makes the opponent draw as a side
    # effect of the attack, not the turn's normal draw step -- it must show
    # up as an effect line on that attack, not silently vanish.
    game = parse_log(FIXTURE)
    turn7 = next(t for t in game.turns if t.number == 7)

    attack = next(a for a in turn7.actions if "Acolyte of Pain" in a.headline)

    assert "Gegner zieht eine Karte" in attack.effects


def test_parse_log_board_snapshot_includes_taunt_keyword() -> None:
    # By turn 7's start, the friendly player's Skywall Sentinel (kept in
    # the mulligan) is a 1/1 Taunt minion on the board, tagged "#1" -- a
    # stable per-name instance number so identical copies can be told
    # apart later in the match.
    game = parse_log(FIXTURE)
    turn7 = next(t for t in game.turns if t.number == 7)

    sentinel = next(m for m in turn7.start.board.own if m.name == "Skywall Sentinel #1")

    assert sentinel.attack == 1
    assert sentinel.health == 1
    assert "Spott" in sentinel.keywords


def test_parse_log_extracts_mulligan_choice() -> None:
    # Real mulligan from the fixture (Friendly#1000, entity id 2): offered
    # Skywall Sentinel/Lightning Bolt/Envoy of the End, sent back Skywall
    # Sentinel (SendChoices m_chosenEntities=[15]), kept the other two.
    game = parse_log(FIXTURE)

    assert game.mulligan is not None
    assert game.mulligan.kept == ["CORE_EX1_238", "CATA_722"]
    assert game.mulligan.returned == ["CATA_565"]


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
