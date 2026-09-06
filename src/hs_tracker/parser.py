"""Parse a Hearthstone Power.log into a small, stable snapshot.

This module isolates the rest of the app from the `hslog`/`hearthstone`
package internals: callers only ever see `ParsedGame`.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hearthstone.cardxml import load as load_cards
from hearthstone.entities import Game, Player
from hearthstone.enums import BlockType, GameTag, PlayState
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
    `PlayEvent` for every BlockType.PLAY block.

    Hearthstone numbers turns globally (turn 1 = the first player's
    opening turn, turn 2 = the second player's opening turn, ...) and only
    the GameEntity's own TAG_CHANGE TURN reflects that global count -- each
    player also gets their own per-player TURN tag (their Nth own turn),
    which would misattribute turn numbers if not filtered out.
    """

    def __init__(self, game: Game, card_db: Any) -> None:
        self._game = game
        self._card_db = card_db
        self._current_turn = 0
        self.events: list[PlayEvent] = []

    def walk(self, packet_tree: Any) -> list[PlayEvent]:
        for packet in packet_tree:
            self._visit(packet)
        return self.events

    def _visit(self, packet: Any) -> None:
        if isinstance(packet, hslog_packets.TagChange):
            self._handle_tag_change(packet)
        elif isinstance(packet, hslog_packets.Block):
            self._handle_block(packet)
        for child in getattr(packet, "packets", []):
            self._visit(child)

    def _handle_tag_change(self, packet: Any) -> None:
        if packet.tag != GameTag.TURN:
            return
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
        player_name = controller.name if controller else "?"
        card = self._card_db.get(entity.card_id)
        card_name = card.name if card else entity.card_id
        self.events.append(
            PlayEvent(turn=self._current_turn, player_name=player_name, card_name=card_name)
        )


def _extract_turn_log(packet_tree: Any, game: Game, card_db: Any) -> list[PlayEvent]:
    return _TurnLogWalker(game, card_db).walk(packet_tree)


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

    return ParsedGame(
        own_class=_class_name(me, card_db),
        opponent_class=_class_name(opponent, card_db),
        starting_deck=me.known_starting_deck_list,
        result=_result_for(me),
        turn_log=_extract_turn_log(packet_tree, game, card_db),
    )
