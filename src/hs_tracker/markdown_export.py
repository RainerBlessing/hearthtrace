"""Render a `ParsedGame` as a Markdown match summary."""

from datetime import datetime

from hs_tracker.parser import ParsedGame

_RESULT_LABELS = {
    "WON": "Sieg",
    "LOST": "Niederlage",
    "TIED": "Unentschieden",
    "CONCEDED": "Aufgegeben",
    "UNKNOWN": "Unbekannt",
}


def render_match_summary(game: ParsedGame, when: datetime | None = None) -> str:
    when = when or datetime.now()
    lines = [
        f"# Hearthstone Match – {when.strftime('%d.%m.%Y %H:%M')}",
        "",
        f"**Ergebnis:** {_RESULT_LABELS.get(game.result, game.result)}",
        f"**Eigene Klasse:** {game.own_class} | **Gegner-Klasse:** {game.opponent_class}",
        "",
        "## Zugverlauf",
    ]
    for i, event in enumerate(game.turn_log, start=1):
        lines.append(f"{i}. Zug {event.turn} — **{event.player_name}:** {event.card_name} gespielt")
    return "\n".join(lines) + "\n"
