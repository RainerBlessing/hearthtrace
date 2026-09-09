from pathlib import Path
from types import SimpleNamespace

import pytest
from hearthstone.cardxml import load as load_cards
from hearthstone.entities import Card, Game, Player
from hearthstone.enums import CardType, ChoiceType, GameTag, Zone
from hslog import packets as hslog_packets

from hs_tracker.parser import (
    Action,
    BoardState,
    HandState,
    LifeState,
    ManaState,
    NoGameFoundError,
    Turn,
    TurnSnapshot,
    _build_attack_action,
    _class_name,
    _deck_status,
    _diff_effects,
    _extract_discoveries,
    _extract_mulligan,
    _InstanceNamer,
    _is_deathrattle_trigger,
    _is_merge_only_block,
    _mana_headline_suffix,
    _mana_state,
    _snapshot_entities,
    _target_suffix,
    _TurnBuilder,
    _weapon_of,
    _zone_transition_line,
    parse_log,
    parse_log_at_index,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_match.power.log"
# A second real match, captured specifically because the first fixture has
# no weapon in it at all -- the opponent (Rogue) repeatedly equips and uses
# a Wicked Knife via Dagger Mastery.
WEAPON_FIXTURE = Path(__file__).parent / "fixtures" / "weapon_match.power.log"
# A third real match, captured because the opponent's lethal-dealing hero
# had a Prince Renathal +10 Health aura attached: once that hero died, the
# engine cleaned up the aura and rewrote the hero's own (by then irrelevant)
# HEALTH tag from 40 back down to base 30 -- a live re-read at export time
# doesn't know that DAMAGE=41 was reached while HEALTH was still 40, and
# would report opponent_health=-11 instead of the true -1.
LETHAL_HEALTH_REWRITE_FIXTURE = (
    Path(__file__).parent / "fixtures" / "lethal_health_rewrite_match.power.log"
)
# A real two-match session log (game 2 above, immediately followed by the
# start of a real game 3): the same account was assigned player_id=2 in
# game 2 and player_id=1 in game 3 -- ordinary (who goes first is decided
# fresh every match), but reading both games through one continuous
# `LogParser`/`PlayerManager` (as `parse_log` used to) makes hslog reject
# the second assignment as inconsistent with the first and crash. Verified
# this fixture reproduces that crash byte-for-byte against a bare
# `hslog.LogParser` before the fix in `_split_last_game`.
MULTI_GAME_FIXTURE = (
    Path(__file__).parent / "fixtures" / "multi_game_player_id_conflict.power.log"
)
# A real match where the opponent's Factory Assemblybot (Miniaturize: "At
# the end of your turn, summon a 6/7 Bot that attacks a random enemy")
# fires its end-of-turn trigger against the friendly hero. User-reported:
# the resulting 6 damage and the summoned Copybot were both completely
# absent from turn 27's action log, even though the turn's closing
# snapshot correctly reflected them (23 -> 15 life, not 23 -> 21).
END_OF_TURN_TRIGGER_FIXTURE = (
    Path(__file__).parent / "fixtures" / "end_of_turn_trigger_match.power.log"
)


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


def test_mana_headline_suffix_says_cost_zero_for_a_net_zero_change() -> None:
    # Verified against a real match (live-hook traced): a discounted
    # Bloodmage Thalnos, printed cost 2, was actually charged 0 -- mana
    # available before and after the block were identical (0 == 0). Net 0
    # mana spent is a real, decision-relevant fact and should say so
    # explicitly, not look like mana simply wasn't tracked for this action.
    assert _mana_headline_suffix(0, 0) == " (Kosten: 0)"
    assert _mana_headline_suffix(4, 4) == " (Kosten: 0)"
    assert _mana_headline_suffix(4, 2) == " (Mana: 4 → 2)"
    assert _mana_headline_suffix(None, 2) == ""
    assert _mana_headline_suffix(4, None) == ""


def test_is_merge_only_block_identifies_deaths_and_deathrattle_trigger() -> None:
    # A death and its deathrattle resolve as their own separate top-level
    # blocks (verified against a real match's packet tree) -- both must be
    # recognized as "fold into the causing action", not just DEATHS alone.
    from hearthstone.enums import BlockType

    deaths = SimpleNamespace(type=BlockType.DEATHS, trigger_keyword=None)
    deathrattle_trigger = SimpleNamespace(
        type=BlockType.TRIGGER, trigger_keyword=GameTag.DEATHRATTLE
    )
    unrelated_trigger = SimpleNamespace(type=BlockType.TRIGGER, trigger_keyword=None)
    play = SimpleNamespace(type=BlockType.PLAY, trigger_keyword=None)

    assert _is_merge_only_block(deaths) is True
    assert _is_deathrattle_trigger(deathrattle_trigger) is True
    assert _is_merge_only_block(deathrattle_trigger) is True
    # An ordinary trigger (e.g. "start of turn") must NOT be swept into
    # whatever action happened to run right before it.
    assert _is_merge_only_block(unrelated_trigger) is False
    assert _is_merge_only_block(play) is False


def _make_turn_snapshot() -> TurnSnapshot:
    return TurnSnapshot(
        mana=ManaState(available=0, maximum=0, locked=0, overload_pending=0),
        life=LifeState(own_health=30, own_armor=0, opponent_health=30, opponent_armor=0),
        hand=HandState(own_cards=[], opponent_count=0),
        board=BoardState(own=[], opponent=[]),
    )


def test_turn_builder_merges_deathrattle_effects_into_causing_action() -> None:
    # A minion's death (BlockType.DEATHS bookkeeping) and its deathrattle's
    # actual effect (a separate BlockType.TRIGGER block) must fold into
    # the attack/play that caused them -- otherwise a deathrattle-summoned
    # minion appears on the board with no explanation at all.
    game, friendly, opponent = _make_game_with_players()
    attacker = _register_card(
        game, entity_id=1, card_id="CORE_CS2_189", controller=friendly, zone=Zone.PLAY
    )
    attacker.tag_change(GameTag.CARDTYPE, CardType.MINION)
    attacker.tag_change(GameTag.ATK, 1)
    attacker.tag_change(GameTag.HEALTH, 1)
    summoned = _register_card(
        game, entity_id=2, card_id="CORE_EX1_304", controller=friendly, zone=Zone.PLAY
    )
    summoned.tag_change(GameTag.CARDTYPE, CardType.MINION)
    summoned.tag_change(GameTag.ATK, 3)
    summoned.tag_change(GameTag.HEALTH, 2)

    card_db, _ = load_cards()
    builder = _TurnBuilder(friendly.player_id, card_db)
    builder._current = Turn(  # noqa: SLF001 - exercising internal merge logic directly
        number=1,
        player_name="Du",
        opening_draws=[],
        start=_make_turn_snapshot(),
        actions=[Action(headline="Du: Elven Archer #1 → Void Terror #1")],
        end=_make_turn_snapshot(),
    )

    before: dict = {1: (Zone.PLAY, 1, 1, 0, "CORE_CS2_189")}
    after = _snapshot_entities(game)

    builder._merge_effects_into_last_action(game, friendly, before, after)  # noqa: SLF001

    assert builder._current.actions[-1].effects == ["Void Terror #1 beschworen"]  # noqa: SLF001


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


def test_build_attack_action_narrates_a_secret_interrupted_attack() -> None:
    # Freezing Trap-style interruption: the attacker gets bounced back to
    # hand before dealing combat damage. Detected generically -- the
    # attacker ends the block in Zone.HAND, and something left Zone.SECRET
    # during it -- with no Freezing-Trap-specific logic anywhere.
    game, friendly, opponent = _make_game_with_players()
    attacker = _register_card(
        game, entity_id=1, card_id="CORE_CS2_189", controller=friendly, zone=Zone.HAND
    )
    attacker.tag_change(GameTag.CARDTYPE, CardType.MINION)
    attacker.tag_change(GameTag.ATK, 3)
    attacker.tag_change(GameTag.HEALTH, 4)
    defender = _register_card(
        game, entity_id=2, card_id="CORE_EX1_304", controller=opponent, zone=Zone.PLAY
    )
    defender.tag_change(GameTag.CARDTYPE, CardType.MINION)
    defender.tag_change(GameTag.ATK, 5)
    defender.tag_change(GameTag.HEALTH, 3)
    secret = _register_card(
        game, entity_id=3, card_id="EX1_611", controller=opponent, zone=Zone.GRAVEYARD
    )
    secret.tag_change(GameTag.CARDTYPE, CardType.SPELL)

    before = {
        1: (Zone.PLAY, 3, 4, 0, "CORE_CS2_189"),
        2: (Zone.PLAY, 5, 3, 0, "CORE_EX1_304"),
        3: (Zone.SECRET, 0, 0, 0, ""),
    }
    after = _snapshot_entities(game)

    card_db, _ = load_cards()
    namer = _InstanceNamer(card_db)
    block = SimpleNamespace(entity=1, target=2)
    action = _build_attack_action(block, game, namer, friendly, before, after)

    assert action.headline == "Du: Elven Archer #1 → Void Terror #1"
    assert "Secret ausgelöst: Freezing Trap" in action.effects
    assert "Elven Archer #1: Board → Hand" in action.effects
    assert action.effects[-2:] == [
        "Angriff abgebrochen",
        "Void Terror #1 nimmt keinen Kampfschaden",
    ]


def test_generated_into_hand_line_does_not_leak_future_identity() -> None:
    # A card generated hidden into a hand must show as "Unbekannte Karte"
    # at that point -- its identity must never be resolved using knowledge
    # from later in the match (e.g. once it's actually played and becomes
    # public). A later, *separate* resolution of the same entity is fine
    # and expected to show the real name by then; the earlier line itself
    # must never be rewritten.
    game, friendly, opponent = _make_game_with_players()
    source = _register_card(
        game, entity_id=1, card_id="CORE_CS2_189", controller=opponent, zone=Zone.PLAY
    )
    source.tag_change(GameTag.CARDTYPE, CardType.MINION)
    generated = _register_card(game, entity_id=2, card_id="", controller=opponent, zone=Zone.HAND)
    generated.initial_creator = 1

    before: dict = {1: (Zone.PLAY, 0, 0, 0, "CORE_CS2_189")}  # source already on board
    after = _snapshot_entities(game)

    card_db, _ = load_cards()
    namer = _InstanceNamer(card_db)
    lines = _diff_effects(game, namer, friendly, before, after)

    assert lines == ["Elven Archer #1 → erzeugt Unbekannte Karte"]

    # Later in the match, the same entity gets revealed -- a *fresh*
    # resolution at that later point correctly shows the real name; the
    # already-built line above is never retroactively rewritten.
    generated.card_id = "CORE_EX1_304"
    generated.tag_change(GameTag.CARDTYPE, CardType.MINION)

    assert namer.display_name(generated, friendly) == "Void Terror #1"
    assert lines == ["Elven Archer #1 → erzeugt Unbekannte Karte"]


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


def test_class_name_uses_the_starting_hero_not_a_mid_match_transform() -> None:
    # Some effects (Lord Jaraxxus, Majordomo Executus) replace a player's
    # hero mid-match -- `own_class`/`opponent_class` describe the class
    # picked at deck-select for the *whole* export, so they must stay that,
    # not flip to whatever the hero became later in the match.
    game, friendly, _opponent = _make_game_with_players()
    original_hero = _register_card(
        game, entity_id=10, card_id="HERO_01", controller=friendly, zone=Zone.PLAY
    )
    original_hero.tag_change(GameTag.CARDTYPE, CardType.HERO)
    friendly.initial_hero_entity_id = original_hero.id
    friendly.tags[GameTag.HERO_ENTITY] = original_hero.id

    transformed_hero = _register_card(
        game, entity_id=11, card_id="EX1_323h", controller=friendly, zone=Zone.PLAY
    )
    transformed_hero.tag_change(GameTag.CARDTYPE, CardType.HERO)
    friendly.tags[GameTag.HERO_ENTITY] = transformed_hero.id  # mid-match swap

    card_db, _ = load_cards()

    assert _class_name(friendly, card_db) == "WARRIOR"  # HERO_01 = Garrosh Hellscream


def test_weapon_of_returns_none_when_no_weapon_equipped() -> None:
    game, friendly, _opponent = _make_game_with_players()
    card_db, _ = load_cards()

    assert _weapon_of(friendly, card_db) is None


def test_weapon_of_reads_attack_and_durability() -> None:
    # Durability lives in the same HEALTH/DAMAGE pair as a minion's or
    # hero's health, not a separate GameTag.DURABILITY -- verified against
    # a real match's Power.log (a Wicked Knife's HEALTH=2 at creation,
    # DAMAGE incrementing as it's used, breaking exactly when DAMAGE
    # reaches HEALTH; GameTag.DURABILITY never appears at all despite
    # existing as an enum value).
    game, friendly, _opponent = _make_game_with_players()
    weapon = _register_card(
        game, entity_id=1, card_id="CS2_106", controller=friendly, zone=Zone.PLAY
    )
    weapon.tag_change(GameTag.CARDTYPE, CardType.WEAPON)
    weapon.tag_change(GameTag.ATK, 3)
    weapon.tag_change(GameTag.HEALTH, 2)
    card_db, _ = load_cards()

    state = _weapon_of(friendly, card_db)

    assert state is not None
    assert state.name == "Fiery War Axe"
    assert state.attack == 3
    assert state.durability == 2

    weapon.tag_change(GameTag.DAMAGE, 1)  # one attack landed
    state_after_hit = _weapon_of(friendly, card_db)

    assert state_after_hit is not None
    assert state_after_hit.durability == 1


def test_parse_log_tracks_a_real_weapons_durability() -> None:
    # Real-match ground truth (found while investigating why the export
    # always showed a weapon's durability as 0): Hearthstone's Power.log
    # never sets GameTag.DURABILITY at all -- a weapon's durability is the
    # same HEALTH-DAMAGE pair used for minions/heroes. The opponent equips
    # a Wicked Knife (1 attack, 2 durability) via Dagger Mastery on turn 4,
    # uses it once (attack.effects on turn 6), and it breaks.
    game = parse_log(WEAPON_FIXTURE)
    turn4 = next(t for t in game.turns if t.number == 4)
    turn6 = next(t for t in game.turns if t.number == 6)

    equip = next(a for a in turn4.actions if "Dolchbeherrschung gespielt" in a.headline)

    assert equip.effects == ["Tückisches Messer ausgerüstet"]
    assert any("Tückisches Messer zerbricht" in a.effects for a in turn6.actions)
    assert turn4.end.opponent_weapon is not None
    assert turn4.end.opponent_weapon.attack == 1
    assert turn4.end.opponent_weapon.durability == 1  # already used once this turn
    assert turn6.end.opponent_weapon is None


def test_parse_log_freezes_lethal_heros_health_against_a_post_mortem_tag_rewrite() -> None:
    # Real-match ground truth: the opponent's hero had a Prince Renathal
    # +10 Health aura in play (HEALTH tag showing 40) when a lethal attack
    # took DAMAGE to 41. Immediately after, the engine cleaned up that
    # aura (now moot, the hero is dead) and rewrote HEALTH back down to the
    # base 30 -- a value that was never true *during* the match, only
    # afterwards, as bookkeeping. The per-action effect trail (built from a
    # snapshot taken right after the lethal attack, before that rewrite)
    # already showed the correct 2 -> -1; the turn's closing snapshot must
    # match it, not the post-mortem HEALTH=30 giving -11.
    game = parse_log(LETHAL_HEALTH_REWRITE_FIXTURE)
    last_turn = game.turns[-1]

    assert last_turn.end.life.opponent_health == -1


def test_freeze_dead_hero_health_keeps_the_first_value_seen_not_the_latest() -> None:
    # Code-review-caught regression in the fix above: `after_block` runs
    # for *every* top-level block, and a hero that's already dead can
    # still show up in a later block's fresh `_snapshot_entities` call --
    # e.g. a separate deathrattle trigger, or a fatigue tick, later in the
    # same turn. If that later snapshot were allowed to overwrite the
    # frozen value, a HEALTH tag rewritten sometime between the two blocks
    # (the exact kind of post-mortem bookkeeping this fix guards against)
    # would silently corrupt it again. Freezing must be first-write-wins.
    game, friendly, opponent = _make_game_with_players()
    hero = _register_card(
        game, entity_id=10, card_id="HERO_02", controller=opponent, zone=Zone.PLAY
    )
    hero.tag_change(GameTag.CARDTYPE, CardType.HERO)
    opponent.tags[GameTag.HERO_ENTITY] = hero.id
    card_db, _ = load_cards()
    builder = _TurnBuilder(friendly.player_id, card_db)

    # The moment of death: HEALTH is still the real, buffed value.
    builder._freeze_dead_hero_health(  # noqa: SLF001
        friendly, opponent, {hero.id: (Zone.GRAVEYARD, 0, -1, 0, "HERO_02")}
    )
    # A later block re-snapshots the same still-dead hero; its tags have
    # since been rewritten for unrelated bookkeeping reasons.
    builder._freeze_dead_hero_health(  # noqa: SLF001
        friendly, opponent, {hero.id: (Zone.GRAVEYARD, 0, -11, 0, "HERO_02")}
    )

    assert builder._frozen_hero_health[hero.id] == -1  # noqa: SLF001


def test_parse_log_handles_a_third_match_after_a_players_role_flips() -> None:
    # Real-match ground truth: this session log holds two matches, and the
    # same account went from player_id=2 in the first to player_id=1 in
    # the second -- ordinary, but it used to crash `parse_log` outright
    # (see `MULTI_GAME_FIXTURE`), losing tracking for the rest of the
    # session. `parse_log` must isolate each match's lines before handing
    # them to hslog rather than replaying the whole session through one
    # shared `PlayerManager`.
    game = parse_log(MULTI_GAME_FIXTURE)

    assert game.game_index == 2
    assert game.own_class == "SHAMAN"
    assert game.opponent_class == "SHAMAN"
    assert len(game.turns) > 0


def test_parse_log_at_index_reopens_an_earlier_match_in_the_same_session() -> None:
    # MULTI_GAME_FIXTURE holds two matches back to back; parse_log always
    # reaches only the second (still in progress, 3 turns). Reopening the
    # *first* one -- the real scenario for a Verlauf entry whose match
    # finished before later ones were played in the same session log --
    # must recover the complete 17-turn WON match, not the second one.
    #
    # This also regression-tests a real bug caught while writing this
    # function: reading the requested match's lines via
    # `f.read(end - start)` (a byte-count difference between two text-mode
    # `tell()` cookies, which aren't byte offsets) silently truncated the
    # last line whenever the file had multi-byte UTF-8 content -- which a
    # German-locale log always does (umlauts, "Al'Akir"'s typographic
    # apostrophe, ...).
    game = parse_log_at_index(MULTI_GAME_FIXTURE, game_index=1)

    assert game.game_index == 1
    assert game.result == "WON"
    assert game.own_class == "SHAMAN"
    assert game.opponent_class == "WARLOCK"
    assert len(game.turns) == 17


def test_parse_log_at_index_rejects_an_out_of_range_index() -> None:
    with pytest.raises(NoGameFoundError):
        parse_log_at_index(MULTI_GAME_FIXTURE, game_index=3)
    with pytest.raises(NoGameFoundError):
        parse_log_at_index(MULTI_GAME_FIXTURE, game_index=0)


def test_parse_log_captures_an_attack_nested_inside_an_untracked_trigger() -> None:
    # Real-match ground truth (user-reported): a card's own end-of-turn
    # trigger (Factory Assemblybot's Miniaturize) is itself a top-level
    # BlockType.TRIGGER block, which isn't an action type and isn't a
    # deathrattle merge -- so it's never tracked as its own action. The
    # ATTACK block it summons and immediately triggers is *nested inside*
    # that untracked trigger, and used to inherit its untracked-ness
    # (`_depth` stayed incremented for the whole untracked block's
    # duration), silently dropping a real 6-damage attack from the log
    # even though the turn's closing snapshot already reflected it.
    game = parse_log(END_OF_TURN_TRIGGER_FIXTURE)
    turn27 = next(t for t in game.turns if t.number == 27)

    assert turn27.start.life.own_health == 23
    assert turn27.end.life.own_health == 15  # not 21 -- the missing 6 damage
    attack = next(a for a in turn27.actions if "Kopierbot #2" in a.headline)
    assert attack.headline == "Gegner: Kopierbot #2 (6 Angriff) → Dein Held: 21 → 15"


def test_zone_transition_line_narrates_weapon_equip_and_break() -> None:
    assert (
        _zone_transition_line(CardType.WEAPON, "Fiery War Axe", True, Zone.HAND, Zone.PLAY)
        == "Fiery War Axe ausgerüstet"
    )
    assert (
        _zone_transition_line(CardType.WEAPON, "Fiery War Axe", True, Zone.PLAY, Zone.GRAVEYARD)
        == "Fiery War Axe zerbricht"
    )
    # Same generic "Board -> Hand" wording as a bounced minion -- e.g. an
    # effect that returns an equipped weapon to hand instead of breaking it.
    assert (
        _zone_transition_line(CardType.WEAPON, "Fiery War Axe", True, Zone.PLAY, Zone.HAND)
        == "Fiery War Axe: Board → Hand"
    )


def test_diff_effects_shows_weapon_durability_loss() -> None:
    # A weapon's durability change was previously invisible: an earlier
    # version of `_snapshot_entities` read GameTag.DURABILITY for weapons,
    # which real matches never actually set (durability lives in the same
    # HEALTH/DAMAGE pair as everything else -- see `_snapshot_entities`),
    # so a before/after diff on a weapon entity always compared 0 vs 0.
    game, friendly, _opponent = _make_game_with_players()
    weapon = _register_card(
        game, entity_id=1, card_id="CS2_106", controller=friendly, zone=Zone.PLAY
    )
    weapon.tag_change(GameTag.CARDTYPE, CardType.WEAPON)
    weapon.tag_change(GameTag.ATK, 3)
    weapon.tag_change(GameTag.HEALTH, 2)
    weapon.tag_change(GameTag.DAMAGE, 1)
    before = {1: (Zone.PLAY, 3, 2, 0, "CS2_106")}
    after = _snapshot_entities(game)

    card_db, _ = load_cards()
    namer = _InstanceNamer(card_db)
    lines = _diff_effects(game, namer, friendly, before, after)

    assert lines == ["Fiery War Axe: 3/2 → 3/1"]


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


def test_extract_mulligan_returns_none_when_confirmation_is_missing() -> None:
    # A `Choices` (mulligan offer) packet with no matching `ChosenEntities`
    # (e.g. the log got truncated between the two) must not be reported as
    # "everything returned" -- that would present missing data as if it
    # were an observed fact. Absence of a confident answer must stay
    # absence, same as when no `Choices` packet exists at all.
    game, friendly, _opponent = _make_game_with_players()
    _register_card(game, entity_id=10, card_id="CS2_022", controller=friendly)  # Polymorph

    choice = hslog_packets.Choices(
        ts=None, entity=friendly.id, id=1, tasklist=None, type=ChoiceType.MULLIGAN, min=0, max=1
    )
    choice.choices = [10]
    packet_tree = [choice]  # no ChosenEntities packet at all

    mulligan = _extract_mulligan(packet_tree, game, friendly)

    assert mulligan is None


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

    play = next(a for a in turn2.actions if "Elfenbogenschützin #1 gespielt" in a.headline)

    assert play.headline == "Gegner: Elfenbogenschützin #1 gespielt → Ziel: Dein Held (Mana: 1 → 0)"
    assert play.effects == ["Dein Held: 30 → 29"]


def test_parse_log_attributes_generated_cards_to_their_source() -> None:
    # Turn 7: Ritual of Power's (Ritual der Macht) effect adds two Breezling
    # (Lüftchen) cards directly to hand (never drawn from the deck) -- they
    # must be attributed to their source, not silently appear in the next
    # hand snapshot as if from nowhere. Turn 11: Witch's Apprentice's (Hexe
    # in Ausbildung) battlecry does the same for Molten Blast (Geschmolzener
    # Schlag).
    game = parse_log(FIXTURE)
    turn7 = next(t for t in game.turns if t.number == 7)
    turn11 = next(t for t in game.turns if t.number == 11)

    ritual = next(a for a in turn7.actions if "Ritual der Macht gespielt" in a.headline)
    apprentice = next(
        a for a in turn11.actions if "Hexe in Ausbildung #1 gespielt" in a.headline
    )

    assert "Ritual der Macht → erzeugt Lüftchen #1" in ritual.effects
    assert "Ritual der Macht → erzeugt Lüftchen #2" in ritual.effects
    assert "Hexe in Ausbildung #1 → erzeugt Geschmolzener Schlag" in apprentice.effects


def test_parse_log_records_attack_action_against_hero_on_one_line() -> None:
    # Turn 4: the opponent's Elven Archer attacks the friendly hero. A
    # hero-target attack is folded into a single line (no separate result
    # bullet), per the format the user asked for.
    game = parse_log(FIXTURE)
    turn4 = next(t for t in game.turns if t.number == 4)

    attack = next(
        a
        for a in turn4.actions
        if "Elfenbogenschützin" in a.headline and "Angriff" in a.headline
    )

    assert attack.headline == "Gegner: Elfenbogenschützin #1 (1 Angriff) → Dein Held: 29 → 28"
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
    assert turn3.opening_draws == ["Klagender Dampf gezogen"]
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

    assert "Soldat von Al’Akir #4" in names
    assert "Soldat von Al’Akir #5" in names


def test_parse_log_filters_out_stat_changes_to_entities_never_on_board() -> None:
    # Turn 7: Ritual of Power's implementation touches several internal
    # "Soldier of Al'Akir" candidate entities that never actually reach
    # Zone.PLAY (only #4 and the newly-summoned #5 really end up on the
    # board, per the board snapshot) -- their stat churn is invisible to
    # both players and must not appear as if it happened on the board.
    game = parse_log(FIXTURE)
    turn7 = next(t for t in game.turns if t.number == 7)

    ritual = next(a for a in turn7.actions if "Ritual der Macht gespielt" in a.headline)
    board_names = {m.name for m in turn7.end.board.own}

    assert board_names == {
        "Klagender Dampf #1",
        "Himmelswallwächter #1",
        "Soldat von Al’Akir #4",
        "Soldat von Al’Akir #5",
    }
    assert not any("#1:" in e or "#2:" in e or "#3:" in e for e in ritual.effects)
    assert "Soldat von Al’Akir #4: 1/2 → 2/2" in ritual.effects
    assert "Soldat von Al’Akir #5 beschworen" in ritual.effects


def test_parse_log_captures_effect_triggered_draw_mid_action() -> None:
    # Turn 7: attacking the opponent's Acolyte of Pain ("whenever this
    # minion takes damage, draw a card") makes the opponent draw as a side
    # effect of the attack, not the turn's normal draw step -- it must show
    # up as an effect line on that attack, not silently vanish.
    game = parse_log(FIXTURE)
    turn7 = next(t for t in game.turns if t.number == 7)

    attack = next(a for a in turn7.actions if "Akolyth des Schmerzes" in a.headline)

    assert "Gegner zieht eine Karte" in attack.effects
    # The draw is a *consequence* of the damage (Acolyte of Pain's "whenever
    # this minion takes damage, draw a card") -- effect lines must reflect
    # the order things actually happened, not incidentally the order
    # entities were first created.
    damage_index = next(
        i for i, e in enumerate(attack.effects) if e.startswith("Akolyth des Schmerzes")
    )
    draw_index = attack.effects.index("Gegner zieht eine Karte")
    assert damage_index < draw_index


def test_parse_log_board_snapshot_includes_taunt_keyword() -> None:
    # By turn 7's start, the friendly player's Skywall Sentinel (kept in
    # the mulligan) is a 1/1 Taunt minion on the board, tagged "#1" -- a
    # stable per-name instance number so identical copies can be told
    # apart later in the match.
    game = parse_log(FIXTURE)
    turn7 = next(t for t in game.turns if t.number == 7)

    sentinel = next(m for m in turn7.start.board.own if m.name == "Himmelswallwächter #1")

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
    hand_names = [card.name for card in game.turns[0].start.hand.own_cards]
    assert "Himmelswallwächter" in hand_names


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


def test_parse_log_skips_unparseable_lines_instead_of_failing_the_whole_match(
    tmp_path: Path,
) -> None:
    # Real-world observed cause: once Power.log hits its 10MB size limit,
    # Hearthstone writes a non-log "Truncating log..." banner straight
    # into the file, which hslog's tokenizer can't parse -- and used to
    # abort the *entire* match over that one line. Insert the exact real
    # banner text into a copy of the real fixture and confirm parsing
    # still succeeds with the same result as the unmodified fixture.
    banner = (
        "\n\n"
        "==================================================================\n"
        "Truncating log, which has reached the size limit of 10000KB\n"
        "==================================================================\n"
    )
    lines = FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)
    midpoint = len(lines) // 2
    corrupted = tmp_path / "Power.log"
    corrupted.write_text("".join(lines[:midpoint]) + banner + "".join(lines[midpoint:]))

    game = parse_log(corrupted)
    expected = parse_log(FIXTURE)

    assert game.result == expected.result
    assert len(game.turns) == len(expected.turns)
    assert game.log_truncated is True
    assert expected.log_truncated is False


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
