"""Render a `ParsedGame` as a Markdown match summary."""

from datetime import datetime
from pathlib import Path
from typing import Any

from hearthstone.cardxml import load as load_cards

from hearthtrace import deck_state
from hearthtrace.parser import (
    HandState,
    MatchEnded,
    MinionState,
    ParsedGame,
    Turn,
    TurnSnapshot,
    WeaponState,
)

# Public: also used by match_history.py to label a past match's result
# consistently with how it reads in the export itself.
RESULT_LABELS = {
    "WON": "Sieg",
    "LOST": "Niederlage",
    "TIED": "Unentschieden",
    "CONCEDED": "Aufgegeben",
    "UNKNOWN": "Unbekannt",
}

# Present-tense, matching the existing action-headline style ("X gespielt",
# "X beschworen") -- only for a reason we're actually confident about (see
# `MatchEnded`'s own docstring). Missing (reason, actor) combinations (e.g.
# reason == "UNKNOWN", or actor is None) deliberately have no entry here --
# `_match_end_action_headline` returns None for those, and no numbered
# action line is added, only the "Partie beendet" summary below it.
_MATCH_END_ACTION_HEADLINES: dict[tuple[str, str | None], str] = {
    ("CONCEDE", "YOU"): "Du gibst auf",
    ("CONCEDE", "OPPONENT"): "Gegner gibt auf",
    ("DISCONNECT", "YOU"): "Du verlierst die Verbindung",
    ("DISCONNECT", "OPPONENT"): "Gegner verliert die Verbindung",
}


def _match_end_action_headline(match_ended: MatchEnded) -> str | None:
    return _MATCH_END_ACTION_HEADLINES.get((match_ended.reason, match_ended.actor))

# A real constructed-format deck always has exactly 30 cards. `starting_deck`
# only contains cards actually seen (drawn, played, revealed) by the end of
# the match, so it's frequently an incomplete subset -- the wording must say
# so, rather than implying it's the true, complete deck/remaining-deck state.
_FULL_DECK_SIZE = 30


def _card_names(card_ids: list[str], card_db: Any) -> list[str]:
    names = []
    for card_id in card_ids:
        card = card_db.get(card_id)
        names.append(card.name if card else card_id)
    return names


def _render_minion(minion: MinionState) -> str:
    if minion.keywords:
        return f"- {minion.name} ({minion.attack}/{minion.health}, {', '.join(minion.keywords)})"
    return f"- {minion.name} ({minion.attack}/{minion.health})"


def _render_board(label: str, minions: list[MinionState]) -> list[str]:
    lines = [f"Board ({label}):"]
    lines += [_render_minion(m) for m in minions] if minions else ["- (leer)"]
    return lines


def _render_hand(hand: HandState) -> list[str]:
    lines = ["Hand (Du):"]
    lines += [f"- {card.name}" for card in hand.own_cards] if hand.own_cards else ["- (leer)"]
    lines += ["", f"Hand (Gegner): {hand.opponent_count} Karten"]
    return lines


def _render_weapon(weapon: WeaponState | None) -> str:
    if weapon is None:
        return "(keine)"
    return f"{weapon.name} ({weapon.attack}/{weapon.durability})"


def _render_mana_and_life(snapshot: TurnSnapshot, *, include_armor: bool) -> list[str]:
    mana = snapshot.mana
    life = snapshot.life
    lines = [
        f"Mana: {mana.available}/{mana.maximum} | Gesperrt: {mana.locked}"
        f" | Überladen: {mana.overload_pending}",
        f"Heldenleben: Du {life.own_health} | Gegner {life.opponent_health}",
        f"Waffe: Du {_render_weapon(snapshot.own_weapon)}"
        f" | Gegner {_render_weapon(snapshot.opponent_weapon)}",
    ]
    if include_armor:
        lines.append(f"Rüstung: Du {life.own_armor} | Gegner {life.opponent_armor}")
    return lines


def _render_start_snapshot(snapshot: TurnSnapshot) -> list[str]:
    # The full decision-relevant picture: what the player could see and
    # act on at the start of the turn -- this is what "was this turn
    # optimal?" analysis has to be judged against.
    return [
        "### Start",
        *_render_mana_and_life(snapshot, include_armor=True),
        "",
        *_render_hand(snapshot.hand),
        "",
        *_render_board("Du", snapshot.board.own),
        "",
        *_render_board("Gegner", snapshot.board.opponent),
    ]


def _render_end_snapshot(snapshot: TurnSnapshot) -> list[str]:
    # Deliberately leaner than Start: hand and armor changes are already
    # visible as their own action/effect lines during the turn, so
    # repeating the full picture here would just be redundant with the
    # next turn's Start (or the previous action's diff lines).
    return [
        "### Ende",
        *_render_mana_and_life(snapshot, include_armor=False),
        "",
        *_render_board("Du", snapshot.board.own),
        "",
        *_render_board("Gegner", snapshot.board.opponent),
    ]


def _render_actions(turn: Turn, match_ended: MatchEnded | None) -> list[str]:
    if not turn.actions and match_ended is None:
        return ["### Aktionen", "- (keine)"]
    lines = ["### Aktionen"]
    if not turn.actions:
        lines.append("- (keine)")
    else:
        for i, action in enumerate(turn.actions, start=1):
            lines.append(f"{i}. {action.headline}")
            lines += [f"   → {effect}" for effect in action.effects]
    if match_ended is not None:
        reason_headline = _match_end_action_headline(match_ended)
        if reason_headline is not None:
            lines.append(f"{len(turn.actions) + 1}. {reason_headline}")
        result_label = RESULT_LABELS.get(match_ended.result, match_ended.result)
        lines += ["", f"Partie beendet – {result_label}"]
    return lines


def _render_opening_draws(turn: Turn) -> list[str]:
    if not turn.opening_draws:
        return []
    return [f"Zugbeginn: {', '.join(turn.opening_draws)}", ""]


def _render_turn(turn: Turn, match_ended: MatchEnded | None) -> list[str]:
    return [
        f"## Zug {turn.number} – {turn.player_name}",
        "",
        *_render_opening_draws(turn),
        *_render_start_snapshot(turn.start),
        "",
        *_render_actions(turn, match_ended),
        "",
        *_render_end_snapshot(turn.end),
        "",
    ]


def render_match_summary(game: ParsedGame, when: datetime | None = None) -> str:
    when = when or datetime.now()
    card_db, _ = load_cards(locale="deDE")
    deck_names = _card_names(game.starting_deck, card_db)
    remaining_names = _card_names(deck_state.remaining_deck(game), card_db)

    deck_label = "Deck" if len(deck_names) >= _FULL_DECK_SIZE else "Bekannte Deck-Karten"
    lines = [
        f"# Hearthstone Match – {when.strftime('%d.%m.%Y %H:%M')}",
        "",
        f"**Ergebnis:** {RESULT_LABELS.get(game.result, game.result)}",
        f"**Eigene Klasse:** {game.own_class} | **Gegner-Klasse:** {game.opponent_class}",
        f"**{deck_label}:** {', '.join(deck_names)} ({len(deck_names)} Karten)",
        "",
    ]
    if game.log_truncated:
        lines += [
            "**Hinweis:** Hearthstones eigenes Power.log hat während dieser Partie sein"
            " Größenlimit (10 MB) erreicht; Hearthstone stellt das Schreiben ab diesem"
            " Punkt komplett ein. Spätere Ereignisse (ggf. auch das Ende der Partie)"
            " fehlen dadurch möglicherweise.",
            "",
        ]
    if game.mulligan is not None:
        kept_names = _card_names(game.mulligan.kept, card_db)
        returned_names = _card_names(game.mulligan.returned, card_db)
        lines += [
            "## Mulligan",
            f"- Behalten: {', '.join(kept_names)}",
            f"- Zurückgelegt: {', '.join(returned_names)}",
            "",
        ]
    last_turn_index = len(game.turns) - 1
    for i, turn in enumerate(game.turns):
        turn_match_ended = game.match_ended if i == last_turn_index else None
        lines += _render_turn(turn, turn_match_ended)
    if len(deck_names) >= _FULL_DECK_SIZE:
        lines += [
            "## Restdeck bei Spielende",
            f"- Noch {len(remaining_names)} Karten im Deck: {', '.join(remaining_names)}",
        ]
    else:
        lines += [
            "## Bekannte Karten im Restdeck",
            "(Das Deck wird nicht im Voraus geladen -- nur bereits gesehene Karten sind bekannt,"
            " die tatsächliche Kartenzahl im Deck kann höher sein.)",
            f"- {len(remaining_names)} bekannte Karten: {', '.join(remaining_names)}",
        ]
    return "\n".join(lines) + "\n"


def export_match_summary(game: ParsedGame, export_dir: Path, when: datetime | None = None) -> Path:
    """Render `game` and write it to `export_dir`, returning the file path."""
    when = when or datetime.now()
    export_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{when.strftime('%Y-%m-%d_%H-%M-%S')}_{game.result}.md"
    path = export_dir / filename
    path.write_text(render_match_summary(game, when=when))
    return path
