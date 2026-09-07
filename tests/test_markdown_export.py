from pathlib import Path

from hs_tracker.markdown_export import export_match_summary, render_match_summary
from hs_tracker.parser import MulliganChoice, ParsedGame, PlayEvent


def test_render_match_summary_includes_result_and_turn_log() -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=["CS2_022"] * 30,
        result="WON",
        turn_log=[PlayEvent(turn=1, player_name="Du", card_name="Arcane Missiles")],
        drawn_card_ids=[],
        game_index=1,
    )

    markdown = render_match_summary(game)

    assert "**Ergebnis:** Sieg" in markdown
    assert "MAGE" in markdown
    assert "Arcane Missiles" in markdown


def test_render_match_summary_uses_du_and_gegner_not_raw_account_names() -> None:
    # The turn log must never leak the user's real Battle.net account name
    # (or the opponent's) into text meant to be pasted into an AI chat.
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=[],
        result="WON",
        turn_log=[
            PlayEvent(turn=1, player_name="Du", card_name="Arcane Missiles"),
            PlayEvent(turn=2, player_name="Gegner", card_name="Fiery War Axe"),
        ],
        drawn_card_ids=[],
        game_index=1,
    )

    markdown = render_match_summary(game)

    assert "**Du:**" in markdown
    assert "**Gegner:**" in markdown


def test_render_match_summary_includes_full_deck_section_with_card_names() -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=["CS2_022", "CS2_023"],
        result="WON",
        turn_log=[],
        drawn_card_ids=[],
        game_index=1,
    )

    markdown = render_match_summary(game)

    assert "**Deck:**" in markdown
    assert "Polymorph" in markdown  # CS2_022
    assert "2 Karten" in markdown


def test_render_match_summary_includes_remaining_deck_section() -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=["CS2_022", "CS2_023"],
        result="WON",
        turn_log=[],
        drawn_card_ids=["CS2_022"],
        game_index=1,
    )

    markdown = render_match_summary(game)

    assert "## Restdeck bei Spielende" in markdown
    assert "Noch 1 Karten im Deck" in markdown
    # CS2_022 (Polymorph) was drawn -- only CS2_023 (Arcane Intellect) remains.
    assert "Polymorph" not in markdown.split("## Restdeck bei Spielende")[1]
    assert "Arcane Intellect" in markdown.split("## Restdeck bei Spielende")[1]


def test_render_match_summary_includes_mulligan_section_with_card_names() -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=[],
        result="WON",
        turn_log=[],
        drawn_card_ids=[],
        game_index=1,
        mulligan=MulliganChoice(kept=["CS2_023"], returned=["CS2_022"]),
    )

    markdown = render_match_summary(game)

    assert "## Mulligan" in markdown
    assert "- Behalten: Arcane Intellect" in markdown
    assert "- Zurückgelegt: Polymorph" in markdown


def test_render_match_summary_omits_mulligan_section_when_none() -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=[],
        result="WON",
        turn_log=[],
        drawn_card_ids=[],
        game_index=1,
        mulligan=None,
    )

    markdown = render_match_summary(game)

    assert "## Mulligan" not in markdown


def test_export_match_summary_writes_a_file(tmp_path: Path) -> None:
    game = ParsedGame(
        own_class="MAGE", opponent_class="WARRIOR", starting_deck=[],
        result="WON", turn_log=[], drawn_card_ids=[], game_index=1,
    )

    written = export_match_summary(game, export_dir=tmp_path)

    assert written.exists()
    assert written.parent == tmp_path
    assert written.read_text().startswith("# Hearthstone Match")
