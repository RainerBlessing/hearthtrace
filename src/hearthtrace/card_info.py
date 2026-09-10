"""Static, printed-card reference info for a card-info tooltip -- "what
does this card actually do", as opposed to `parser.py`'s live board state
("what is it doing right now"). Sourced from the same offline card
database (`hearthstone.cardxml.load`) the rest of the app already uses,
keyed by card_id -- never by display name, which is localized and not
unique (a transformed or generated card keeps its entity's display
numbering, not its card identity).
"""

import re
from dataclasses import dataclass
from typing import Any

from hearthstone.enums import CardType, Rarity

from hearthtrace.parser import KEYWORD_LABELS

_TYPE_LABELS: dict[CardType, str] = {
    CardType.MINION: "Diener",
    CardType.SPELL: "Zauber",
    CardType.WEAPON: "Waffe",
    CardType.HERO: "Held",
    CardType.HERO_POWER: "Heldenkraft",
    CardType.LOCATION: "Ort",
}

_RARITY_LABELS: dict[Rarity, str] = {
    Rarity.COMMON: "Gewöhnlich",
    Rarity.RARE: "Selten",
    Rarity.EPIC: "Episch",
    Rarity.LEGENDARY: "Legendär",
}

# The card's own *printed* keyword abilities -- Blizzard's own glossary
# sense of "keyword" (Taunt, Rush, Combo, ...), not "any section this
# card's text happens to have". Battlecry/Deathrattle are deliberately
# excluded: they're trigger types, not standalone traits, and the text
# already spells them out verbatim ("<b>Kampfschrei:</b> ...") -- a
# separate chip would just repeat that. A broader set than parser.py's
# `_KEYWORD_TAGS` (which only lists what's worth showing on a live board
# frame: Taunt/Divine Shield/Frozen/Stealth/Windfury) -- the three labels
# that do overlap come from the same `KEYWORD_LABELS` parser.py uses, so
# a wording fix can't apply to only one of the two views. Attribute names
# are exactly what `hearthstone.cardxml`'s `Card` exposes; Charge and
# Stealth aren't separate attributes there (Charge no longer exists as a
# keyword, Stealth lives only in runtime tags), so they're not listed
# here.
_KEYWORD_ATTRS: list[tuple[str, str]] = [
    ("taunt", KEYWORD_LABELS["taunt"]),
    ("divine_shield", KEYWORD_LABELS["divine_shield"]),
    ("windfury", KEYWORD_LABELS["windfury"]),
    ("rush", "Ansturm"),
    ("poisonous", "Giftig"),
    ("lifesteal", "Lebensraub"),
    ("reborn", "Wiedergeburt"),
    ("elusive", "Schwer fassbar"),
    ("echo", "Echo"),
    ("magnetic", "Magnetisch"),
    ("outcast", "Außenseiter"),
    ("overload", "Überladung"),
    ("secret", "Geheimnis"),
    ("quest", "Auftrag"),
    ("combo", "Kombo"),
]

_UNKNOWN_CARD_NAME = "Unbekannte Karte"


@dataclass
class CardInfo:
    name: str
    type_label: str
    cost: int | None
    attack: int | None
    health: int | None  # a weapon's durability lives here too, see below
    rarity_label: str | None
    keywords: list[str]
    text: str


def sanitize_description(description: str | None) -> str:
    """Cleans up quirks in Hearthstone's own card-text XML that would
    otherwise show up literally or break Pango markup rendering (the
    tooltip renders `<b>`/`<i>` from the card text as real bold/italic,
    since GTK labels understand that directly) -- all verified against the
    real card database, not guessed:

    - a bare "_" used as a line-wrap control character in some German
      strings (e.g. "Leben_betragen_40") -- replaced with a space.
    - a stray uppercase `</I>` closing tag (a handful of cards) -- Pango
      markup parsing is case-sensitive and would reject it outright.
    - a literal, unescaped "&" (two enchantment cards) -- would otherwise
      make Pango's XML-ish parser choke.
    """
    if not description:
        return ""
    text = description.replace("_", " ")
    text = text.replace("</I>", "</i>")
    text = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#)", "&amp;", text)
    return text


def _keywords_of(card: Any) -> list[str]:
    return [label for attr, label in _KEYWORD_ATTRS if getattr(card, attr, False)]


_STATS_CARD_TYPES = (CardType.MINION, CardType.WEAPON, CardType.LOCATION)


def card_info(card_id: str, card_db: Any) -> CardInfo:
    """Looks up `card_id` in `card_db` (as returned by
    `hearthstone.cardxml.load`). Never raises.

    Two distinct "don't know" cases, matching `parser._card_name`'s own
    distinction for the exact same lookup (so a card's board/hand chip
    label and its tooltip never disagree): no `card_id` at all (an
    entity that hasn't been revealed yet) falls back to a contentless
    "Unbekannte Karte", while a `card_id` the local card_db just doesn't
    have an entry for falls back to showing that id as the name, same as
    the chip already does.
    """
    if not card_id:
        return CardInfo(
            name=_UNKNOWN_CARD_NAME,
            type_label="",
            cost=None,
            attack=None,
            health=None,
            rarity_label=None,
            keywords=[],
            text="",
        )
    card = card_db.get(card_id)
    if card is None:
        return CardInfo(
            name=card_id,
            type_label="",
            cost=None,
            attack=None,
            health=None,
            rarity_label=None,
            keywords=[],
            text="",
        )

    # A weapon's or location's durability is printed into `.health`, not
    # `.durability` (always 0, unused in this data) -- the same HEALTH/
    # DAMAGE pair the live parser already reads for a weapon, for the
    # exact same reason (see `parser._weapon_of`). Locations have no
    # attack value at all, unlike weapons/minions.
    health = card.health if card.type in _STATS_CARD_TYPES else None
    attack = card.atk if card.type in (CardType.MINION, CardType.WEAPON) else None

    return CardInfo(
        name=card.name,
        type_label=_TYPE_LABELS.get(card.type, ""),
        cost=card.cost,
        attack=attack,
        health=health,
        rarity_label=_RARITY_LABELS.get(card.rarity),
        keywords=_keywords_of(card),
        text=sanitize_description(card.description),
    )
