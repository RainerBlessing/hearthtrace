from hs_tracker.deck_state import remaining_deck
from hs_tracker.parser import ParsedGame


def test_remaining_deck_removes_drawn_cards() -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=["CS2_022", "CS2_022", "CS2_023"],
        result="WON",
        drawn_card_ids=["CS2_022"],
        game_index=1,
    )

    remaining = remaining_deck(game)

    assert remaining == ["CS2_022", "CS2_023"]
