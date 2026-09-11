from hearthstone.cardxml import load as load_cards

from hearthtrace.card_info import CardInfo, _select_text_variant, card_info, sanitize_description

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
        race_label=None,
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


def test_card_info_reads_the_printed_minion_race() -> None:
    info = card_info("CORE_WC_042", _CARD_DB)  # Whimpering Wretch (Elemental)

    assert info.race_label == "Elementar"


def test_card_info_strips_the_internal_x_marker_from_a_real_card() -> None:
    # User-reported (real screenshot): "[x]Erhält jedes Mal +1 Angriff..."
    # showed the raw "[x]" prefix verbatim in the tooltip.
    info = card_info("CORE_WC_042", _CARD_DB)

    assert info.text == (
        "Erhält jedes Mal +1 Angriff,\nnachdem Ihr einen Elementar\nausgespielt habt."
    )


def test_card_info_resolves_an_unresolved_herald_placeholder_and_drops_the_leading_keyword() -> (
    None
):
    # User-reported (real screenshot): "Spott. Kampfschrei: Kündigt {0} an."
    # showed the raw "{0}" -- the actual card named here is chosen per-copy
    # at collection time (a live-entity fact `card_info` has no access to,
    # see its own docstring), so it falls back to the generic wording the
    # user explicitly signed off on. "Spott" is also dropped from the body
    # since it's already shown separately via `keywords`.
    info = card_info("CATA_722", _CARD_DB)  # Envoy of the End

    assert info.keywords == ["Spott"]
    assert info.text == "<b>Kampfschrei:</b> <b>Kündigt</b> einen Diener an."
    assert "{0}" not in info.text
    assert "Spott" not in info.text


def test_card_info_renders_only_the_active_upgrade_variant() -> None:
    # User-reported (real screenshot): Soldat von Al'Akir's tooltip showed
    # all three "@"-joined upgrade-stage texts concatenated, including
    # each stage's own "Zum Aufwerten N mal ankündigen" hint. Verified
    # against the real match's raw entity tags: the active stage is
    # `TAG_SCRIPT_DATA_NUM_1 - 1`, and that same raw value is also the
    # literal number named in that stage's own text.
    base = card_info("CATA_565t", _CARD_DB, script_data_num_1=1)
    assert base.text == (
        "Benachbarte Diener haben +1 Angriff. "
        "<i>Zum Aufwerten zweimal <b>ankündigen</b>.</i>"
    )
    assert "@" not in base.text

    once_announced = card_info("CATA_565t", _CARD_DB, script_data_num_1=2)
    assert once_announced.text == (
        "Benachbarte Diener haben +2 Angriff. <i>Zum Aufwerten einmal <b>ankündigen</b>.</i>"
    )

    fully_upgraded = card_info("CATA_565t", _CARD_DB, script_data_num_1=3)
    assert fully_upgraded.text == "Benachbarte Diener\nhaben +3 Angriff."
    assert "ankündigen" not in fully_upgraded.text


def test_select_text_variant_leaves_a_single_variant_card_untouched() -> None:
    assert _select_text_variant("Kein Trenner hier.", script_data_num_1=0) == "Kein Trenner hier."


def test_select_text_variant_clamps_an_out_of_range_value() -> None:
    # An unexpectedly large or missing (0) script_data_num_1 must still
    # pick a real variant, not raise or return an empty string.
    variants = "eins@zwei@drei"
    assert _select_text_variant(variants, script_data_num_1=0) == "eins"
    assert _select_text_variant(variants, script_data_num_1=99) == "drei"


def test_card_info_falls_back_for_an_empty_card_id() -> None:
    # No card_id at all happens for a not-yet-revealed entity -- must
    # degrade gracefully, not raise, so a tooltip can still be attempted
    # unconditionally.
    info = card_info("", _CARD_DB)

    assert info.name == "Unbekannte Karte"
    assert info.text == ""
    assert info.cost is None


def test_card_info_falls_back_to_the_raw_id_for_an_unresolvable_card_id() -> None:
    # Code-review-caught inconsistency: a card_id the local card_db has no
    # entry for (as opposed to no card_id at all) must fall back the same
    # way `parser._card_name` already does for the exact same lookup --
    # showing the raw id, not the generic "Unbekannte Karte" -- or a
    # card's board/hand chip label and its tooltip could disagree.
    info = card_info("NOT_A_REAL_CARD_ID", _CARD_DB)

    assert info.name == "NOT_A_REAL_CARD_ID"
    assert info.text == ""
    assert info.cost is None


def test_card_info_reads_a_locations_durability_from_the_same_health_field() -> None:
    # Code-review-caught gap: a Location's durability lives in the same
    # HEALTH field as a weapon's (verified against the real card
    # database: CATA_301 has health=3, durability=0, the unused field) --
    # card_info only special-cased minions/weapons for this, silently
    # dropping a Location's durability from its tooltip.
    info = card_info("CATA_301", _CARD_DB)  # Rubinsanktum (Ruby Sanctum)

    assert info.type_label == "Ort"
    assert info.health == 3
    assert info.attack is None  # Locations have no attack value at all


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


def test_sanitize_description_strips_the_internal_x_marker() -> None:
    assert sanitize_description("[x]Text ohne Marker") == "Text ohne Marker"


def test_sanitize_description_resolves_a_herald_placeholder_generically() -> None:
    assert sanitize_description("<b>Kündigt</b> {0} an.") == "<b>Kündigt</b> einen Diener an."


def test_sanitize_description_drops_a_parenthetical_with_an_unresolved_stat_placeholder() -> None:
    # Verified against the real card database (Twilight Egg): the stats in
    # "Ruft einen Welpling ({0}/{1}) herbei." are only known from a live
    # entity's tags, not the static card_id -- dropped rather than shown
    # as a raw placeholder.
    assert (
        sanitize_description("Ruft einen Welpling ({0}/{1}) herbei.")
        == "Ruft einen Welpling herbei."
    )


def test_sanitize_description_resolves_a_plural_selector_to_its_singular_form() -> None:
    assert sanitize_description("Mischt |4(Kopie,Kopien) davon ein.") == "Mischt Kopie davon ein."


def test_sanitize_description_strips_any_other_unresolved_placeholder() -> None:
    # Safety net for a pattern not specifically handled above -- per
    # explicit user instruction, a "{N}" placeholder must never be
    # visible in the UI, even in a case not seen before.
    assert sanitize_description("Ein {2} Test.") == "Ein Test."
