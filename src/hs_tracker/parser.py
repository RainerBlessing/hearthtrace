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

# (GameTag, German label) for the minion keywords worth surfacing in a board
# snapshot. Not exhaustive -- only the ones that commonly change a decision
# (per the user's own guidance: "nicht jeder interne Hearthstone-State muss
# exportiert werden").
_KEYWORD_TAGS: list[tuple[GameTag, str]] = [
    (GameTag.TAUNT, "Spott"),
    (GameTag.DIVINE_SHIELD, "Göttlicher Schild"),
    (GameTag.FROZEN, "Eingefroren"),
    (GameTag.STEALTH, "Getarnt"),
    (GameTag.WINDFURY, "Windfury"),
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
    overload_pending: int  # locks *next* turn; already-locked mana is baked into `available`


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


@dataclass
class HandState:
    # Card names for the friendly player's own hand (always fully known),
    # and just a count for the opponent's -- their identities are hidden
    # information the player did not have at this point in the match.
    own_cards: list[str]
    opponent_count: int


@dataclass
class BoardState:
    own: list[MinionState]
    opponent: list[MinionState]


@dataclass
class TurnSnapshot:
    mana: ManaState
    life: LifeState
    hand: HandState
    board: BoardState


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
      recalculating) are captured too, best-effort -- but the "gezogene
      Karten" action lines and Discover choices are not tracked as separate
      packet-level events; draws are inferred from the hand snapshot delta
      between two turns, and Discover offers/picks are not shown at all
      (known follow-up).
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
    # found (e.g. an incomplete/truncated log) -- absence, not an empty
    # mulligan, since every real match has one.
    mulligan: MulliganChoice | None = None
    turns: list[Turn] = field(default_factory=list)


def _class_name(player: Player, card_db: Any) -> str:
    hero = player.hero
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


# --- Turn snapshots (mana / life / hand / board) --------------------------


def _mana_state(player: Player) -> ManaState:
    resources = player.tags.get(GameTag.RESOURCES, 0)
    used = player.tags.get(GameTag.RESOURCES_USED, 0)
    locked = player.tags.get(GameTag.OVERLOAD_LOCKED, 0)
    overload_pending = player.tags.get(GameTag.OVERLOAD, 0)
    return ManaState(
        available=resources - used - locked, maximum=resources, overload_pending=overload_pending
    )


def _life_state(me: Player, opponent: Player) -> LifeState:
    my_hero, their_hero = me.hero, opponent.hero
    assert my_hero is not None and their_hero is not None
    return LifeState(
        own_health=my_hero.tags.get(GameTag.HEALTH, 0) - my_hero.tags.get(GameTag.DAMAGE, 0),
        own_armor=my_hero.tags.get(GameTag.ARMOR, 0),
        opponent_health=their_hero.tags.get(GameTag.HEALTH, 0)
        - their_hero.tags.get(GameTag.DAMAGE, 0),
        opponent_armor=their_hero.tags.get(GameTag.ARMOR, 0),
    )


def _card_name(entity: Entity, card_db: Any) -> str:
    card = card_db.get(entity.card_id) if entity.card_id else None
    return str(card.name) if card else (entity.card_id or "Unbekannte Karte")


def _hand_state(me: Player, opponent: Player, card_db: Any) -> HandState:
    own = sorted(me.in_zone(Zone.HAND), key=lambda e: e.tags.get(GameTag.ZONE_POSITION, 0))
    return HandState(
        own_cards=[_card_name(entity, card_db) for entity in own],
        opponent_count=sum(1 for _ in opponent.in_zone(Zone.HAND)),
    )


def _minion_keywords(entity: Entity) -> list[str]:
    keywords = [label for tag, label in _KEYWORD_TAGS if entity.tags.get(tag, 0)]
    attack = entity.tags.get(GameTag.ATK, 0)
    exhausted = entity.tags.get(GameTag.EXHAUSTED, 0)
    frozen = entity.tags.get(GameTag.FROZEN, 0)
    if attack > 0 and not exhausted and not frozen:
        keywords.append("kann angreifen")
    return keywords


def _minion_state(entity: Entity, card_db: Any) -> MinionState:
    health = entity.tags.get(GameTag.HEALTH, 0) - entity.tags.get(GameTag.DAMAGE, 0)
    return MinionState(
        name=_card_name(entity, card_db),
        attack=entity.tags.get(GameTag.ATK, 0),
        health=health,
        keywords=_minion_keywords(entity),
    )


def _board_of(player: Player, card_db: Any) -> list[MinionState]:
    minions = [e for e in player.in_zone(Zone.PLAY) if e.type == CardType.MINION]
    minions.sort(key=lambda e: e.tags.get(GameTag.ZONE_POSITION, 0))
    return [_minion_state(entity, card_db) for entity in minions]


def _board_state(me: Player, opponent: Player, card_db: Any) -> BoardState:
    return BoardState(own=_board_of(me, card_db), opponent=_board_of(opponent, card_db))


def _turn_snapshot(me: Player, opponent: Player, active: Player, card_db: Any) -> TurnSnapshot:
    # Life/hand/board are always shown from the friendly player's own point
    # of view (Du/Gegner); mana is shown for whoever's turn it is, since
    # that's the resource that turn's actions are actually spent from.
    return TurnSnapshot(
        mana=_mana_state(active),
        life=_life_state(me, opponent),
        hand=_hand_state(me, opponent, card_db),
        board=_board_state(me, opponent, card_db),
    )


# --- Action log (before/after state diffing) -------------------------------

# entity_id -> (zone, attack, effective_health, armor), for every entity
# that carries a HEALTH tag (heroes and minions -- not spells/weapons/
# enchantments), captured just before and just after a top-level block runs.
_EntitySnapshot = dict[int, tuple[Zone, int, int, int]]


def _snapshot_entities(game: Game) -> _EntitySnapshot:
    snapshot: _EntitySnapshot = {}
    for entity in game.entities:
        if entity is game:
            continue
        health = entity.tags.get(GameTag.HEALTH)
        if health is None:
            continue
        damage = entity.tags.get(GameTag.DAMAGE, 0)
        attack = entity.tags.get(GameTag.ATK, 0)
        armor = entity.tags.get(GameTag.ARMOR, 0)
        snapshot[entity.id] = (entity.zone, attack, health - damage, armor)
    return snapshot


def _display_name(entity: Entity, card_db: Any, friendly_player: Player) -> str:
    if entity.type == CardType.HERO:
        return "Dein Held" if entity.controller is friendly_player else "Gegnerischer Held"
    return _card_name(entity, card_db)


def _entity_diff_lines(
    is_hero: bool,
    name: str,
    previous: tuple[Zone, int, int, int],
    current: tuple[Zone, int, int, int],
) -> list[str]:
    zone_before, attack_before, health_before, armor_before = previous
    zone_after, attack_after, health_after, armor_after = current
    if zone_before == Zone.PLAY and zone_after == Zone.GRAVEYARD:
        return [f"{name} stirbt"]

    lines = []
    if health_after != health_before or (not is_hero and attack_after != attack_before):
        if is_hero:
            lines.append(f"{name}: {health_before} → {health_after}")
        else:
            lines.append(f"{name}: {attack_before}/{health_before} → {attack_after}/{health_after}")
    if armor_after != armor_before:
        lines.append(f"{name}: Rüstung {armor_before} → {armor_after}")
    return lines


def _diff_effects(
    game: Game,
    card_db: Any,
    friendly_player: Player,
    before: _EntitySnapshot,
    after: _EntitySnapshot,
    exclude: frozenset[int] = frozenset(),
) -> list[str]:
    lines = []
    for entity_id, current in after.items():
        if entity_id in exclude:
            continue
        entity = game.find_entity_by_id(entity_id)
        if entity is None:
            continue
        name = _display_name(entity, card_db, friendly_player)
        previous = before.get(entity_id)
        if previous is None:
            if current[0] == Zone.PLAY:
                lines.append(f"{name} beschworen")
            continue
        lines += _entity_diff_lines(entity.type == CardType.HERO, name, previous, current)
    return lines


_BLOCK_VERBS = {
    BlockType.PLAY: "gespielt",
    BlockType.POWER: "eingesetzt",
}


def _mana_headline_suffix(mana_before: int | None, mana_after: int | None) -> str:
    if mana_before is None or mana_after is None or mana_before == mana_after:
        return ""
    return f" (Mana: {mana_before} → {mana_after})"


def _attack_headline(
    block: Any, game: Game, card_db: Any, friendly_player: Player, before: _EntitySnapshot
) -> tuple[str, frozenset[int]]:
    """Returns (headline, entity ids already folded into the headline --
    excluded from the generic effect-line diff to avoid duplicating them)."""
    attacker = game.find_entity_by_id(block.entity)
    defender = game.find_entity_by_id(block.target)
    player_label = _player_label(attacker.controller if attacker else None, friendly_player)
    if attacker is None or defender is None:
        return f"{player_label}: Angriff", frozenset()

    attacker_name = _display_name(attacker, card_db, friendly_player)
    defender_name = _display_name(defender, card_db, friendly_player)
    attacker_attack = before.get(block.entity, (Zone.INVALID, 0, 0, 0))[1]

    if defender.type == CardType.HERO:
        health_before = before.get(block.target, (Zone.INVALID, 0, 0, 0))[2]
        headline = (
            f"{player_label}: {attacker_name} ({attacker_attack} Angriff) → "
            f"{defender_name}: {health_before} → "
        )
        return headline, frozenset({block.target})

    return f"{player_label}: {attacker_name} → {defender_name}", frozenset()


def _build_action(
    block: Any,
    game: Game,
    card_db: Any,
    friendly_player: Player,
    before: _EntitySnapshot,
    after: _EntitySnapshot,
    mana_before: int | None,
    mana_after: int | None,
) -> Action:
    if block.type == BlockType.ATTACK:
        headline, folded = _attack_headline(block, game, card_db, friendly_player, before)
        if headline.endswith("→ "):
            # Hero-target attack: fold the life change straight into the
            # headline (per spec, no separate result line for this case).
            health_after = after.get(block.target, (Zone.INVALID, 0, 0, 0))[2]
            headline += str(health_after)
        effects = _diff_effects(game, card_db, friendly_player, before, after, exclude=folded)
        return Action(headline=headline, effects=effects)

    entity = game.find_entity_by_id(block.entity)
    controller = entity.controller if entity is not None else None
    player_label = _player_label(controller, friendly_player)

    if block.type == BlockType.FATIGUE:
        headline = f"{player_label}: Ermüdungsschaden"
    else:
        name = _display_name(entity, card_db, friendly_player) if entity is not None else "?"
        verb = _BLOCK_VERBS[block.type]
        headline = f"{player_label}: {name} {verb}{_mana_headline_suffix(mana_before, mana_after)}"

    effects = _diff_effects(game, card_db, friendly_player, before, after)
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
        self._game: Game | None = None
        self._me: Player | None = None
        self._opponent: Player | None = None
        self._current: Turn | None = None
        self._active: Player | None = None
        self._pending_turn_number: int | None = None
        self.turns: list[Turn] = []

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
            self._current.end = _turn_snapshot(me, opponent, self._active, self._card_db)
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
        first = game.first_player
        others = [p for p in game.players if p is not first]
        active = first if turn_number % 2 == 1 else (others[0] if others else first)
        assert active is not None
        self._active = active
        self._current = Turn(
            number=turn_number,
            player_name=_player_label(active, me),
            start=_turn_snapshot(me, opponent, active, self._card_db),
            actions=[],
            end=_turn_snapshot(me, opponent, active, self._card_db),
        )

    def before_block(self, block: Any, game: Game) -> tuple[_EntitySnapshot, int | None] | None:
        if block.type not in _ACTION_BLOCK_TYPES or self._current is None:
            return None
        controller = self._block_controller(block, game)
        mana_before = _mana_state(controller).available if controller is not None else None
        return _snapshot_entities(game), mana_before

    def after_block(
        self, block: Any, game: Game, before: tuple[_EntitySnapshot, int | None]
    ) -> None:
        if self._current is None:
            return
        me, _opponent = self._players(game)
        before_entities, mana_before = before
        after_entities = _snapshot_entities(game)
        controller = self._block_controller(block, game)
        mana_after = _mana_state(controller).available if controller is not None else None
        action = _build_action(
            block, game, self._card_db, me, before_entities, after_entities, mana_before, mana_after
        )
        self._current.actions.append(action)

    @staticmethod
    def _block_controller(block: Any, game: Game) -> Player | None:
        entity = game.find_entity_by_id(block.entity)
        return entity.controller if entity is not None else None

    def flush(self, game: Game) -> None:
        if self._current is None or self._active is None:
            return
        me, opponent = self._players(game)
        self._current.end = _turn_snapshot(me, opponent, self._active, self._card_db)
        self.turns.append(self._current)
        self._current = None


class _SnapshotEntityTreeExporter(EntityTreeExporter):
    def __init__(self, packet_tree: Any, player_manager: Any, builder: _TurnBuilder) -> None:
        super().__init__(packet_tree, player_manager=player_manager)
        self._builder = builder
        self._depth = 0

    def handle_tag_change(self, packet: Any) -> Any:
        entity = super().handle_tag_change(packet)
        if entity is self.game and self.game is not None:
            if packet.tag == GameTag.TURN:
                self._builder.on_turn_number(packet.value, self.game)
            elif packet.tag == GameTag.STEP and packet.value == Step.MAIN_ACTION:
                self._builder.on_turn_ready(self.game)
        return entity

    def handle_block(self, packet: Any) -> None:
        is_top = self._depth == 0
        before = self._builder.before_block(packet, self.game) if is_top and self.game else None
        self._depth += 1
        try:
            super().handle_block(packet)
        finally:
            self._depth -= 1
        if is_top and before is not None and self.game is not None:
            self._builder.after_block(packet, self.game, before)

    def flush(self) -> None:
        if self.game is not None:
            self._builder.flush(self.game)


# --- Mulligan ---------------------------------------------------------------


def _collect_choice_packets(packet_tree: Any) -> tuple[list[Any], list[Any]]:
    """Recursively find every `Choices` and `ChosenEntities` packet in the
    tree. Both are registered wherever the parser's "current block" happens
    to be at the time (top level during mulligan), not necessarily nested
    under a `Block`, so this walks the tree directly rather than via the
    live exporter hooks."""
    choices: list[Any] = []
    chosen: list[Any] = []

    def visit(packet: Any) -> None:
        if isinstance(packet, hslog_packets.Choices):
            choices.append(packet)
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


def _is_friendly_mulligan_choice(choice: Any, game: Game, friendly_player: Player) -> bool:
    if choice.type != ChoiceType.MULLIGAN:
        return False
    entity_id = int(coerce_to_entity_id(choice.entity))
    return game.find_entity_by_id(entity_id) is friendly_player


def _resolve_card_ids(game: Game, entity_ids: list[int]) -> list[str]:
    return [cid for cid in (_entity_card_id(game, eid) for eid in entity_ids) if cid]


def _extract_mulligan(
    packet_tree: Any, game: Game, friendly_player: Player
) -> MulliganChoice | None:
    """Find the friendly player's mulligan: the `Choices` packet of type
    MULLIGAN whose player is `friendly_player` gives the offered hand; the
    `ChosenEntities` packet sharing its choice id gives the cards actually
    sent back (Hearthstone's mulligan choice is "which cards to replace",
    not "which to keep")."""
    choices, chosen = _collect_choice_packets(packet_tree)
    mulligan_choice = next(
        (c for c in choices if _is_friendly_mulligan_choice(c, game, friendly_player)), None
    )
    if mulligan_choice is None:
        return None

    chosen_entities = next((c for c in chosen if c.id == mulligan_choice.id), None)
    returned_ids = chosen_entities.choices if chosen_entities else []
    kept_ids = [entity_id for entity_id in mulligan_choice.choices if entity_id not in returned_ids]

    kept = _resolve_card_ids(game, kept_ids)
    returned = _resolve_card_ids(game, returned_ids)
    return MulliganChoice(kept=kept, returned=returned)


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


def _draw_actions(previous_hand: HandState, current_hand: HandState) -> list[Action]:
    """Cards newly present in `current_hand` compared to `previous_hand`.

    Nothing else happens between the end of one turn and the start of the
    next besides that turn's own draw, so a hand-size/contents delta
    between two adjacent snapshots is, in practice, exactly what was drawn
    -- friendly draws by name (always known), opponent draws as a count
    (their identity is hidden information until played or revealed)."""
    actions = []
    newly_drawn = Counter(current_hand.own_cards) - Counter(previous_hand.own_cards)
    for card_name in sorted(newly_drawn.elements()):
        actions.append(Action(headline=f"Du: gezogen — {card_name}"))
    opponent_drawn = current_hand.opponent_count - previous_hand.opponent_count
    for _ in range(max(0, opponent_drawn)):
        actions.append(Action(headline="Gegner: zieht eine Karte"))
    return actions


def _insert_draw_actions(turns: list[Turn]) -> None:
    for previous, current in zip(turns, turns[1:], strict=False):
        current.actions = _draw_actions(previous.end.hand, current.start.hand) + current.actions


def parse_log(path: Path) -> ParsedGame:
    parser = LogParser()
    with path.open() as f:
        parser.read(f)

    if not parser.games:
        raise NoGameFoundError(f"No CREATE_GAME found in log: {path}")
    packet_tree = parser.games[-1]
    friendly_id = FriendlyPlayerExporter(packet_tree).export()
    card_db, _ = load_cards()

    builder = _TurnBuilder(friendly_id, card_db)
    exporter = _SnapshotEntityTreeExporter(packet_tree, parser.player_manager, builder)
    exporter.export()
    game = exporter.game
    assert game is not None

    me, opponent = _friendly_and_opponent(game, friendly_id)
    _remaining, not_in_deck = _deck_status(me)
    mulligan = _extract_mulligan(packet_tree, game, me)
    _insert_draw_actions(builder.turns)

    return ParsedGame(
        own_class=_class_name(me, card_db),
        opponent_class=_class_name(opponent, card_db),
        starting_deck=me.known_starting_deck_list,
        result=_result_for(me),
        drawn_card_ids=not_in_deck,
        game_index=len(parser.games),
        mulligan=mulligan,
        turns=builder.turns,
    )
