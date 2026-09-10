"""Compute which cards remain unseen in the friendly player's deck."""

from hearthtrace.parser import ParsedGame


def remaining_deck(game: ParsedGame) -> list[str]:
    """Return `game.starting_deck` with one copy removed per drawn card.

    Cards are matched by id; only as many copies are removed as were
    actually drawn (duplicates in `starting_deck` are otherwise kept).
    """
    remaining = list(game.starting_deck)
    for card_id in game.drawn_card_ids:
        if card_id in remaining:
            remaining.remove(card_id)
    return remaining
