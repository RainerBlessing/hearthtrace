"""Parse a Hearthstone Power.log into a small, stable snapshot.

This module isolates the rest of the app from the `hslog`/`hearthstone`
package internals: callers only ever see `ParsedGame`.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hearthstone.cardxml import load as load_cards
from hearthstone.entities import Game, Player
from hearthstone.enums import BlockType, GameTag, PlayState, Zone
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
    # Card ids that left Zone.DECK for the friendly player during the
    # match, in draw order. Duplicates allowed; not deduplicated against
    # `starting_deck`.
    drawn_card_ids: list[str]


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
    `PlayEvent` for every BlockType.PLAY block and a drawn card id for
    every card that leaves the friendly player's deck.

    Hearthstone numbers turns globally (turn 1 = the first player's
    opening turn, turn 2 = the second player's opening turn, ...) and only
    the GameEntity's own TAG_CHANGE TURN reflects that global count -- each
    player also gets their own per-player TURN tag (their Nth own turn),
    which would misattribute turn numbers if not filtered out.

    `EntityTreeExporter.export()` has already applied every packet by the
    time this walker runs, so `entity.tags` only ever reflects the final,
    post-match state -- it cannot tell us what an entity's zone or
    controller was right before a given TAG_CHANGE. Zone history and
    controller history are therefore tracked ourselves, packet by packet,
    seeded from `entity.initial_zone` / `entity.initial_controller` (the
    values captured when the entity was first registered) the first time
    each entity is seen. Tracking controller history matters because some
    effects (e.g. Death Knight "Plague" cards) change an entity's
    controller mid-match, which would otherwise misattribute a historical
    draw to whichever player ends up controlling the card by the end.

    A given entity is only ever recorded as "drawn" once, no matter how
    many times it leaves `Zone.DECK` -- a card dealt into the opening hand
    and then mulliganed back only re-enters the deck to be drawn again
    later; that is still the same single physical card leaving the deck.
    """

    def __init__(self, game: Game, card_db: Any, friendly_player: Player) -> None:
        self._game = game
        self._card_db = card_db
        self._friendly_player = friendly_player
        self._current_turn = 0
        self._zone_by_entity_id: dict[int, Zone] = {}
        self._controller_by_entity_id: dict[int, Player | None] = {}
        self._drawn_entity_ids: set[int] = set()
        self.events: list[PlayEvent] = []
        self.drawn_card_ids: list[str] = []

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
        elif packet.tag == GameTag.ZONE:
            self._handle_zone_change(packet)
        elif packet.tag == GameTag.CONTROLLER:
            self._handle_controller_change(packet)

    def _handle_turn_change(self, packet: Any) -> None:
        entity_id = int(coerce_to_entity_id(packet.entity))
        if self._game.find_entity_by_id(entity_id) is self._game:
            self._current_turn = packet.value

    def _handle_controller_change(self, packet: Any) -> None:
        entity_id = int(coerce_to_entity_id(packet.entity))
        self._controller_by_entity_id[entity_id] = self._game.get_player(packet.value)

    def _controller_at_time(self, entity_id: int, entity: Any) -> Player | None:
        return self._controller_by_entity_id.get(entity_id, entity.initial_controller)

    def _handle_zone_change(self, packet: Any) -> None:
        entity_id = int(coerce_to_entity_id(packet.entity))
        entity = self._game.find_entity_by_id(entity_id)
        if entity is None:
            return
        old_zone = self._zone_by_entity_id.get(entity_id, entity.initial_zone)
        new_zone = packet.value
        self._zone_by_entity_id[entity_id] = new_zone

        if old_zone != Zone.DECK or new_zone == Zone.DECK:
            return
        if entity_id in self._drawn_entity_ids:
            return
        if self._controller_at_time(entity_id, entity) is not self._friendly_player:
            return
        card_id = getattr(entity, "card_id", None)
        if card_id:
            self._drawn_entity_ids.add(entity_id)
            self.drawn_card_ids.append(card_id)

    def _handle_block(self, packet: Any) -> None:
        if packet.type != BlockType.PLAY:
            return
        entity_id = int(coerce_to_entity_id(packet.entity))
        entity = self._game.find_entity_by_id(entity_id)
        if entity is None or not entity.card_id:
            return
        controller = entity.controller
        player_name = controller.name if controller else "?"
        card = self._card_db.get(entity.card_id)
        card_name = card.name if card else entity.card_id
        self.events.append(
            PlayEvent(turn=self._current_turn, player_name=player_name, card_name=card_name)
        )


def _extract_turn_log_and_draws(
    packet_tree: Any, game: Game, card_db: Any, friendly_player: Player
) -> tuple[list[PlayEvent], list[str]]:
    walker = _TurnLogWalker(game, card_db, friendly_player)
    walker.walk(packet_tree)
    return walker.events, walker.drawn_card_ids


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
    turn_log, drawn_card_ids = _extract_turn_log_and_draws(packet_tree, game, card_db, me)

    return ParsedGame(
        own_class=_class_name(me, card_db),
        opponent_class=_class_name(opponent, card_db),
        starting_deck=me.known_starting_deck_list,
        result=_result_for(me),
        turn_log=turn_log,
        drawn_card_ids=drawn_card_ids,
    )
