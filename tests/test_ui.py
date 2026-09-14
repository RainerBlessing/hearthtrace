from hearthtrace.ui import _split_action_headline


def test_split_action_headline_strips_the_redundant_actor_prefix() -> None:
    # Real example (turn 13, opponent): the Aktionen page already has
    # "Zug 13 – Gegner" as its own heading -- repeating "Gegner: " on every
    # single action line just eats into the width available for it, most
    # of all in the compact view.
    main, target = _split_action_headline(
        "Gegner: Teufelskreischer #2 → Soldat von Al'Akir #6", "Gegner"
    )

    assert main == "Teufelskreischer #2 → Soldat von Al'Akir #6"
    assert target is None


def test_split_action_headline_moves_the_target_onto_its_own_line() -> None:
    main, target = _split_action_headline(
        "Gegner: Unterweltriss #1 gespielt → Ziel: Verfluchte Katakomben (Kosten: 0)",
        "Gegner",
    )

    assert main == "Unterweltriss #1 gespielt"
    assert target == "→ Ziel: Verfluchte Katakomben (Kosten: 0)"


def test_split_action_headline_leaves_a_headline_without_the_prefix_untouched() -> None:
    # "Folgeeffekt" (an untracked trigger's folded-in effects) and
    # "Du: Discover" never start with "{player_name}: " in the plain
    # "{player_name}: " sense expected here -- must not mangle either.
    main, target = _split_action_headline("Folgeeffekt", "Gegner")

    assert main == "Folgeeffekt"
    assert target is None
