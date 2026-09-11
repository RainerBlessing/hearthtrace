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

from hearthstone.enums import CardType, Race, Rarity

from hearthtrace.parser import KEYWORD_LABELS

_TYPE_LABELS: dict[CardType, str] = {
    CardType.MINION: "Diener",
    CardType.SPELL: "Zauber",
    CardType.WEAPON: "Waffe",
    CardType.HERO: "Held",
    CardType.HERO_POWER: "Heldenkraft",
    CardType.LOCATION: "Ort",
}

# Only the tribes that actually show up on a normal hand/board minion --
# deliberately not exhaustive (the enum also has adventure-only/removed
# values like the individual old-WoW humanoid races, BLANK, ALL) since
# those never appear on a real drafted card this tooltip would be shown
# for.
_RACE_LABELS: dict[Race, str] = {
    Race.BEAST: "Bestie",
    Race.DEMON: "Dämon",
    Race.DRAGON: "Drache",
    Race.ELEMENTAL: "Elementar",
    Race.MECHANICAL: "Mechanisch",
    Race.MURLOC: "Murloc",
    Race.NAGA: "Naga",
    Race.PIRATE: "Pirat",
    Race.QUILBOAR: "Quilboar",
    Race.TOTEM: "Totem",
    Race.UNDEAD: "Untot",
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
    race_label: str | None
    keywords: list[str]
    text: str


# User-reported (real screenshots): both a leading "[x]" and a literal
# unresolved "{0}" showed up verbatim in the tooltip -- these are
# Blizzard's own raw templating artifacts, never meant to reach a player,
# that this locale's export doesn't always fully resolve on its own.
#
# "[x]" is a bare formatting marker with no discernible meaning of its own
# (verified: stripping it never changes what the sentence says) -- always
# removed outright, wherever it appears.
_INTERNAL_MARKER_RE = re.compile(r"\[x\]")

# "Kündigt {0} an." ("Herald {0}.") -- the printed text names a specific
# card chosen per-copy at collection time (Cataclysm's "Herald" cards),
# which needs the live *entity's* tags to resolve, not just its card_id
# (see `card_info`'s own docstring for why this function only ever gets a
# card_id). Rather than showing the raw placeholder, falls back to the
# generic phrasing the user explicitly signed off on.
_HERALD_PLACEHOLDER_RE = re.compile(r"\{[0-9]+\}(\s*an\b)")

# "Ruft ... ({0}) herbei." / "({0}/{1})" -- a parenthetical stating the
# summoned token's stats, also only known from live entity data. Dropped
# entirely rather than guessed at; the sentence still reads fine without
# it ("Ruft einen Welpling herbei." instead of "Ruft einen Welpling (2/1)
# herbei.").
_UNRESOLVED_STATS_PAREN_RE = re.compile(r"\s*\([^()]*\{[0-9]+\}[^()]*\)")

# Blizzard's own count-agreement (singular/plural) selector syntax, e.g.
# "|4(Kopie,Kopien)" -- which of the two forms is grammatically correct
# depends on a live count we don't have. Falls back to the first
# (singular) alternative, always at least grammatically plausible.
_PLURAL_SELECTOR_RE = re.compile(r"\|[0-9]+\(([^,()]+),[^()]*\)")

# Catch-all for any other/unanticipated "{N}" placeholder this locale's
# export left unresolved -- per explicit user instruction, a placeholder
# must never be visible in the UI, even if the specific card/pattern
# wasn't seen before. Applied last, after the more specific substitutions
# above already handled the known patterns with better wording.
_UNRESOLVED_PLACEHOLDER_RE = re.compile(r"\{[0-9]+\}")


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
    - internal template markers ("[x]", "{0}", "|4(a,b)") that depend on
      per-entity live data this function doesn't have -- see the regexes
      above for what each one does and why.
    """
    if not description:
        return ""
    text = description.replace("_", " ")
    text = text.replace("</I>", "</i>")
    text = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#)", "&amp;", text)
    text = _INTERNAL_MARKER_RE.sub("", text)
    text = _HERALD_PLACEHOLDER_RE.sub(r"einen Diener\1", text)
    text = _UNRESOLVED_STATS_PAREN_RE.sub("", text)
    text = _PLURAL_SELECTOR_RE.sub(r"\1", text)
    text = _UNRESOLVED_PLACEHOLDER_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _strip_leading_keyword_prefix(text: str, keywords: list[str]) -> str:
    """Drops a keyword's own bold-labeled mention from the *start* of the
    body text when that keyword is already shown separately (see
    `CardInfo.keywords`) -- e.g. "<b>Spott</b>. Kampfschrei: ..." becomes
    just "Kampfschrei: ..." once "Spott" is its own line above, instead of
    repeating it. Only ever strips an exact match at the current start of
    the text (never mid-sentence), and only for labels this exact card
    actually has (`keywords`, not the full `_KEYWORD_ATTRS` list) -- a
    keyword mentioned in a dependent clause elsewhere in the text is left
    untouched.
    """
    changed = True
    while changed:
        changed = False
        for label in keywords:
            pattern = rf"^\s*<b>{re.escape(label)}</b>\.?\s*"
            new_text = re.sub(pattern, "", text, count=1)
            if new_text != text:
                text = new_text
                changed = True
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
            race_label=None,
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
            race_label=None,
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
    keywords = _keywords_of(card)
    text = _strip_leading_keyword_prefix(sanitize_description(card.description), keywords)

    return CardInfo(
        name=card.name,
        type_label=_TYPE_LABELS.get(card.type, ""),
        cost=card.cost,
        attack=attack,
        health=health,
        rarity_label=_RARITY_LABELS.get(card.rarity),
        race_label=_RACE_LABELS.get(card.race),
        keywords=keywords,
        text=text,
    )
