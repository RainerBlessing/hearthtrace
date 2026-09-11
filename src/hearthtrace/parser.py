"""Parse a Hearthstone Power.log into a small, stable snapshot.

This module isolates the rest of the app from the `hslog`/`hearthstone`
package internals: callers only ever see `ParsedGame`.
"""

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hearthstone.cardxml import load as load_cards
from hearthstone.entities import Entity, Game, Player
from hearthstone.enums import BlockType, CardType, ChoiceType, GameTag, PlayState, Step, Zone
from hearthstone.utils import get_original_card_id
from hslog import LogParser
from hslog import packets as hslog_packets
from hslog.exceptions import ParsingError
from hslog.export import EntityTreeExporter, FriendlyPlayerExporter
from hslog.player import coerce_to_entity_id

# Top-level block types that represent a player decision worth showing as
# its own line in the turn-by-turn action log. Blocks nested inside one of
# these (battlecries, triggered secrets, sub-spells, ...) are not recorded
# separately -- their effects are folded into the enclosing action via
# before/after state diffing instead.
_ACTION_BLOCK_TYPES = frozenset(
    {BlockType.PLAY, BlockType.ATTACK, BlockType.POWER, BlockType.FATIGUE}
)

# A death, and the deathrattle it triggers, resolve as their own *separate*
# top-level blocks, siblings of (not nested inside) the block that caused
# the death -- verified against a real match's packet tree: an ATTACK
# block killing a minion is followed by a top-level BlockType.DEATHS block
# (bookkeeping: moves it to the graveyard) and *then* a top-level
# BlockType.TRIGGER block with `trigger_keyword == GameTag.DEATHRATTLE`
# (the deathrattle's actual effect, e.g. Twilight Egg hatching into
# Accelerated Whelp). Neither has a meaningful "actor" entity for a
# headline of its own, so both are tracked here purely to fold their
# effects into whichever action most recently ran instead of losing them.
def _is_deathrattle_trigger(block: Any) -> bool:
    return block.type == BlockType.TRIGGER and block.trigger_keyword == GameTag.DEATHRATTLE


def _is_merge_only_block(block: Any) -> bool:
    return block.type == BlockType.DEATHS or _is_deathrattle_trigger(block)

# Public (not module-private) since card_info.py's own printed-keyword
# list reuses the labels for the subset of keywords both modules track,
# so a wording fix only has to happen in one place -- same reasoning
# markdown_export.RESULT_LABELS was made public for.
KEYWORD_LABELS: dict[str, str] = {
    "taunt": "Spott",
    "divine_shield": "Göttlicher Schild",
    "frozen": "Eingefroren",
    "stealth": "Getarnt",
    "windfury": "Windfury",
}

# (GameTag, German label) for the minion keywords worth surfacing in a board
# snapshot. Not exhaustive -- only the ones that commonly change a decision
# (per the user's own guidance: "nicht jeder interne Hearthstone-State muss
# exportiert werden").
_KEYWORD_TAGS: list[tuple[GameTag, str]] = [
    (GameTag.TAUNT, KEYWORD_LABELS["taunt"]),
    (GameTag.DIVINE_SHIELD, KEYWORD_LABELS["divine_shield"]),
    (GameTag.FROZEN, KEYWORD_LABELS["frozen"]),
    (GameTag.STEALTH, KEYWORD_LABELS["stealth"]),
    (GameTag.WINDFURY, KEYWORD_LABELS["windfury"]),
]


class NoGameFoundError(Exception):
    """Raised when a Power.log contains no CREATE_GAME block.

    This happens for a log captured before any match started, or a
    truncated/corrupted capture -- there is simply no game data to parse.
    """


@dataclass
class MulliganChoice:
    # Card ids the friendly player kept in their opening hand, and card ids
    # they sent back to the deck, both in the order Hearthstone reports
    # them. Opponent mulligans are not tracked (their cards are hidden).
    kept: list[str]
    returned: list[str]


@dataclass
class ManaState:
    available: int
    maximum: int
    # Crystals locked *this* turn (from overload committed on a previous
    # turn) -- already subtracted out of `available`, but shown separately
    # too since "why is available lower than maximum" is decision-relevant
    # on its own, especially for an Overload class like Shaman.
    locked: int
    overload_pending: int  # newly committed this turn/action; will lock *next* turn


@dataclass
class LifeState:
    own_health: int
    own_armor: int
    opponent_health: int
    opponent_armor: int


@dataclass
class MinionState:
    name: str
    attack: int
    health: int
    keywords: list[str]
    # The card behind this display name -- e.g. for a card-info tooltip,
    # which needs the real card (localized names/`#N` suffixes/"Unbekannte
    # Karte" aren't reliable lookup keys). Empty for an unrevealed entity
    # (see `_card_name`).
    card_id: str
    # How many attacks this minion has left *this turn* -- 0, 1, or 2 with
    # Windfury. "kann angreifen" in `keywords` is just `attacks_remaining >
    # 0`; this is the actual count a caller needs to tell "used one of two
    # Windfury attacks" apart from "used none of two", which a boolean
    # can't (both look identical as a plain keyword). User-requested,
    # generalized from a review hint that was originally going to be
    # Al'Akir/Windfury-specific: verified against a real match that any
    # minion can end a turn with an unused attack, not just a Windfury
    # one, so the hint (and this field) must not special-case Windfury.
    attacks_remaining: int = 0


@dataclass
class HandCard:
    name: str
    card_id: str


@dataclass
class HandState:
    # The friendly player's own hand (always fully known), and just a
    # count for the opponent's -- their identities are hidden information
    # the player did not have at this point in the match.
    own_cards: list[HandCard]
    opponent_count: int


@dataclass
class BoardState:
    own: list[MinionState]
    opponent: list[MinionState]


@dataclass
class WeaponState:
    name: str
    attack: int
    durability: int
    card_id: str


@dataclass
class TurnSnapshot:
    mana: ManaState
    life: LifeState
    hand: HandState
    board: BoardState
    # None when that player has no weapon equipped -- a player has at most
    # one at a time, unlike minions, so (unlike `board`) this is a single
    # optional value rather than a list.
    own_weapon: WeaponState | None = None
    opponent_weapon: WeaponState | None = None


@dataclass
class Action:
    # Pre-rendered, ready-to-print text: the single headline for this
    # action, and zero or more indented result lines below it (summons,
    # damage/heal, deaths). Kept as plain strings rather than further
    # structured data since Markdown rendering is this data's only
    # consumer.
    headline: str
    effects: list[str] = field(default_factory=list)


@dataclass
class Turn:
    number: int
    player_name: str  # "Du" | "Gegner"
    # The turn's own automatic draw (nothing else happens between the
    # previous turn's Ende and this one's Start) -- shown separately from
    # `actions` since `start` already reflects the post-draw hand; listing
    # it again as action #1 would make it look like a decision that hadn't
    # happened yet at the point `start` describes.
    opening_draws: list[str]
    start: TurnSnapshot
    actions: list[Action]
    end: TurnSnapshot


@dataclass
class ParsedGame:
    """A stable snapshot of one match, derived from a single Power.log.

    Known MVP limitations (accepted scope boundaries, not bugs):
    - `starting_deck` is only reliable for constructed formats (Standard/
      Wild/Practice). In Arena, Battlegrounds, Tavern Brawl, etc. it may be
      empty or not represent a real 30-card deck — callers building on this
      (e.g. a "remaining deck" view) should treat a suspiciously short or
      empty `starting_deck` as "not supported for this mode", not as "few
      cards left".
    - `turns` actions cover top-level PLAY/ATTACK/POWER/FATIGUE blocks only
      (one decision = one action). Their effects (summons, damage, heals,
      deaths) are derived by diffing all entities' health/zone/armor across
      the block, so multi-step chains (cleave, deathrattles, auras
      recalculating) are captured too, best-effort. "Gezogene Karten"
      action lines aren't a separate packet-level event either -- they're
      inferred from the hand snapshot delta between two turns. Friendly
      Discover picks (offered + chosen) are appended as their own action at
      the end of the turn they happened in, not nested under the play/power
      action that triggered them -- opponent discoveries are never shown
      (what was offered to them is never visible to us).
    - If the game reconnects mid-match, Hearthstone re-emits CREATE_GAME,
      and `parse_log` (which always reads the last game in the file) would
      then reflect only the post-reconnect fragment, missing mulligan and
      earlier turns. Not handled — out of scope for the MVP.
    """

    own_class: str
    opponent_class: str
    # Card ids known by the end of the match (drawn, played, or otherwise
    # revealed). May be fewer than 30 if not every card was seen, and may
    # contain duplicates.
    starting_deck: list[str]
    result: str  # "WON" | "LOST" | "TIED" | "CONCEDED" | "UNKNOWN"
    # Card ids from `starting_deck` that are NOT in Zone.DECK at the end of
    # the match (i.e. they were drawn, played, discarded, etc. at some
    # point and never ended up back in the deck). This is a final-state
    # snapshot, not a history of every card that was ever drawn -- a card
    # mulliganed away and never redrawn is correctly excluded from this
    # list, since it ends the match back in Zone.DECK. Duplicates allowed;
    # not deduplicated against `starting_deck`. (Field name kept as
    # `drawn_card_ids` for minimal API churn; semantically it now means
    # "not currently in deck".)
    drawn_card_ids: list[str]
    # 1-based count of CREATE_GAME blocks seen in the file up to and
    # including this game (i.e. which match, in order, this is within a
    # single session's Power.log). A session log can contain several
    # matches; this lets callers tell two different matches with the same
    # `result` apart for dedup purposes.
    game_index: int
    # None when no mulligan Choices packet for the friendly player was
    # found, or one was found but its confirmation (ChosenEntities) never
    # arrived (e.g. an incomplete/truncated log cut off between the two) --
    # absence, not an empty mulligan, since every real match has one.
    mulligan: MulliganChoice | None = None
    turns: list[Turn] = field(default_factory=list)
    # True when Hearthstone's own "Truncating log, which has reached the
    # size limit of 10000KB" banner was found in the file: past that
    # point, Hearthstone itself stops writing to Power.log entirely (not
    # a parsing failure -- observed directly: the banner is the literal
    # last line of the file, with nothing after it and no new session
    # folder created). Everything from there on, including the match's
    # true final result, was simply never recorded anywhere -- no parser
    # can recover it. Callers should surface this rather than presenting
    # a stale non-terminal `result` (e.g. "PLAYING") as if it were current.
    log_truncated: bool = False


def _class_name(player: Player, card_db: Any) -> str:
    # `player.starting_hero` (not `.hero`, which is whatever the *current*
    # hero entity is) -- some effects (Lord Jaraxxus, Majordomo Executus)
    # replace a player's hero mid-match, but `own_class`/`opponent_class`
    # describe the class picked at deck-select for the whole export, and
    # must not flip to a later transform's class.
    hero = player.starting_hero
    if hero is None or not hero.card_id:
        return "UNKNOWN"
    return str(card_db[hero.card_id].card_class.name)


def _friendly_and_opponent(game: Game, friendly_id: int | None) -> tuple[Player, Player]:
    me = game.get_player(friendly_id) if friendly_id else game.players[0]
    assert me is not None
    opponent = next(p for p in game.players if p is not me)
    return me, opponent


def _result_for(player: Player) -> str:
    playstate = player.tags.get(GameTag.PLAYSTATE, PlayState.INVALID)
    return PlayState(playstate).name if playstate else "UNKNOWN"


def _player_label(player: Player | None, friendly_player: Player) -> str:
    return "Du" if player is friendly_player else "Gegner"


def _active_player(game: Game, turn_number: int) -> Player | None:
    """Whose turn it actually is, read from the live CURRENT_PLAYER tag --
    not assumed from turn-number parity. A card like Temporus ("Your
    opponent takes 2 turns. Then you take 2 turns.") breaks the normal
    strict-alternation assumption a parity computation would make, while
    the global turn counter keeps incrementing right through it -- a
    parity-based `active` silently mislabels every turn's own/opponent
    attribution for the rest of the match once that happens. Verified
    against a real match: right after a Temporus play, one turn's entire
    action trail was clearly the opponent's ("Gegner: ..." throughout),
    but a parity computation labeled that turn "Du".
    """
    for player in game.players:
        if player.tags.get(GameTag.CURRENT_PLAYER, 0):
            return player
    # Defensive fallback, never observed in practice: CURRENT_PLAYER is
    # already set (verified in a real log) well before STEP reaches
    # MAIN_ACTION, which is what triggers this call -- but if it were
    # ever missing, falling back to the old parity assumption is at least
    # right for a normal, extra-turn-free match.
    first = game.first_player
    others = [p for p in game.players if p is not first]
    return first if turn_number % 2 == 1 else (others[0] if others else first)


# --- Turn snapshots (mana / life / hand / board) --------------------------


def _mana_state(player: Player) -> ManaState:
    # Sourced directly from Hearthstone's own RESOURCES/RESOURCES_USED/
    # OVERLOAD_LOCKED tags -- these already reflect any cost discount the
    # game applied, so there's no need (or reason to trust our own guess
    # over the game's) to compute a cost ourselves. `max(0, ...)` only
    # guards the *display*: a real match showed RESOURCES_USED transiently
    # exceeding available crystals for a cost-modified card, and negative
    # mana must never appear in the export regardless of why that happens.
    resources = player.tags.get(GameTag.RESOURCES, 0)
    used = player.tags.get(GameTag.RESOURCES_USED, 0)
    locked = player.tags.get(GameTag.OVERLOAD_LOCKED, 0)
    overload_pending = player.tags.get(GameTag.OVERLOAD, 0)
    return ManaState(
        available=max(0, resources - used - locked),
        maximum=resources,
        locked=locked,
        overload_pending=overload_pending,
    )


def _hero_health(hero: Entity, frozen: dict[int, int] | None) -> int:
    # A dead hero's own HEALTH tag can still change afterwards -- e.g. an
    # aura enchantment (Prince Renathal's +10 Health) attached to the hero
    # gets cleaned up as part of the same death processing, which
    # recalculates HEALTH back down from 40 to the base 30. That rewrite is
    # pure post-mortem bookkeeping, not a gameplay event, but a live re-read
    # of `HEALTH - DAMAGE` at export time can't tell the difference and
    # would report a wrong, inflated-magnitude number (observed for real:
    # -11 instead of the true -1 at the moment of death). Once the hero is
    # in the graveyard, trust the value frozen the *first* time it was seen
    # there (`_TurnBuilder._freeze_dead_hero_health`, called right after
    # every top-level block) instead of re-reading its tags now -- a block
    # that runs *later* in the same turn (a separate deathrattle trigger, a
    # fatigue tick, ...) must not be allowed to overwrite it with an
    # already-corrupted value.
    if hero.zone == Zone.GRAVEYARD and frozen is not None:
        cached = frozen.get(hero.id)
        if cached is not None:
            return cached
    return hero.tags.get(GameTag.HEALTH, 0) - hero.tags.get(GameTag.DAMAGE, 0)


def _life_state(
    me: Player, opponent: Player, frozen: dict[int, int] | None = None
) -> LifeState:
    my_hero, their_hero = me.hero, opponent.hero
    assert my_hero is not None and their_hero is not None
    return LifeState(
        own_health=_hero_health(my_hero, frozen),
        own_armor=my_hero.tags.get(GameTag.ARMOR, 0),
        opponent_health=_hero_health(their_hero, frozen),
        opponent_armor=their_hero.tags.get(GameTag.ARMOR, 0),
    )


def _card_name(entity: Entity, card_db: Any) -> str:
    # Deliberately *not* resolved retroactively once the card is later
    # revealed: a line built at this point in the replay must reflect only
    # what was actually knowable then. A later mention of the same entity
    # (e.g. once the opponent actually plays it) calls this again at that
    # later point and correctly shows the real name then -- there's no
    # need, and it would be actively wrong for analysis, to rewrite this
    # earlier line with knowledge from the future.
    if not entity.card_id:
        return "Unbekannte Karte"
    card = card_db.get(entity.card_id)
    return card.name if card else entity.card_id


def _hand_state(me: Player, opponent: Player, card_db: Any) -> HandState:
    own = sorted(me.in_zone(Zone.HAND), key=lambda e: e.tags.get(GameTag.ZONE_POSITION, 0))
    return HandState(
        own_cards=[
            HandCard(name=_card_name(entity, card_db), card_id=entity.card_id or "")
            for entity in own
        ],
        opponent_count=sum(1 for _ in opponent.in_zone(Zone.HAND)),
    )


@dataclass
class _AttackReadiness:
    """Per-match, per-entity id tracking needed to correctly compute "kann
    angreifen" across a same-turn transform (Polymorph/Hex-style effects,
    or a multi-minion combo like Colifero the Artist turning several
    minions into Alexstrasza): verified against a real match's raw log,
    Hearthstone's own CHANGE_ENTITY packet resets *both* EXHAUSTED and
    NUM_ATTACKS_THIS_TURN to a fresh baseline for the transform's result,
    unconditionally -- even for an entity that had correctly been
    exhausted=1 (summoning sickness, or an already-used attack) the
    instant before, and nothing ever corrects either tag back for the
    rest of that turn. Neither live tag is trustworthy right after a
    transform, so attack readiness is computed entirely from the two
    facts below instead -- both about the *entity id*, which a transform
    (same id, only the card changes) must not reset, and both recorded
    independently of the live tags for exactly that reason.
    """

    turn_number: int
    # entity_id -> the turn it first reached Zone.PLAY. First-write-wins,
    # kept for the whole match (an entity only ever enters play once).
    entered_play_turn: dict[int, int]
    # entity_id -> how many attacks it has made *this* turn. A plain
    # count, not a boolean: a Windfury minion gets two attacks a turn, and
    # a first attack must not be mistaken for "done for the turn" --
    # verified against a real match (Al'Akir, Lord of Storms attacking
    # only once on a turn, its second Windfury attack left unused). Reset
    # to empty at the start of every turn (see `_TurnBuilder.on_turn_ready`).
    attacks_used_this_turn: dict[int, int]


def _attacks_remaining(entity: Entity, readiness: _AttackReadiness) -> int:
    attack = entity.tags.get(GameTag.ATK, 0)
    frozen = entity.tags.get(GameTag.FROZEN, 0)
    if attack <= 0 or frozen:
        return 0
    has_charge_or_rush = entity.tags.get(GameTag.CHARGE, 0) or entity.tags.get(GameTag.RUSH, 0)
    summoning_sick = (
        readiness.entered_play_turn.get(entity.id) == readiness.turn_number
        and not has_charge_or_rush
    )
    if summoning_sick:
        return 0
    allowed_attacks = 2 if entity.tags.get(GameTag.WINDFURY, 0) else 1
    return max(0, allowed_attacks - readiness.attacks_used_this_turn.get(entity.id, 0))


def _minion_keywords(entity: Entity, readiness: _AttackReadiness) -> list[str]:
    keywords = [label for tag, label in _KEYWORD_TAGS if entity.tags.get(tag, 0)]
    if _attacks_remaining(entity, readiness) > 0:
        keywords.append("kann angreifen")
    return keywords


def _minion_state(
    entity: Entity, namer: "_InstanceNamer", readiness: _AttackReadiness
) -> MinionState:
    health = entity.tags.get(GameTag.HEALTH, 0) - entity.tags.get(GameTag.DAMAGE, 0)
    return MinionState(
        name=namer.minion_name(entity),
        attack=entity.tags.get(GameTag.ATK, 0),
        health=health,
        keywords=_minion_keywords(entity, readiness),
        card_id=entity.card_id or "",
        attacks_remaining=_attacks_remaining(entity, readiness),
    )


def _board_of(
    player: Player, namer: "_InstanceNamer", readiness: _AttackReadiness
) -> list[MinionState]:
    minions = [e for e in player.in_zone(Zone.PLAY) if e.type == CardType.MINION]
    minions.sort(key=lambda e: e.tags.get(GameTag.ZONE_POSITION, 0))
    return [_minion_state(entity, namer, readiness) for entity in minions]


def _board_state(
    me: Player, opponent: Player, namer: "_InstanceNamer", readiness: _AttackReadiness
) -> BoardState:
    return BoardState(
        own=_board_of(me, namer, readiness),
        opponent=_board_of(opponent, namer, readiness),
    )


def _weapon_of(player: Player, card_db: Any) -> WeaponState | None:
    weapon = next((e for e in player.in_zone(Zone.PLAY) if e.type == CardType.WEAPON), None)
    if weapon is None:
        return None
    # Durability lives in the same HEALTH/DAMAGE pair as a minion's or
    # hero's health, not a separate GameTag.DURABILITY -- see the comment
    # on `_snapshot_entities`.
    health = weapon.tags.get(GameTag.HEALTH, 0)
    damage = weapon.tags.get(GameTag.DAMAGE, 0)
    return WeaponState(
        name=_card_name(weapon, card_db),
        attack=weapon.tags.get(GameTag.ATK, 0),
        durability=health - damage,
        card_id=weapon.card_id or "",
    )


def _turn_snapshot(
    me: Player,
    opponent: Player,
    active: Player,
    card_db: Any,
    namer: "_InstanceNamer",
    readiness: _AttackReadiness,
    frozen: dict[int, int] | None = None,
) -> TurnSnapshot:
    # Life/hand/board are always shown from the friendly player's own point
    # of view (Du/Gegner); mana is shown for whoever's turn it is, since
    # that's the resource that turn's actions are actually spent from.
    return TurnSnapshot(
        mana=_mana_state(active),
        life=_life_state(me, opponent, frozen),
        hand=_hand_state(me, opponent, card_db),
        board=_board_state(me, opponent, namer, readiness),
        own_weapon=_weapon_of(me, card_db),
        opponent_weapon=_weapon_of(opponent, card_db),
    )


# --- Action log (before/after state diffing) -------------------------------

# entity_id -> (zone, attack, effective_health, armor, card_id) for every
# real card entity (not the Game/Player objects themselves), captured just
# before and just after a top-level block runs. attack/health/armor default
# to 0 for entities that don't carry those tags (spells, ...) -- harmless,
# since a 0-vs-0 comparison never produces a spurious diff line. Weapon
# durability also lives in the HEALTH/DAMAGE pair, same as a minion's or
# hero's health -- verified directly against a real match's Power.log
# (a Wicked Knife's HEALTH=2 at creation, DAMAGE incrementing 1 then 2 as
# it's used, moving to Zone.GRAVEYARD exactly when DAMAGE reaches HEALTH);
# GameTag.DURABILITY never appears in that log at all, despite existing as
# an enum value -- an earlier version of this code read DURABILITY for
# weapons and always got 0, since the tag is simply never set.
# card_id is captured too (not just read live at diff time) so a target's
# *pre-transform* identity can still be named after a Hex/Polymorph-style
# effect has already replaced it with a different card.
_EntitySnapshot = dict[int, tuple[Zone, int, int, int, str]]
_NOT_TRACKED: tuple[Zone, int, int, int, str] = (Zone.INVALID, 0, 0, 0, "")


def _snapshot_entities(game: Game) -> _EntitySnapshot:
    snapshot: _EntitySnapshot = {}
    for entity in game.entities:
        if entity is game or isinstance(entity, Player):
            continue
        attack = entity.tags.get(GameTag.ATK, 0)
        armor = entity.tags.get(GameTag.ARMOR, 0)
        health = entity.tags.get(GameTag.HEALTH, 0)
        damage = entity.tags.get(GameTag.DAMAGE, 0)
        snapshot[entity.id] = (entity.zone, attack, health - damage, armor, entity.card_id or "")
    return snapshot


class _InstanceNamer:
    """Names entities for display, assigning each minion a stable, ever-
    increasing `#N` suffix (per card name) the first time it's displayed,
    so identical copies -- e.g. two "Soldier of Al'Akir" tokens -- can be
    told apart across board snapshots and action lines referring to the
    same physical entity. Numbers are assigned lazily and never reused,
    for the lifetime of one `parse_log` call."""

    def __init__(self, card_db: Any) -> None:
        self._card_db = card_db
        # Keyed by (entity id, card name) rather than just entity id: a
        # transformed minion (Hex, Polymorph, ...) keeps its entity id but
        # becomes a different card, and should be numbered among *that*
        # card's copies, not carry over its pre-transform number.
        self._numbers: dict[tuple[int, str], int] = {}
        self._next_number: dict[str, int] = {}

    def _numbered(self, entity_id: int, base: str) -> str:
        key = (entity_id, base)
        number = self._numbers.get(key)
        if number is None:
            number = self._next_number.get(base, 0) + 1
            self._next_number[base] = number
            self._numbers[key] = number
        return f"{base} #{number}"

    def minion_name(self, entity: Entity) -> str:
        return self._numbered(entity.id, _card_name(entity, self._card_db))

    def minion_name_for_card_id(self, entity_id: int, card_id: str) -> str:
        """Names a minion by a *specific* card id rather than the entity's
        current one -- for naming a transform target by what it was."""
        card = self._card_db.get(card_id) if card_id else None
        base = card.name if card else (card_id or "Unbekannte Karte")
        return self._numbered(entity_id, base)

    def display_name(self, entity: Entity, friendly_player: Player) -> str:
        if entity.type == CardType.HERO:
            return "Dein Held" if entity.controller is friendly_player else "Gegnerischer Held"
        if entity.type == CardType.MINION:
            return self.minion_name(entity)
        return _card_name(entity, self._card_db)


# Zones a player actually sees and can reason about: the board (either
# side) and their own hand. A minion buffed while sitting in some other,
# internal-only zone (e.g. a candidate-summon pool a card's implementation
# uses to compute which tokens to actually place) never becomes visible to
# either player and would just be confusing noise in the export.
_VISIBLE_ZONES = frozenset({Zone.PLAY, Zone.HAND})


def _zone_label(zone: Zone) -> str:
    # Board changes read naturally without a zone tag (that's the default
    # expectation); a change to something sitting in a hand needs to say so
    # explicitly, or it reads as if it just appeared on the board.
    return " (Hand)" if zone == Zone.HAND else ""


def _play_zone_transition_line(
    name: str, zone_before: Zone, zone_after: Zone, *, die_verb: str, enter_verb: str
) -> str | None:
    """Shared shape for anything that lives in Zone.PLAY (minions,
    weapons): leaving play to the graveyard, bouncing back to hand (same
    generic "Board -> Hand" wording either way -- its board-modified stats
    are gone, not "changed", so this replaces the generic stat-diff line),
    or entering play from anywhere else (a freshly-arrived entity's stats
    are already visible in the board/weapon snapshot, no need to also
    print a 0/0 -> atk/hp diff). Only the death/entry verbs differ by
    entity type ("stirbt"/"beschworen" for minions, "zerbricht"/
    "ausgerüstet" for weapons)."""
    if zone_before == Zone.PLAY and zone_after == Zone.GRAVEYARD:
        return f"{name} {die_verb}"
    if zone_before == Zone.PLAY and zone_after == Zone.HAND:
        return f"{name}: Board → Hand"
    if zone_after == Zone.PLAY and zone_before != Zone.PLAY:
        return f"{name} {enter_verb}"
    return None


def _minion_zone_transition_line(name: str, zone_before: Zone, zone_after: Zone) -> str | None:
    return _play_zone_transition_line(
        name, zone_before, zone_after, die_verb="stirbt", enter_verb="beschworen"
    )


def _weapon_zone_transition_line(name: str, zone_before: Zone, zone_after: Zone) -> str | None:
    return _play_zone_transition_line(
        name, zone_before, zone_after, die_verb="zerbricht", enter_verb="ausgerüstet"
    )


def _zone_transition_line(
    entity_type: CardType, name: str, is_friendly_owner: bool, zone_before: Zone, zone_after: Zone
) -> str | None:
    if entity_type == CardType.MINION:
        minion_line = _minion_zone_transition_line(name, zone_before, zone_after)
        if minion_line is not None:
            return minion_line
    if entity_type == CardType.WEAPON:
        weapon_line = _weapon_zone_transition_line(name, zone_before, zone_after)
        if weapon_line is not None:
            return weapon_line
    if zone_before == Zone.SECRET and zone_after != Zone.SECRET:
        # Generic, no card-specific knowledge needed: a Secret leaving its
        # hidden zone means it just triggered (its identity is revealed as
        # part of that, so `name` is already the real card name here).
        return f"Secret ausgelöst: {name}"
    if zone_before == Zone.DECK and zone_after == Zone.HAND:
        return f"{name} gezogen" if is_friendly_owner else "Gegner zieht eine Karte"
    return None


def _stat_diff_lines(
    is_hero: bool,
    name: str,
    previous: tuple[Zone, int, int, int, str],
    current: tuple[Zone, int, int, int, str],
) -> list[str]:
    _zone_before, attack_before, health_before, armor_before, _card_id_before = previous
    zone_after, attack_after, health_after, armor_after, _card_id_after = current
    label = f"{name}{_zone_label(zone_after)}"
    lines = []
    if health_after != health_before or (not is_hero and attack_after != attack_before):
        if is_hero:
            lines.append(f"{label}: {health_before} → {health_after}")
        else:
            lines.append(
                f"{label}: {attack_before}/{health_before} → {attack_after}/{health_after}"
            )
    if armor_after != armor_before:
        lines.append(f"{label}: Rüstung {armor_before} → {armor_after}")
    return lines


def _entity_diff_lines(
    entity_type: CardType,
    name: str,
    is_friendly_owner: bool,
    previous: tuple[Zone, int, int, int, str] | None,
    current: tuple[Zone, int, int, int, str],
) -> list[str]:
    resolved_previous = previous or _NOT_TRACKED
    transition = _zone_transition_line(
        entity_type, name, is_friendly_owner, resolved_previous[0], current[0]
    )
    if transition is not None:
        return [transition]
    if resolved_previous[0] not in _VISIBLE_ZONES and current[0] not in _VISIBLE_ZONES:
        # Neither before nor after touches the board or a hand -- this
        # entity was never visible to either player, so its stat churn
        # (e.g. a card implementation's internal candidate-summon pool)
        # isn't something that "happened" from the player's perspective.
        # Applies just as much to a brand-new entity (`previous is None`)
        # as to one we already had a snapshot for -- e.g. Hearthstone's
        # own client-side history-tile plumbing creates short-lived
        # REMOVEDFROMGAME entities that are never real board events.
        # NOTE: a minion that is both summoned *and* removed within one
        # block without ever being observed in a visible zone at a block
        # boundary (previous is None, current zone e.g. GRAVEYARD) is
        # still silently dropped here -- not yet seen in a real match, so
        # not worth a speculative fix; revisit if one turns up.
        return []
    if previous is None:
        return []
    return _stat_diff_lines(entity_type == CardType.HERO, name, resolved_previous, current)


def _format_minion_stats(entity: Entity, zone: Zone, readiness: _AttackReadiness) -> str:
    attack = entity.tags.get(GameTag.ATK, 0)
    health = entity.tags.get(GameTag.HEALTH, 0) - entity.tags.get(GameTag.DAMAGE, 0)
    # Keywords like Taunt or "kann angreifen" are board concepts -- showing
    # them for a card sitting in hand would be misleading.
    keywords = _minion_keywords(entity, readiness) if zone == Zone.PLAY else []
    if keywords:
        return f"{attack}/{health}, {', '.join(keywords)}"
    return f"{attack}/{health}"


def _transform_line(
    entity: Entity,
    namer: _InstanceNamer,
    friendly_player: Player,
    previous: tuple[Zone, int, int, int, str] | None,
    current: tuple[Zone, int, int, int, str],
    readiness: _AttackReadiness,
) -> str | None:
    """A minion that changes card id while staying in the same visible
    zone (Hex/Polymorph on the board, or a hand card whose generated
    identity resolves to something else) keeps the same entity id, so the
    generic diff would otherwise just report it as an ordinary stat change
    under its *new* name -- losing which minion was actually targeted or
    replaced. Named explicitly instead: "{old} transformiert zu {new}
    (atk/hp, keywords)", tagged "(Hand)" when that's where it happened."""
    if entity.type != CardType.MINION or previous is None:
        return None
    zone_before, _attack_before, _health_before, _armor_before, card_id_before = previous
    zone_after, _attack_after, _health_after, _armor_after, card_id_after = current
    if zone_before != zone_after or zone_before not in _VISIBLE_ZONES:
        return None
    if card_id_before == card_id_after:
        return None
    old_name = namer.minion_name_for_card_id(entity.id, card_id_before) + _zone_label(zone_before)
    new_name = namer.display_name(entity, friendly_player)
    stats = _format_minion_stats(entity, zone_after, readiness)
    return f"{old_name} transformiert zu {new_name} ({stats})"


def _generated_into_hand_line(
    game: Game,
    entity: Entity,
    namer: _InstanceNamer,
    friendly_player: Player,
    previous: tuple[Zone, int, int, int, str] | None,
    current: tuple[Zone, int, int, int, str],
) -> str | None:
    """A card that appears directly in a hand without ever having been in
    the deck (Discover excepted -- that's tracked separately) was added by
    some effect, e.g. a Battlecry generating a random card. Silently
    letting it just show up in the next hand snapshot would look like it
    came from nowhere -- name its source when the game tells us
    (`initial_creator`), otherwise say plainly that its origin is unknown
    rather than pretending it was drawn."""
    if previous is not None or current[0] != Zone.HAND:
        return None
    name = namer.display_name(entity, friendly_player)
    creator_id = entity.initial_creator
    creator = game.find_entity_by_id(creator_id) if creator_id else None
    if creator is not None:
        return f"{namer.display_name(creator, friendly_player)} → erzeugt {name}"
    return f"{name} zur Hand hinzugefügt (Quelle unbekannt)"


def _ordered_entity_ids(after: _EntitySnapshot, order: list[int] | None) -> list[int]:
    """`after`'s own key order is entity creation order (an artifact of
    `_snapshot_entities` iterating `game.entities`), not the order things
    actually happened -- e.g. a damage-triggered draw's new hand card was
    *created* long before the attack that triggers it. `order` (from
    `_TurnBuilder`'s live touch-tracking) reflects real touch order for
    whatever it saw; entities it never explicitly touched (or when no
    tracking happened at all) fall back to the original creation order."""
    if not order:
        return list(after)
    touched = [entity_id for entity_id in order if entity_id in after]
    touched_set = set(touched)
    remaining = [entity_id for entity_id in after if entity_id not in touched_set]
    return touched + remaining


def _diff_effects(
    game: Game,
    namer: _InstanceNamer,
    friendly_player: Player,
    before: _EntitySnapshot,
    after: _EntitySnapshot,
    exclude: frozenset[int] = frozenset(),
    order: list[int] | None = None,
    readiness: _AttackReadiness | None = None,
) -> list[str]:
    readiness = readiness or _AttackReadiness(0, {}, {})
    lines = []
    for entity_id in _ordered_entity_ids(after, order):
        if entity_id in exclude:
            continue
        current = after[entity_id]
        entity = game.find_entity_by_id(entity_id)
        if entity is None:
            continue
        previous = before.get(entity_id)
        transform = _transform_line(entity, namer, friendly_player, previous, current, readiness)
        if transform is not None:
            lines.append(transform)
            continue
        generated = _generated_into_hand_line(
            game, entity, namer, friendly_player, previous, current
        )
        if generated is not None:
            lines.append(generated)
            continue
        name = namer.display_name(entity, friendly_player)
        is_friendly_owner = entity.controller is friendly_player
        lines += _entity_diff_lines(entity.type, name, is_friendly_owner, previous, current)
    return lines


_BLOCK_VERBS = {
    BlockType.PLAY: "gespielt",
    BlockType.POWER: "eingesetzt",
}


def _mana_headline_suffix(mana_before: int | None, mana_after: int | None) -> str:
    if mana_before is None or mana_after is None:
        return ""
    if mana_before == mana_after:
        # Net 0 mana spent -- could be a genuinely free action, or a cost
        # discount/refund that happened to net out. Either way, that's a
        # real, decision-relevant fact (verified live against a real
        # match: a discounted Bloodmage Thalnos costing 0 despite its
        # printed cost of 2) -- worth saying explicitly rather than
        # silently showing no mana note at all, which reads as "not
        # tracked" rather than "confirmed free".
        return " (Kosten: 0)"
    return f" (Mana: {mana_before} → {mana_after})"


def _resolve_attack_defender(
    block: Any, game: Game, before: _EntitySnapshot, after: _EntitySnapshot
) -> Entity | None:
    """The attack's actual target entity. Usually `block.target` names it
    directly, but an engine-triggered "attacks a random enemy" effect
    (e.g. Factory Assemblybot's Miniaturize) prints its BLOCK_START with
    Target=0 -- the real target is only conveyed moments later, via a
    PROPOSED_DEFENDER tag change *inside* the block, which hslog's
    `Block.target` is never updated to reflect (it's parsed once, from
    the BLOCK_START line itself, and reset to 0 again before the block
    ends -- verified against a real match's raw log). Falls back to
    whichever other entity actually took damage during this block."""
    defender = game.find_entity_by_id(block.target)
    if defender is not None:
        return defender
    for entity_id, (_zone, _atk, health, _armor, _card_id) in after.items():
        if entity_id == block.entity:
            continue
        if health < before.get(entity_id, _NOT_TRACKED)[2]:
            return game.find_entity_by_id(entity_id)
    return None


def _attack_headline(
    block: Any,
    game: Game,
    namer: _InstanceNamer,
    friendly_player: Player,
    before: _EntitySnapshot,
    after: _EntitySnapshot,
    interrupted: bool,
) -> tuple[str, frozenset[int]]:
    """Returns (headline, entity ids already folded into the headline --
    excluded from the generic effect-line diff to avoid duplicating them).
    `interrupted` (the attacker never actually reached the board again,
    e.g. bounced by a triggered Secret) suppresses the hero-target life
    fold, since no damage was actually dealt to fold in."""
    attacker = game.find_entity_by_id(block.entity)
    defender = _resolve_attack_defender(block, game, before, after)
    player_label = _player_label(attacker.controller if attacker else None, friendly_player)
    if attacker is None or defender is None:
        return f"{player_label}: Angriff", frozenset()

    attacker_name = namer.display_name(attacker, friendly_player)
    defender_name = namer.display_name(defender, friendly_player)
    attacker_attack = before.get(block.entity, _NOT_TRACKED)[1]

    if defender.type != CardType.HERO:
        return f"{player_label}: {attacker_name} → {defender_name}", frozenset()

    attacker_label = f"{attacker_name} ({attacker_attack} Angriff)"
    if interrupted:
        return f"{player_label}: {attacker_label} → {defender_name}", frozenset()
    health_before = before.get(defender.id, _NOT_TRACKED)[2]
    health_after = after.get(defender.id, _NOT_TRACKED)[2]
    headline = (
        f"{player_label}: {attacker_label} → {defender_name}: {health_before} → {health_after}"
    )
    return headline, frozenset({defender.id})


def _target_suffix(
    block: Any, game: Game, namer: _InstanceNamer, friendly_player: Player, before: _EntitySnapshot
) -> str:
    """Names the target as it was *before* this block ran, not what it may
    have become by the time the headline is built (e.g. a Hex target must
    be named by what was actually chosen, not the Frog it ends up as)."""
    if not block.target:
        return ""
    target = game.find_entity_by_id(block.target)
    if target is None:
        return ""
    card_id_before = before.get(block.target, _NOT_TRACKED)[4]
    if target.type == CardType.MINION and card_id_before:
        name = namer.minion_name_for_card_id(block.target, card_id_before)
    else:
        name = namer.display_name(target, friendly_player)
    return f" → Ziel: {name}"


def _build_attack_action(
    block: Any,
    game: Game,
    namer: _InstanceNamer,
    friendly_player: Player,
    before: _EntitySnapshot,
    after: _EntitySnapshot,
    order: list[int] | None = None,
    readiness: _AttackReadiness | None = None,
) -> Action:
    # The attacker ending up back in hand (rather than staying on the
    # board or dying) means the attack never actually connected -- most
    # commonly a triggered Secret (Freezing Trap, ...) bounced it away.
    # Detected generically from the zone change alone, no card-specific
    # knowledge needed. A death mid-attack (e.g. trading into a bigger
    # minion) is a normal combat outcome and goes through the usual path.
    interrupted = after.get(block.entity, _NOT_TRACKED)[0] == Zone.HAND
    headline, folded = _attack_headline(
        block, game, namer, friendly_player, before, after, interrupted
    )
    effects = _diff_effects(
        game,
        namer,
        friendly_player,
        before,
        after,
        exclude=folded,
        order=order,
        readiness=readiness,
    )
    if interrupted:
        effects.append("Angriff abgebrochen")
        defender_before = before.get(block.target, _NOT_TRACKED)
        defender_after = after.get(block.target, _NOT_TRACKED)
        if defender_before[2] == defender_after[2]:
            defender = game.find_entity_by_id(block.target)
            if defender is not None:
                defender_name = namer.display_name(defender, friendly_player)
                effects.append(f"{defender_name} nimmt keinen Kampfschaden")
    return Action(headline=headline, effects=effects)


def _build_action(
    block: Any,
    game: Game,
    namer: _InstanceNamer,
    friendly_player: Player,
    before: _EntitySnapshot,
    after: _EntitySnapshot,
    mana_before: int | None,
    mana_after: int | None,
    order: list[int] | None = None,
    readiness: _AttackReadiness | None = None,
) -> Action:
    if block.type == BlockType.ATTACK:
        return _build_attack_action(
            block, game, namer, friendly_player, before, after, order, readiness=readiness
        )

    entity = game.find_entity_by_id(block.entity)
    controller = entity.controller if entity is not None else None
    player_label = _player_label(controller, friendly_player)

    exclude: frozenset[int] = frozenset()
    if block.type == BlockType.FATIGUE:
        headline = f"{player_label}: Ermüdungsschaden"
    else:
        name = namer.display_name(entity, friendly_player) if entity is not None else "?"
        verb = _BLOCK_VERBS[block.type]
        target_suffix = _target_suffix(block, game, namer, friendly_player, before)
        mana_suffix = _mana_headline_suffix(mana_before, mana_after)
        headline = f"{player_label}: {name} {verb}{target_suffix}{mana_suffix}"
        # The played card's own hand->play transition is already conveyed
        # by "gespielt" -- showing a redundant "beschworen" line for it too
        # would just repeat the headline.
        exclude = frozenset({block.entity})

    effects = _diff_effects(
        game,
        namer,
        friendly_player,
        before,
        after,
        exclude=exclude,
        order=order,
        readiness=readiness,
    )
    return Action(headline=headline, effects=effects)


class _TurnBuilder:
    """Consumes turn-boundary and top-level-block notifications from
    `_SnapshotEntityTreeExporter` while it replays the packet tree, and
    assembles `Turn` objects with pre/post snapshots and an action log.

    Snapshots need real point-in-time state (mana available *before* this
    specific play, board state *after* this specific attack), which the
    final, fully-replayed `Game` object can no longer give us -- so this
    hooks into the same live replay the library already performs, rather
    than re-deriving zone/tag history itself (the project's own zone-replay
    code caused three separate bugs earlier; observing the library's
    trusted, already-mutating entities avoids repeating that).
    """

    def __init__(self, friendly_id: int | None, card_db: Any) -> None:
        self._friendly_id = friendly_id
        self._card_db = card_db
        self._namer = _InstanceNamer(card_db)
        self._game: Game | None = None
        self._me: Player | None = None
        self._opponent: Player | None = None
        self._current: Turn | None = None
        self._active: Player | None = None
        self._pending_turn_number: int | None = None
        # First-touch order of entity ids since the currently open top-level
        # block started (see `on_entity_touched`) -- lets effect lines be
        # shown in the order things actually happened, not incidentally in
        # `_snapshot_entities`' creation-id order. `None` means "not
        # currently inside a tracked block" (a single field, not a separate
        # order-list + tracking-flag pair, so there's nothing to forget to
        # toggle in lockstep) -- an untracked block or the gap between
        # blocks never pollutes the next tracked block's order.
        self._touch_order: list[int] | None = None
        self._touched_ids: set[int] = set()
        # A hero's health (by entity id), frozen the *first* time that hero
        # is seen dead -- not updated again after that, even if a later
        # block in the same turn re-snapshots a since-corrupted HEALTH tag
        # (see `_hero_health`). First-write-wins, since the correct value
        # is only ever true right when the hero actually dies.
        self._frozen_hero_health: dict[int, int] = {}
        # The turn number an entity id first reached Zone.PLAY -- first-
        # write-wins, so a later transform (same entity id, new card via
        # CHANGE_ENTITY) never overwrites it. See `_AttackReadiness`.
        self._entered_play_turn: dict[int, int] = {}
        # entity_id -> how many attacks it has made this turn. Reset to
        # empty at the start of every turn (`on_turn_ready`) -- unlike
        # `_entered_play_turn`, this must NOT persist across turns. See
        # `_AttackReadiness`.
        self._attacks_used_this_turn: dict[int, int] = {}
        self.turns: list[Turn] = []

    def on_entity_entered_play(self, entity_id: int) -> None:
        if self._current is None or entity_id in self._entered_play_turn:
            return
        self._entered_play_turn[entity_id] = self._current.number

    def _readiness(self, turn_number: int) -> _AttackReadiness:
        return _AttackReadiness(
            turn_number, self._entered_play_turn, self._attacks_used_this_turn
        )

    def _players(self, game: Game) -> tuple[Player, Player]:
        if self._me is None:
            self._me, self._opponent = _friendly_and_opponent(game, self._friendly_id)
        assert self._me is not None and self._opponent is not None
        return self._me, self._opponent

    def on_turn_number(self, turn_number: int, game: Game) -> None:
        # `GameTag.TURN` fires right as the *previous* turn ends, before
        # the new turn's own mana grant and draw are applied -- exactly the
        # right moment to close out the previous turn's "end" snapshot.
        # Opening the new turn is deferred to `on_turn_ready` (`STEP`
        # reaching MAIN_ACTION), so *its* start snapshot reflects what that
        # player could actually act on (mana granted, card drawn).
        self._game = game
        if self._current is not None and self._active is not None:
            me, opponent = self._players(game)
            self._current.end = _turn_snapshot(
                me,
                opponent,
                self._active,
                self._card_db,
                self._namer,
                self._readiness(self._current.number),
                frozen=self._frozen_hero_health,
            )
            self.turns.append(self._current)
            self._current = None
        self._pending_turn_number = turn_number

    def on_turn_ready(self, game: Game) -> None:
        if self._pending_turn_number is None:
            return
        turn_number = self._pending_turn_number
        self._pending_turn_number = None
        self._game = game
        me, opponent = self._players(game)
        active = _active_player(game, turn_number)
        assert active is not None
        self._active = active
        # Hearthstone itself resets every entity's attack count at the
        # start of each new turn -- mirrored here since this dict is the
        # authoritative source for `_minion_keywords`, not the live tag.
        self._attacks_used_this_turn = {}
        self._current = Turn(
            number=turn_number,
            player_name=_player_label(active, me),
            opening_draws=[],
            start=_turn_snapshot(
                me,
                opponent,
                active,
                self._card_db,
                self._namer,
                self._readiness(turn_number),
                frozen=self._frozen_hero_health,
            ),
            actions=[],
            end=_turn_snapshot(
                me,
                opponent,
                active,
                self._card_db,
                self._namer,
                self._readiness(turn_number),
                frozen=self._frozen_hero_health,
            ),
        )

    def before_block(self, block: Any, game: Game) -> tuple[_EntitySnapshot, int | None] | None:
        if self._current is None:
            return None
        if block.type not in _ACTION_BLOCK_TYPES and not _is_merge_only_block(block):
            return None
        controller = self._block_controller(block, game)
        mana_before = _mana_state(controller).available if controller is not None else None
        self._touch_order = []
        self._touched_ids = set()
        return _snapshot_entities(game), mana_before

    def on_entity_touched(self, entity_id: int) -> None:
        """Called live, as the replay processes each mutation packet inside
        a tracked top-level block -- records the order entities were first
        touched (TAG_CHANGE, or the entity's own creation/reveal), which is
        the actual causal order, unlike `_snapshot_entities`' before/after
        dict order. `_touched_ids` gives O(1) dedup -- a block touching many
        entities (a board wipe, a cleave) would otherwise make this an O(n)
        scan per touch, O(n^2) for the whole block."""
        if self._touch_order is None or entity_id in self._touched_ids:
            return
        self._touched_ids.add(entity_id)
        self._touch_order.append(entity_id)

    def _freeze_dead_hero_health(
        self, me: Player, opponent: Player, entities: _EntitySnapshot
    ) -> None:
        """Records each hero's health the first time it's seen dead, and
        never again -- see `_hero_health`. Deliberately first-write-wins,
        not "most recent": a later top-level block in the same turn (a
        separate deathrattle trigger, a fatigue tick, ...) can still touch
        the same already-dead hero's entry in `entities`, and by then its
        HEALTH tag may already carry a post-mortem rewrite that has nothing
        to do with the match result."""
        for hero in (me.hero, opponent.hero):
            if hero is None or hero.id in self._frozen_hero_health:
                continue
            snapshot = entities.get(hero.id)
            if snapshot is not None and snapshot[0] == Zone.GRAVEYARD:
                self._frozen_hero_health[hero.id] = snapshot[2]

    def after_block(
        self, block: Any, game: Game, before: tuple[_EntitySnapshot, int | None]
    ) -> None:
        if self._current is None:
            return
        order = self._touch_order
        self._touch_order = None
        me, opponent = self._players(game)
        before_entities, mana_before = before
        after_entities = _snapshot_entities(game)
        self._freeze_dead_hero_health(me, opponent, after_entities)
        if block.type == BlockType.ATTACK:
            # Recorded independently of the live EXHAUSTED/NUM_ATTACKS_
            # THIS_TURN tags for the same reason `_entered_play_turn` is:
            # a same-turn transform (CHANGE_ENTITY) resets both of those
            # unconditionally, which would otherwise make an already-
            # attacked minion look ready to attack again. Incremented, not
            # set to a flag -- a Windfury minion's first attack this turn
            # must not be mistaken for its second. See `_AttackReadiness`.
            self._attacks_used_this_turn[block.entity] = (
                self._attacks_used_this_turn.get(block.entity, 0) + 1
            )
        if _is_merge_only_block(block):
            self._merge_effects_into_last_action(game, me, before_entities, after_entities, order)
            return
        controller = self._block_controller(block, game)
        # `_mana_state` already clamps at 0 -- trust the game's own tags
        # rather than computing a cost ourselves (a card's *actual* cost,
        # discounts included, is exactly what those tags already reflect).
        mana_after = _mana_state(controller).available if controller is not None else None
        action = _build_action(
            block,
            game,
            self._namer,
            me,
            before_entities,
            after_entities,
            mana_before,
            mana_after,
            order,
            readiness=self._readiness(self._current.number),
        )
        self._current.actions.append(action)

    def _merge_effects_into_last_action(
        self,
        game: Game,
        friendly_player: Player,
        before: _EntitySnapshot,
        after: _EntitySnapshot,
        order: list[int] | None = None,
    ) -> None:
        """A death's bookkeeping and its deathrattle's effect belong to
        whichever action caused them, not their own separate, actor-less
        entry."""
        assert self._current is not None
        lines = _diff_effects(
            game,
            self._namer,
            friendly_player,
            before,
            after,
            order=order,
            readiness=self._readiness(self._current.number),
        )
        if not lines:
            return
        if self._current.actions:
            self._current.actions[-1].effects.extend(lines)
        else:
            # No preceding action this turn to attach to (e.g. a delayed
            # deathrattle firing with nothing else having happened yet) --
            # surface it on its own rather than dropping it silently.
            self._current.actions.append(Action(headline="Folgeeffekt", effects=lines))

    @staticmethod
    def _block_controller(block: Any, game: Game) -> Player | None:
        entity = game.find_entity_by_id(block.entity)
        return entity.controller if entity is not None else None

    def flush(self, game: Game) -> None:
        if self._current is None or self._active is None:
            return
        me, opponent = self._players(game)
        self._current.end = _turn_snapshot(
            me,
            opponent,
            self._active,
            self._card_db,
            self._namer,
            self._readiness(self._current.number),
            frozen=self._frozen_hero_health,
        )
        self.turns.append(self._current)
        self._current = None


# Packet types that mutate an existing entity (or bring a new one into
# being) without needing any further reaction here beyond recording the
# touch -- unlike TagChange, which also drives the turn-boundary hooks
# below. Dispatched generically via `export_packet` instead of one
# override per type, since all four would otherwise be identical
# boilerplate (call super(), then touch).
_TOUCH_ONLY_PACKET_TYPES = (
    hslog_packets.FullEntity,
    hslog_packets.ShowEntity,
    hslog_packets.HideEntity,
    hslog_packets.ChangeEntity,
)


class _SnapshotEntityTreeExporter(EntityTreeExporter):
    def __init__(self, packet_tree: Any, player_manager: Any, builder: _TurnBuilder) -> None:
        super().__init__(packet_tree, player_manager=player_manager)
        self._builder = builder
        self._depth = 0

    def _touch(self, packet: Any) -> None:
        self._builder.on_entity_touched(int(coerce_to_entity_id(packet.entity)))

    def export_packet(self, packet: Any) -> Any:
        try:
            result = super().export_packet(packet)
        except TypeError:
            # Verified against a real match: a card whose internal effect id
            # contains an apostrophe (Al'Akir, Lord of Storms' "SpawntoHand"
            # sub-spell, SpellPrefabGUID=CATAFX_Al'Akir_SpawntoHand:...)
            # breaks hslog's own SUB_SPELL_START regex, which then hands a
            # PlayerReference object as a packet's entity id instead of an
            # int -- hslog's handle_full_entity does `int(entity_id)` and
            # raises. This is a malformed *packet* (hslog's own parser lost
            # its place), not a malformed *line* (already handled leniently
            # in `parse_log`'s per-line read) -- skip just this one packet
            # rather than losing the rest of the match.
            return None
        if isinstance(packet, _TOUCH_ONLY_PACKET_TYPES):
            self._touch(packet)
            # A summoned token is often created directly into Zone.PLAY via
            # FullEntity/ShowEntity, with no separate ZONE TAG_CHANGE for
            # `handle_tag_change` to see -- verified against a real match
            # (a Copybot token: "FULL_ENTITY - Creating ... tag=ZONE
            # value=PLAY", never followed by its own TAG_CHANGE). Deliberately
            # excludes ChangeEntity (a transform, CHANGE_ENTITY): that must
            # never be treated as "entering play" -- the entity already has
            # a real entered-play turn (or doesn't yet exist in play at all,
            # which _touch's first-write-wins guard handles fine either way).
            if isinstance(packet, (hslog_packets.FullEntity, hslog_packets.ShowEntity)):
                self._maybe_mark_entered_play(packet)
        return result

    def _maybe_mark_entered_play(self, packet: Any) -> None:
        if self.game is None:
            return
        entity_id = int(coerce_to_entity_id(packet.entity))
        entity = self.game.find_entity_by_id(entity_id)
        if entity is not None and entity.zone == Zone.PLAY:
            self._builder.on_entity_entered_play(entity_id)

    def handle_tag_change(self, packet: Any) -> Any:
        entity = super().handle_tag_change(packet)
        self._touch(packet)
        if packet.tag == GameTag.ZONE and packet.value == Zone.PLAY:
            self._builder.on_entity_entered_play(int(coerce_to_entity_id(packet.entity)))
        if entity is self.game and self.game is not None:
            if packet.tag == GameTag.TURN:
                self._builder.on_turn_number(packet.value, self.game)
            elif packet.tag == GameTag.STEP and packet.value == Step.MAIN_ACTION:
                self._builder.on_turn_ready(self.game)
        return entity

    def handle_block(self, packet: Any) -> None:
        is_top = self._depth == 0
        before = self._builder.before_block(packet, self.game) if is_top and self.game else None
        # Only descend into "nested" territory for a block that was itself
        # tracked (an action, or a deaths/deathrattle merge) -- an
        # *untracked* top-level block (e.g. a card's own end-of-turn
        # TRIGGER, which is neither) has no action of its own to fold
        # anything into, so its children must still get their own chance
        # to be top-level actions. Verified against a real match: Factory
        # Assemblybot's "at end of turn, summon a 6/7 Bot that attacks a
        # random enemy" fires as an untracked top-level TRIGGER wrapping a
        # nested ATTACK block -- with unconditional descent, that ATTACK's
        # very real 6 damage (and the Bot's own appearance) never made it
        # into the action log at all, even though the turn's closing
        # snapshot already reflected it.
        tracked = before is not None
        if tracked:
            self._depth += 1
        try:
            super().handle_block(packet)
        finally:
            if tracked:
                self._depth -= 1
        if is_top and before is not None and self.game is not None:
            self._builder.after_block(packet, self.game, before)

    def flush(self) -> None:
        if self.game is not None:
            self._builder.flush(self.game)


# --- Mulligan ---------------------------------------------------------------


def _collect_choice_packets(
    packet_tree: Any, game: Game
) -> tuple[list[tuple[int, Any]], list[Any]]:
    """Recursively find every `Choices` and `ChosenEntities` packet in the
    tree. Both are registered wherever the parser's "current block" happens
    to be at the time (top level during mulligan, nested inside a PLAY
    block for an in-game Discover), not necessarily nested under a
    `Block`, so this walks the tree directly rather than via the live
    exporter hooks. Each `Choices` packet is paired with the global turn
    number active when it was made (0 during mulligan, before turn 1), so
    a later in-game choice (e.g. Discover) can be attributed to a `Turn`.
    """
    choices: list[tuple[int, Any]] = []
    chosen: list[Any] = []
    current_turn = 0

    def visit(packet: Any) -> None:
        nonlocal current_turn
        if isinstance(packet, hslog_packets.TagChange) and packet.tag == GameTag.TURN:
            entity_id = int(coerce_to_entity_id(packet.entity))
            if game.find_entity_by_id(entity_id) is game:
                current_turn = packet.value
        elif isinstance(packet, hslog_packets.Choices):
            choices.append((current_turn, packet))
        elif isinstance(packet, hslog_packets.ChosenEntities):
            chosen.append(packet)
        for child in getattr(packet, "packets", []):
            visit(child)

    for packet in packet_tree:
        visit(packet)
    return choices, chosen


def _entity_card_id(game: Game, entity_id: int) -> str | None:
    entity = game.find_entity_by_id(entity_id)
    if entity is None or not entity.initial_card_id:
        return None
    return get_original_card_id(entity.initial_card_id)


def _is_friendly_choice(
    choice: Any, game: Game, friendly_player: Player, choice_type: ChoiceType
) -> bool:
    if choice.type != choice_type:
        return False
    entity_id = int(coerce_to_entity_id(choice.entity))
    return game.find_entity_by_id(entity_id) is friendly_player


# The Coin is included in a going-second player's mulligan `Choices.choices`
# offer list (it's already sitting in their opening hand at that point), but
# it was never a real mulligan option -- it must never appear in either
# `kept` or `returned`.
_COIN_CARD_ID = "GAME_005"


def _resolve_card_ids(game: Game, entity_ids: list[int]) -> list[str]:
    return [cid for cid in (_entity_card_id(game, eid) for eid in entity_ids) if cid]


def _resolve_card_ids_excluding_coin(game: Game, entity_ids: list[int]) -> list[str]:
    return [cid for cid in _resolve_card_ids(game, entity_ids) if cid != _COIN_CARD_ID]


def _resolve_current_names(game: Game, card_db: Any, entity_ids: list[int]) -> list[str]:
    names = []
    for entity_id in entity_ids:
        entity = game.find_entity_by_id(entity_id)
        names.append(_card_name(entity, card_db) if entity is not None else "?")
    return names


def _extract_mulligan(
    packet_tree: Any, game: Game, friendly_player: Player
) -> MulliganChoice | None:
    """Find the friendly player's mulligan: the `Choices` packet of type
    MULLIGAN whose player is `friendly_player` gives the offered hand; the
    `ChosenEntities`/`SendChoices` packet sharing its choice id gives the
    cards the player actually confirmed keeping -- verified against a real
    match (commit history): the entity in `chosen` ended the match having
    been played from the opening hand (proven by reaching Zone.GRAVEYARD
    with no earlier draw), while an *unchosen* offered entity was never
    drawn again all match (proven by still sitting in Zone.DECK at the
    end) -- i.e. `chosen` is "kept", not "sent back", the opposite of what
    the mulligan UI's card-clicking gesture might suggest."""
    choices, chosen = _collect_choice_packets(packet_tree, game)
    mulligan_choice = next(
        (
            c
            for _turn, c in choices
            if _is_friendly_choice(c, game, friendly_player, ChoiceType.MULLIGAN)
        ),
        None,
    )
    if mulligan_choice is None:
        return None

    chosen_entities = next((c for c in chosen if c.id == mulligan_choice.id), None)
    if chosen_entities is None:
        # We saw the mulligan *offer* (`Choices`) but never its confirmation
        # (`ChosenEntities`) -- e.g. a log truncated between the two. We
        # genuinely don't know what was kept vs. returned; treating that as
        # "everything was returned" (the old behavior of defaulting
        # `kept_ids` to `[]`) would present a confident-looking claim built
        # on missing data as if it were an observed fact. Same "absence, not
        # an empty mulligan" contract as returning None when no `Choices`
        # packet was found at all.
        return None
    kept_ids = chosen_entities.choices
    returned_ids = [entity_id for entity_id in mulligan_choice.choices if entity_id not in kept_ids]

    kept = _resolve_card_ids_excluding_coin(game, kept_ids)
    returned = _resolve_card_ids_excluding_coin(game, returned_ids)
    return MulliganChoice(kept=kept, returned=returned)


def _extract_discoveries(
    packet_tree: Any, game: Game, card_db: Any, friendly_player: Player
) -> list[tuple[int, Action]]:
    """Every Discover-style choice (`ChoiceType.GENERAL`) the friendly
    player made, as (turn_number, Action) pairs ready to attach to the
    matching `Turn`. Opponent discoveries are not tracked -- what was
    offered to them is never visible to us, and showing only their final
    pick without the alternatives they had would misrepresent what they
    "should" have done differently."""
    choices, chosen = _collect_choice_packets(packet_tree, game)
    picks = []
    for turn_number, choice in choices:
        if not _is_friendly_choice(choice, game, friendly_player, ChoiceType.GENERAL):
            continue
        chosen_entities = next((c for c in chosen if c.id == choice.id), None)
        if chosen_entities is None:
            continue
        offered = _resolve_current_names(game, card_db, choice.choices)
        picked = _resolve_current_names(game, card_db, chosen_entities.choices)
        action = Action(
            headline="Du: Discover",
            effects=[f"Angeboten: {', '.join(offered)}", f"Gewählt: {', '.join(picked)}"],
        )
        picks.append((turn_number, action))
    return picks


def _insert_discover_actions(turns: list[Turn], picks: list[tuple[int, Action]]) -> None:
    by_number = {turn.number: turn for turn in turns}
    for turn_number, action in picks:
        turn = by_number.get(turn_number)
        if turn is not None:
            turn.actions.append(action)


def _deck_status(me: Player) -> tuple[list[str], list[str]]:
    """Returns (remaining_card_ids, not_in_deck_card_ids) for the friendly
    player's starting deck, based on each card's final zone at the end of
    the match (not a history replay -- a card that was mulliganed away and
    never redrawn is correctly still "remaining", since it ends the match
    back in Zone.DECK).
    """
    remaining: list[str] = []
    not_in_deck: list[str] = []
    for entity in me.initial_deck:
        card_id = get_original_card_id(entity.initial_card_id) if entity.initial_card_id else None
        if not card_id:
            continue
        if entity.zone == Zone.DECK:
            remaining.append(card_id)
        else:
            not_in_deck.append(card_id)
    return remaining, not_in_deck


def _opening_draw_lines(previous_hand: HandState, current_hand: HandState) -> list[str]:
    """Cards newly present in `current_hand` compared to `previous_hand`.

    Nothing else happens between the end of one turn and the start of the
    next besides that turn's own draw, so a hand-size/contents delta
    between two adjacent snapshots is, in practice, exactly what was drawn
    -- friendly draws by name (always known), opponent draws as a count
    (their identity is hidden information until played or revealed).
    Rendered above `### Start`, not as an "action": `current_hand` (i.e.
    `start.hand`) already reflects the post-draw hand, so the draw is
    already-known context for that turn's decisions, not one of them."""
    lines = []
    newly_drawn = Counter(c.name for c in current_hand.own_cards) - Counter(
        c.name for c in previous_hand.own_cards
    )
    for card_name in sorted(newly_drawn.elements()):
        lines.append(f"{card_name} gezogen")
    opponent_drawn = current_hand.opponent_count - previous_hand.opponent_count
    for _ in range(max(0, opponent_drawn)):
        lines.append("Gegner zieht eine Karte")
    return lines


def _insert_opening_draws(turns: list[Turn]) -> None:
    for previous, current in zip(turns, turns[1:], strict=False):
        current.opening_draws = _opening_draw_lines(previous.end.hand, current.start.hand)


_LOG_TRUNCATION_MARKER = "Truncating log"
# The literal marker Hearthstone writes at the start of every match, at the
# top (non-indented) `GameState` level -- as opposed to the very same text
# appearing again, indented, a little further down as part of a redundant
# `PowerTaskList` echo of the same block. Matching the exact, non-indented
# form is what makes this a reliable one-match-starts-here boundary.
_CREATE_GAME_MARKER = "GameState.DebugPrintPower() - CREATE_GAME"


def _read_log_leniently(parser: LogParser, lines: list[str]) -> bool:
    """Feed `lines` to `parser` one at a time, skipping any single line
    hslog can't parse instead of aborting the whole read. Returns whether
    Hearthstone's own log-truncation banner was found (see `ParsedGame.
    log_truncated`).

    Real-world observed cause of the skip (a genuine hslog gap, not a
    Hearthstone log corruption): once Power.log hits its 10MB size limit,
    Hearthstone itself writes a non-log "Truncating log..." banner straight
    into the file, which hslog's tokenizer was never built to recognize --
    and `LogParser.read()`'s plain `for line in fp: self.read_line(line)`
    loop has no tolerance for a single bad line, aborting the entire match
    (including everything already read) over it. For a live tracker,
    losing a bit of precision from one skipped line is far better than
    losing all tracking for the rest of a long session.
    """
    truncated = False
    for line in lines:
        if _LOG_TRUNCATION_MARKER in line:
            truncated = True
        try:
            parser.read_line(line)
        except ParsingError:
            continue
    return truncated


def _split_last_game(path: Path) -> tuple[int, list[str]]:
    """Splits a whole session log -- which can hold several matches back to
    back, see `ParsedGame.game_index` -- into "how many matches does it
    hold" and "the lines of just the latest one".

    Only the latest match's lines are ever fed to a `LogParser`: hslog's
    `PlayerManager` is a single registry shared across everything it reads,
    keyed by account name, and it hard-errors (`InconsistentPlayerIdError`)
    the moment the *same* account is assigned a different `player_id` than
    it had in an earlier match -- which is completely ordinary (who goes
    first is decided fresh every match), not a real inconsistency. Verified
    against a real session log where it happened: two matches in, the same
    account went from player_id 2 to player_id 1 and the exact same
    session-wide parse that had worked for the first two matches then
    raised on the third. Feeding the parser only one match's lines at a
    time -- a fresh `PlayerManager` each call -- sidesteps this entirely.

    Streams the file rather than materializing it whole (constant memory
    -- `f.tell()` between `readline()` calls, not `for line in f`, which
    disables `tell()` mid-iteration), then a second, targeted read of only
    the requested match's lines, not the whole session. A long session log
    can hold many finished matches by the time this runs on every ~2s poll
    of the current one; only the requested match's line count should scale
    that cost, not the whole session's.
    """
    offsets = _find_game_offsets(path)
    if not offsets:
        return 0, []
    return len(offsets), _read_lines_between(path, offsets[-1], None)


def _find_game_offsets(path: Path) -> list[int]:
    """Byte offsets where each CREATE_GAME marker line starts, in file
    order -- shared by `_split_last_game` (the newest match) and
    `parse_log_at_index` (an arbitrary past one, e.g. reopening a match
    from history in Replay after later matches have already been played
    in the same session log)."""
    offsets: list[int] = []
    with path.open() as f:
        offset = f.tell()
        line = f.readline()
        while line:
            if _CREATE_GAME_MARKER in line:
                offsets.append(offset)
            offset = f.tell()
            line = f.readline()
    return offsets


def _read_lines_between(path: Path, start: int, end: int | None) -> list[str]:
    # Deliberately not `f.read(end - start)`: `tell()` on a text-mode file
    # returns an opaque cookie, not a byte count -- subtracting two of them
    # and passing that as a *character* count to `read()` silently
    # misaligns (and can cut a line mid-way) the moment the file has any
    # multi-byte UTF-8 content, which German card/player names always do.
    # Comparing `tell()` against `end` (itself a `tell()` value, from
    # `_find_game_offsets`) is the only arithmetic-free, safe use of it.
    lines: list[str] = []
    with path.open() as f:
        f.seek(start)
        line = f.readline()
        while line:
            lines.append(line)
            if end is not None and f.tell() >= end:
                break
            line = f.readline()
    return lines


def _parse_lines(lines: list[str], game_index: int, source: str) -> ParsedGame:
    parser = LogParser()
    log_truncated = _read_log_leniently(parser, lines)

    if not parser.games:
        raise NoGameFoundError(f"No CREATE_GAME found in log: {source}")
    packet_tree = parser.games[-1]
    friendly_id = FriendlyPlayerExporter(packet_tree).export()
    card_db, _ = load_cards(locale="deDE")

    builder = _TurnBuilder(friendly_id, card_db)
    exporter = _SnapshotEntityTreeExporter(packet_tree, parser.player_manager, builder)
    exporter.export()
    game = exporter.game
    assert game is not None

    me, opponent = _friendly_and_opponent(game, friendly_id)
    _remaining, not_in_deck = _deck_status(me)
    mulligan = _extract_mulligan(packet_tree, game, me)
    _insert_opening_draws(builder.turns)
    _insert_discover_actions(builder.turns, _extract_discoveries(packet_tree, game, card_db, me))

    return ParsedGame(
        own_class=_class_name(me, card_db),
        opponent_class=_class_name(opponent, card_db),
        starting_deck=me.known_starting_deck_list,
        result=_result_for(me),
        drawn_card_ids=not_in_deck,
        game_index=game_index,
        mulligan=mulligan,
        turns=builder.turns,
        log_truncated=log_truncated,
    )


def parse_log(path: Path) -> ParsedGame:
    game_count, lines = _split_last_game(path)
    if game_count == 0:
        raise NoGameFoundError(f"No CREATE_GAME found in log: {path}")
    return _parse_lines(lines, game_index=game_count, source=str(path))


def parse_log_at_index(path: Path, game_index: int) -> ParsedGame:
    """Like `parse_log`, but for a specific match within a multi-match
    session log (1-based, matching `ParsedGame.game_index`) instead of
    always the newest -- used to reopen a *past* match for Replay from a
    Verlauf entry, once later matches have already been played in the
    same session log and `parse_log` would only ever reach the newest of
    them."""
    offsets = _find_game_offsets(path)
    if game_index < 1 or game_index > len(offsets):
        raise NoGameFoundError(
            f"Match #{game_index} not found in log: {path} (has {len(offsets)} matches)"
        )
    start = offsets[game_index - 1]
    end = offsets[game_index] if game_index < len(offsets) else None
    lines = _read_lines_between(path, start, end)
    return _parse_lines(lines, game_index=game_index, source=f"{path} (match #{game_index})")
