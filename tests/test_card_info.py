from hearthstone.cardxml import load as load_cards

from hs_tracker.card_info import CardInfo, card_info, sanitize_description

_CARD_DB, _ = load_cards(locale="deDE")


def test_card_info_reads_a_real_minion() -> None:
    # Prince Renathal -- a plain minion with no printed keyword.
    info = card_info("REV_018", _CARD_DB)

    assert info == CardInfo(
        name="Prinz Renathal",
        type_label="Diener",
        cost=3,
        attack=3,
        health=4,
        rarity_label="Legendär",
        keywords=[],
        text="Eure Deckgröße\nund Euer anfängliches Leben betragen 40.",
    )


def test_card_info_reads_a_weapon_using_health_as_durability() -> None:
    # Verified against a real match earlier this project (see parser.py's
    # `_weapon_of`): a weapon's durability lives in the same HEALTH field
    # as everything else in this card database, not a separate
    # `durability` attribute (which is always 0, unused).
    info = card_info("CS2_106", _CARD_DB)  # Fiery War Axe

    assert info.name == "Feurige Kriegsaxt"
    assert info.type_label == "Waffe"
    assert info.attack == 3
    assert info.health == 2


def test_card_info_reads_printed_keywords_and_bold_markup() -> None:
    info = card_info("CATA_565", _CARD_DB)  # Skywall Sentinel

    assert info.name == "Himmelswallwächter"
    assert info.keywords == ["Spott"]
    assert "<b>Kampfschrei:</b>" in info.text


def test_card_info_falls_back_for_an_unknown_card_id() -> None:
    # An empty/unresolvable card_id happens for a not-yet-revealed entity
    # (e.g. `_card_name`'s "Unbekannte Karte" case) -- must degrade
    # gracefully, not raise, so a tooltip can still be attempted safely.
    info = card_info("", _CARD_DB)

    assert info.name == "Unbekannte Karte"
    assert info.text == ""
    assert info.cost is None


def test_sanitize_description_replaces_stray_underscore_spacing() -> None:
    # Hearthstone's own German card XML uses a bare "_" as a line-wrap
    # control character in some strings -- verified against a real card
    # (Prince Renathal): "Leben_betragen_40" must read as "Leben betragen
    # 40", not show the underscore.
    assert (
        sanitize_description("Euer anfängliches Leben_betragen_40.")
        == "Euer anfängliches Leben betragen 40."
    )


def test_sanitize_description_normalizes_stray_uppercase_italic_close_tag() -> None:
    # Verified against the real card database: a handful of cards close an
    # <i> with </I> (wrong case) -- Pango markup parsing is case-sensitive
    # and would reject that outright.
    assert sanitize_description("<i>Flavor</I>") == "<i>Flavor</i>"


def test_sanitize_description_escapes_a_stray_ampersand() -> None:
    # Verified against the real card database (two enchantment cards use a
    # literal "&") -- must not be left as invalid Pango markup.
    assert sanitize_description("Natur- & Schattenschaden") == "Natur- &amp; Schattenschaden"


def test_sanitize_description_leaves_already_valid_markup_and_entities_alone() -> None:
    assert sanitize_description("<b>Kampfschrei:</b> Test &amp; mehr Test") == (
        "<b>Kampfschrei:</b> Test &amp; mehr Test"
    )


def test_sanitize_description_handles_empty_text() -> None:
    assert sanitize_description("") == ""
    assert sanitize_description(None) == ""
