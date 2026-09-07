"""Render a `ParsedGame` as a Markdown match summary."""

from datetime import datetime
from pathlib import Path
from typing import Any

from hearthstone.cardxml import load as load_cards

from hs_tracker import deck_state
from hs_tracker.parser import ParsedGame

_RESULT_LABELS = {
    "WON": "Sieg",
    "LOST": "Niederlage",
    "TIED": "Unentschieden",
    "CONCEDED": "Aufgegeben",
    "UNKNOWN": "Unbekannt",
}


def _card_names(card_ids: list[str], card_db: Any) -> list[str]:
    names = []
    for card_id in card_ids:
        card = card_db.get(card_id)
        names.append(card.name if card else card_id)
    return names


def render_match_summary(game: ParsedGame, when: datetime | None = None) -> str:
    when = when or datetime.now()
    card_db, _ = load_cards()
    deck_names = _card_names(game.starting_deck, card_db)
    remaining_names = _card_names(deck_state.remaining_deck(game), card_db)

    lines = [
        f"# Hearthstone Match – {when.strftime('%d.%m.%Y %H:%M')}",
        "",
        f"**Ergebnis:** {_RESULT_LABELS.get(game.result, game.result)}",
        f"**Eigene Klasse:** {game.own_class} | **Gegner-Klasse:** {game.opponent_class}",
        f"**Deck:** {', '.join(deck_names)} ({len(deck_names)} Karten)",
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
    lines += [
        "## Zugverlauf",
    ]
    for i, event in enumerate(game.turn_log, start=1):
        lines.append(f"{i}. Zug {event.turn} — **{event.player_name}:** {event.card_name} gespielt")
    lines += [
        "",
        "## Restdeck bei Spielende",
        f"- Noch {len(remaining_names)} Karten im Deck: {', '.join(remaining_names)}",
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
