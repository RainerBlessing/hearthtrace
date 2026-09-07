from pathlib import Path
from types import SimpleNamespace

import pytest
from hearthstone.cardxml import load as load_cards
from hearthstone.entities import Card, Game, Player
from hearthstone.enums import CardType, ChoiceType, GameTag, Zone
from hslog import packets as hslog_packets

from hs_tracker.parser import (
    _UNKNOWN_CARD_TOKEN,
    Action,
    BoardState,
    HandState,
    LifeState,
    ManaState,
    NoGameFoundError,
    Turn,
    TurnSnapshot,
    _deck_status,
    _diff_effects,
    _extract_discoveries,
    _extract_mulligan,
    _InstanceNamer,
    _mana_state,
    _resolve_unknown_card_names,
    _snapshot_entities,
    _target_suffix,
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


def test_mana_state_never_reports_negative_available_mana() -> None:
    # Observed in a real match: RESOURCES_USED transiently exceeding
    # available crystals for a cost-modified card (e.g. Ultraxion) made the
    # raw tag arithmetic go negative. The export must never show that,
    # regardless of why the underlying tags are momentarily inconsistent.
    _game, friendly, _opponent = _make_game_with_players()
    friendly.tag_change(GameTag.RESOURCES, 5)
    friendly.tag_change(GameTag.RESOURCES_USED, 9)

    mana = _mana_state(friendly)

    assert mana.available == 0
    assert mana.maximum == 5


def _make_snapshot() -> TurnSnapshot:
    return TurnSnapshot(
        mana=ManaState(available=0, maximum=0, locked=0, overload_pending=0),
        life=LifeState(own_health=30, own_armor=0, opponent_health=30, opponent_armor=0),
        hand=HandState(own_cards=[], opponent_count=0),
        board=BoardState(own=[], opponent=[]),
    )


def test_resolve_unknown_card_names_fills_in_a_later_reveal() -> None:
    # A card generated hidden into the opponent's hand (e.g. Selective
    # Breeder) is correctly unknown when first shown -- but once the
    # opponent actually plays it, its identity becomes public, and the
    # earlier "Unbekannte Karte gespielt" text must be resolved to the
    # real name rather than staying permanently unresolved.
    game, _friendly, opponent = _make_game_with_players()
    revealed = _register_card(game, entity_id=99, card_id="CS2_022", controller=opponent)

    placeholder = _UNKNOWN_CARD_TOKEN.format(99)
    turn = Turn(
        number=1,
        player_name="Gegner",
        opening_draws=[f"{placeholder} gezogen"],
        start=_make_snapshot(),
        actions=[
            Action(
                headline=f"Gegner: {placeholder} gespielt", effects=[f"{placeholder} beschworen"]
            )
        ],
        end=_make_snapshot(),
    )

    card_db, _ = load_cards()
    _resolve_unknown_card_names([turn], game, card_db)

    assert turn.opening_draws == ["Polymorph gezogen"]
    assert turn.actions[0].headline == "Gegner: Polymorph gespielt"
    assert turn.actions[0].effects == ["Polymorph beschworen"]
    assert revealed.card_id == "CS2_022"  # sanity: the fixture entity itself is untouched


def test_resolve_unknown_card_names_falls_back_when_never_revealed() -> None:
    game, _friendly, _opponent = _make_game_with_players()
    placeholder = _UNKNOWN_CARD_TOKEN.format(12345)
    turn = Turn(
        number=1,
        player_name="Gegner",
        opening_draws=[],
        start=_make_snapshot(),
        actions=[Action(headline=f"Gegner: {placeholder} gespielt")],
        end=_make_snapshot(),
    )

    card_db, _ = load_cards()
    _resolve_unknown_card_names([turn], game, card_db)

    assert turn.actions[0].headline == "Gegner: Unbekannte Karte gespielt"


def test_diff_effects_names_a_transform_by_pre_and_post_identity() -> None:
    # Hex/Polymorph-style effects keep the same entity id but swap its
    # card id -- the generic stat-diff would otherwise report this as an
    # ordinary "Void Terror: 5/3 -> 1/1" line, losing which minion was
    # actually targeted (it now displays as "Frog").
    game, _friendly, opponent = _make_game_with_players()
    target = _register_card(
        game, entity_id=50, card_id="hexfrog", controller=opponent, zone=Zone.PLAY
    )
    target.tag_change(GameTag.CARDTYPE, CardType.MINION)
    target.tag_change(GameTag.ATK, 1)
    target.tag_change(GameTag.HEALTH, 1)
    before = {50: (Zone.PLAY, 5, 3, 0, "CORE_EX1_304")}
    after = _snapshot_entities(game)

    card_db, _ = load_cards()
    namer = _InstanceNamer(card_db)
    lines = _diff_effects(game, namer, _friendly, before, after)

    assert lines == ["Void Terror #1 transformiert zu Frog #1 (1/1, kann angreifen)"]


def test_target_suffix_names_target_by_pre_block_identity() -> None:
    # The play/power headline's target must be named by what was actually
    # selected (Void Terror), not what it becomes by the time the headline
    # is built (Frog) -- otherwise "Hex -> Ziel: Frog" is nonsensical.
    game, _friendly, opponent = _make_game_with_players()
    target = _register_card(
        game, entity_id=50, card_id="hexfrog", controller=opponent, zone=Zone.PLAY
    )
    target.tag_change(GameTag.CARDTYPE, CardType.MINION)
    target.tag_change(GameTag.ATK, 1)
    target.tag_change(GameTag.HEALTH, 1)
    before: dict[int, tuple] = {50: (Zone.PLAY, 5, 3, 0, "CORE_EX1_304")}

    card_db, _ = load_cards()
    namer = _InstanceNamer(card_db)
    block = SimpleNamespace(target=50)
    suffix = _target_suffix(block, game, namer, _friendly, before)

    assert suffix == " → Ziel: Void Terror #1"


def test_extract_mulligan_excludes_the_coin() -> None:
    # A going-second player's mulligan `Choices.choices` includes The Coin
    # (it's already sitting in their opening hand) -- confirmed structurally
    # in the real fixture for the opponent (who goes second there). The
    # Coin was never a real mulligan option and must never appear in either
    # `kept` or `returned`, regardless of which raw list it ends up in.
    game, friendly, _opponent = _make_game_with_players()
    _register_card(game, entity_id=10, card_id="CS2_022", controller=friendly)  # Polymorph
    _register_card(game, entity_id=11, card_id="GAME_005", controller=friendly)  # The Coin

    choice = hslog_packets.Choices(
        ts=None, entity=friendly.id, id=1, tasklist=None, type=ChoiceType.MULLIGAN, min=0, max=1
    )
    choice.choices = [10, 11]
    chosen_entities = hslog_packets.ChosenEntities(ts=None, entity=friendly.id, id=1)
    chosen_entities.choices = [10, 11]  # the Coin tags along in "chosen" too
    packet_tree = [choice, chosen_entities]

    mulligan = _extract_mulligan(packet_tree, game, friendly)

    assert mulligan is not None
    assert mulligan.kept == ["CS2_022"]
    assert mulligan.returned == []


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


def test_parse_log_attributes_generated_cards_to_their_source() -> None:
    # Turn 7: Ritual of Power's effect adds two Breezling cards directly to
    # hand (never drawn from the deck) -- they must be attributed to their
    # source, not silently appear in the next hand snapshot as if from
    # nowhere. Turn 11: Witch's Apprentice's battlecry does the same for
    # Molten Blast.
    game = parse_log(FIXTURE)
    turn7 = next(t for t in game.turns if t.number == 7)
    turn11 = next(t for t in game.turns if t.number == 11)

    ritual = next(a for a in turn7.actions if "Ritual of Power gespielt" in a.headline)
    apprentice = next(a for a in turn11.actions if "Witch's Apprentice #1 gespielt" in a.headline)

    assert "Ritual of Power → erzeugt Breezling #1" in ritual.effects
    assert "Ritual of Power → erzeugt Breezling #2" in ritual.effects
    assert "Witch's Apprentice #1 → erzeugt Molten Blast" in apprentice.effects


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


def test_parse_log_infers_opening_draw_from_hand_delta_not_as_an_action() -> None:
    # Nothing else happens between the end of one turn and the ready state
    # of the next besides that turn's own draw -- the friendly player's
    # draw is shown by name (always known); the opponent's only as a
    # generic draw notice (their identity is hidden information). It's
    # already reflected in `start.hand`, so it must not also appear as a
    # numbered action (that would misrepresent already-known context as a
    # decision made during the turn).
    game = parse_log(FIXTURE)
    turn2 = next(t for t in game.turns if t.number == 2)
    turn3 = next(t for t in game.turns if t.number == 3)

    assert turn2.opening_draws == ["Gegner zieht eine Karte"]
    assert turn3.opening_draws == ["Wailing Vapor gezogen"]
    assert all("gezogen" not in a.headline and "zieht" not in a.headline for a in turn2.actions)
    assert all("gezogen" not in a.headline and "zieht" not in a.headline for a in turn3.actions)


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


def test_parse_log_filters_out_stat_changes_to_entities_never_on_board() -> None:
    # Turn 7: Ritual of Power's implementation touches several internal
    # "Soldier of Al'Akir" candidate entities that never actually reach
    # Zone.PLAY (only #4 and the newly-summoned #5 really end up on the
    # board, per the board snapshot) -- their stat churn is invisible to
    # both players and must not appear as if it happened on the board.
    game = parse_log(FIXTURE)
    turn7 = next(t for t in game.turns if t.number == 7)

    ritual = next(a for a in turn7.actions if "Ritual of Power gespielt" in a.headline)
    board_names = {m.name for m in turn7.end.board.own}

    assert board_names == {
        "Wailing Vapor #1",
        "Skywall Sentinel #1",
        "Soldier of Al'Akir #4",
        "Soldier of Al'Akir #5",
    }
    assert not any("#1:" in e or "#2:" in e or "#3:" in e for e in ritual.effects)
    assert "Soldier of Al'Akir #4: 1/2 → 2/2" in ritual.effects
    assert "Soldier of Al'Akir #5 beschworen" in ritual.effects


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
    # Skywall Sentinel/Lightning Bolt/Envoy of the End; SendChoices
    # m_chosenEntities=[15] (Skywall Sentinel) is the entity *kept* -- proven
    # by independently tracing each entity's fate: id 15 (Skywall Sentinel)
    # reaches Zone.GRAVEYARD (played from the opening hand, matching turn
    # 1's actual starting hand), while id 20 (Lightning Bolt, *not* chosen)
    # stays in Zone.DECK all game (never drawn again -- consistent with
    # having been sent back). "Chosen" therefore means "kept", not "sent
    # back to the deck", despite the mulligan UI's card-clicking gesture
    # suggesting the opposite.
    game = parse_log(FIXTURE)

    assert game.mulligan is not None
    assert game.mulligan.kept == ["CATA_565"]
    assert game.mulligan.returned == ["CORE_EX1_238", "CATA_722"]
    assert "Skywall Sentinel" in game.turns[0].start.hand.own_cards


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
