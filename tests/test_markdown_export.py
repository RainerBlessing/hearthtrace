from pathlib import Path

from hs_tracker.markdown_export import export_match_summary, render_match_summary
from hs_tracker.parser import ParsedGame, PlayEvent


def test_render_match_summary_includes_result_and_turn_log() -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=["CS2_022"] * 30,
        result="WON",
        turn_log=[PlayEvent(turn=1, player_name="Me", card_name="Arcane Missiles")],
        drawn_card_ids=[],
    )

    markdown = render_match_summary(game)

    assert "**Ergebnis:** Sieg" in markdown
    assert "MAGE" in markdown
    assert "Arcane Missiles" in markdown


def test_export_match_summary_writes_a_file(tmp_path: Path) -> None:
    game = ParsedGame(
        own_class="MAGE", opponent_class="WARRIOR", starting_deck=[],
        result="WON", turn_log=[], drawn_card_ids=[],
    )

    written = export_match_summary(game, export_dir=tmp_path)

    assert written.exists()
    assert written.parent == tmp_path
    assert written.read_text().startswith("# Hearthstone Match")
