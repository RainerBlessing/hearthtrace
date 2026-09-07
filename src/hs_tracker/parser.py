"""Parse a Hearthstone Power.log into a small, stable snapshot.

This module isolates the rest of the app from the `hslog`/`hearthstone`
package internals: callers only ever see `ParsedGame`.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hearthstone.cardxml import load as load_cards
from hearthstone.entities import Game, Player
from hearthstone.enums import BlockType, ChoiceType, GameTag, PlayState, Zone
from hearthstone.utils import get_original_card_id
from hslog import LogParser
from hslog import packets as hslog_packets
from hslog.export import EntityTreeExporter, FriendlyPlayerExporter
from hslog.player import coerce_to_entity_id


class NoGameFoundError(Exception):
    """Raised when a Power.log contains no CREATE_GAME block.

    This happens for a log captured before any match started, or a
    truncated/corrupted capture -- there is simply no game data to parse.
    """


@dataclass
class PlayEvent:
    turn: int
    player_name: str
    card_name: str


@dataclass
class MulliganChoice:
    # Card ids the friendly player kept in their opening hand, and card ids
    # they sent back to the deck, both in the order Hearthstone reports
    # them. Opponent mulligans are not tracked (their cards are hidden).
    kept: list[str]
    returned: list[str]


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
    - `turn_log` only records top-level `BlockType.PLAY` blocks (cards
      played directly from hand). It does not include hero power
      activations (`BlockType.POWER`), nor plays nested inside another
      block (e.g. a battlecry/discover effect that plays a card
      automatically) — those are attributed to the outer effect, not
      recorded as a separate play.
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
    turn_log: list[PlayEvent]
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


def _class_name(player: Player, card_db: Any) -> str:
    hero = player.hero
    if hero is None or not hero.card_id:
        return "UNKNOWN"
    return str(card_db[hero.card_id].card_class.name)


def _friendly_and_opponent(game: Game, packet_tree: Any) -> tuple[Player, Player]:
    friendly_id = FriendlyPlayerExporter(packet_tree).export()
    me = game.get_player(friendly_id) if friendly_id else game.players[0]
    assert me is not None
    opponent = next(p for p in game.players if p is not me)
    return me, opponent


def _result_for(player: Player) -> str:
    playstate = player.tags.get(GameTag.PLAYSTATE, PlayState.INVALID)
    return PlayState(playstate).name if playstate else "UNKNOWN"


class _TurnLogWalker:
    """Walks a packet tree tracking whose turn it is, collecting a
    `PlayEvent` for every top-level `BlockType.PLAY` block.

    Hearthstone numbers turns globally (turn 1 = the first player's
    opening turn, turn 2 = the second player's opening turn, ...) and only
    the GameEntity's own TAG_CHANGE TURN reflects that global count -- each
    player also gets their own per-player TURN tag (their Nth own turn),
    which would misattribute turn numbers if not filtered out.
    """

    def __init__(self, game: Game, card_db: Any, friendly_player: Player) -> None:
        self._game = game
        self._card_db = card_db
        self._friendly_player = friendly_player
        self._current_turn = 0
        self.events: list[PlayEvent] = []

    def walk(self, packet_tree: Any) -> None:
        for packet in packet_tree:
            self._visit(packet)

    def _visit(self, packet: Any) -> None:
        if isinstance(packet, hslog_packets.TagChange):
            self._handle_tag_change(packet)
        elif isinstance(packet, hslog_packets.Block):
            self._handle_block(packet)
        for child in getattr(packet, "packets", []):
            self._visit(child)

    def _handle_tag_change(self, packet: Any) -> None:
        if packet.tag == GameTag.TURN:
            self._handle_turn_change(packet)

    def _handle_turn_change(self, packet: Any) -> None:
        entity_id = int(coerce_to_entity_id(packet.entity))
        if self._game.find_entity_by_id(entity_id) is self._game:
            self._current_turn = packet.value

    def _handle_block(self, packet: Any) -> None:
        if packet.type != BlockType.PLAY:
            return
        entity_id = int(coerce_to_entity_id(packet.entity))
        entity = self._game.find_entity_by_id(entity_id)
        if entity is None or not entity.card_id:
            return
        controller = entity.controller
        player_name = "Du" if controller is self._friendly_player else "Gegner"
        card = self._card_db.get(entity.card_id)
        card_name = card.name if card else entity.card_id
        self.events.append(
            PlayEvent(turn=self._current_turn, player_name=player_name, card_name=card_name)
        )


def _extract_turn_log(
    packet_tree: Any, game: Game, card_db: Any, friendly_player: Player
) -> list[PlayEvent]:
    walker = _TurnLogWalker(game, card_db, friendly_player)
    walker.walk(packet_tree)
    return walker.events


def _collect_choice_packets(packet_tree: Any) -> tuple[list[Any], list[Any]]:
    """Recursively find every `Choices` and `ChosenEntities` packet in the
    tree. Both are registered wherever the parser's "current block" happens
    to be at the time (top level during mulligan), not necessarily nested
    under a `Block`, so this walks the same way `_TurnLogWalker` does."""
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


def parse_log(path: Path) -> ParsedGame:
    parser = LogParser()
    with path.open() as f:
        parser.read(f)

    if not parser.games:
        raise NoGameFoundError(f"No CREATE_GAME found in log: {path}")
    packet_tree = parser.games[-1]
    exporter = EntityTreeExporter(packet_tree, player_manager=parser.player_manager)
    exporter.export()
    game = exporter.game
    assert game is not None

    me, opponent = _friendly_and_opponent(game, packet_tree)
    card_db, _ = load_cards()
    turn_log = _extract_turn_log(packet_tree, game, card_db, me)
    _remaining, not_in_deck = _deck_status(me)
    mulligan = _extract_mulligan(packet_tree, game, me)

    return ParsedGame(
        own_class=_class_name(me, card_db),
        opponent_class=_class_name(opponent, card_db),
        starting_deck=me.known_starting_deck_list,
        result=_result_for(me),
        turn_log=turn_log,
        drawn_card_ids=not_in_deck,
        game_index=len(parser.games),
        mulligan=mulligan,
    )
