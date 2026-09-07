from pathlib import Path

from hs_tracker.markdown_export import export_match_summary, render_match_summary
from hs_tracker.parser import (
    Action,
    BoardState,
    HandState,
    LifeState,
    ManaState,
    MinionState,
    MulliganChoice,
    ParsedGame,
    Turn,
    TurnSnapshot,
)


def _snapshot(
    *,
    mana: ManaState | None = None,
    life: LifeState | None = None,
    hand: HandState | None = None,
    board: BoardState | None = None,
) -> TurnSnapshot:
    return TurnSnapshot(
        mana=mana or ManaState(available=1, maximum=1, locked=0, overload_pending=0),
        life=life or LifeState(own_health=30, own_armor=0, opponent_health=30, opponent_armor=0),
        hand=hand or HandState(own_cards=[], opponent_count=0),
        board=board or BoardState(own=[], opponent=[]),
    )


def _turn(
    *,
    number: int = 1,
    player_name: str = "Du",
    opening_draws: list[str] | None = None,
    start: TurnSnapshot | None = None,
    actions: list[Action] | None = None,
    end: TurnSnapshot | None = None,
) -> Turn:
    return Turn(
        number=number,
        player_name=player_name,
        opening_draws=opening_draws or [],
        start=start or _snapshot(),
        actions=actions or [],
        end=end or _snapshot(),
    )


def test_render_match_summary_includes_result_and_turn_heading() -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=["CS2_022"] * 30,
        result="WON",
        drawn_card_ids=[],
        game_index=1,
        turns=[_turn(number=1, player_name="Du")],
    )

    markdown = render_match_summary(game)

    assert "**Ergebnis:** Sieg" in markdown
    assert "MAGE" in markdown
    assert "## Zug 1 – Du" in markdown


def test_render_match_summary_notes_log_truncation() -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=[],
        result="WON",
        drawn_card_ids=[],
        game_index=1,
        log_truncated=True,
    )

    markdown = render_match_summary(game)

    assert "Größenlimit" in markdown


def test_render_match_summary_omits_truncation_note_by_default() -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=[],
        result="WON",
        drawn_card_ids=[],
        game_index=1,
    )

    markdown = render_match_summary(game)

    assert "Größenlimit" not in markdown


def test_render_match_summary_uses_du_and_gegner_not_raw_account_names() -> None:
    # Action headlines must never leak the user's real Battle.net account
    # name (or the opponent's) into text meant to be pasted into an AI chat.
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=[],
        result="WON",
        drawn_card_ids=[],
        game_index=1,
        turns=[
            _turn(
                number=1,
                actions=[
                    Action(headline="Du: Arcane Missiles gespielt (Mana: 1 → 0)"),
                    Action(headline="Gegner: Fiery War Axe gespielt (Mana: 2 → 0)"),
                ],
            )
        ],
    )

    markdown = render_match_summary(game)

    assert "Du: Arcane Missiles gespielt" in markdown
    assert "Gegner: Fiery War Axe gespielt" in markdown


def test_render_match_summary_includes_full_deck_section_with_card_names() -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=["CS2_022", "CS2_023"],
        result="WON",
        drawn_card_ids=[],
        game_index=1,
    )

    markdown = render_match_summary(game)

    # Only 2 of a real deck's 30 cards are known here -- the label must say
    # so, not claim this is the whole deck.
    assert "**Bekannte Deck-Karten:**" in markdown
    assert "**Deck:**" not in markdown
    assert "Polymorph" in markdown  # CS2_022
    assert "2 Karten" in markdown


def test_render_match_summary_includes_full_deck_label_when_all_30_known() -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=["CS2_022"] * 30,
        result="WON",
        drawn_card_ids=[],
        game_index=1,
    )

    markdown = render_match_summary(game)

    assert "**Deck:**" in markdown
    assert "## Restdeck bei Spielende" in markdown


def test_render_match_summary_includes_remaining_deck_section() -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=["CS2_022"] * 15 + ["CS2_023"] * 15,
        result="WON",
        drawn_card_ids=["CS2_022"],
        game_index=1,
    )

    markdown = render_match_summary(game)

    assert "## Restdeck bei Spielende" in markdown
    assert "Noch 29 Karten im Deck" in markdown
    assert "Arcane Intellect" in markdown.split("## Restdeck bei Spielende")[1]


def test_render_match_summary_labels_remaining_deck_as_known_only_when_incomplete() -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=["CS2_022", "CS2_023"],
        result="WON",
        drawn_card_ids=["CS2_022"],
        game_index=1,
    )

    markdown = render_match_summary(game)

    assert "## Bekannte Karten im Restdeck" in markdown
    assert "## Restdeck bei Spielende" not in markdown
    assert "1 bekannte Karten: Arcane Intellect" in markdown


def test_render_match_summary_includes_mulligan_section_with_card_names() -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=[],
        result="WON",
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
        drawn_card_ids=[],
        game_index=1,
        mulligan=None,
    )

    markdown = render_match_summary(game)

    assert "## Mulligan" not in markdown


def test_render_match_summary_includes_mana_life_hand_and_board_snapshot() -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=[],
        result="WON",
        drawn_card_ids=[],
        game_index=1,
        turns=[
            _turn(
                number=7,
                start=_snapshot(
                    mana=ManaState(available=4, maximum=5, locked=2, overload_pending=1),
                    life=LifeState(
                        own_health=27, own_armor=2, opponent_health=24, opponent_armor=0
                    ),
                    hand=HandState(own_cards=["Hex", "Lightning Bolt"], opponent_count=5),
                    board=BoardState(
                        own=[
                            MinionState(
                                name="Skywall Sentinel", attack=0, health=2, keywords=["Spott"]
                            )
                        ],
                        opponent=[],
                    ),
                ),
            )
        ],
    )

    markdown = render_match_summary(game)

    assert "Mana: 4/5 | Gesperrt: 2 | Überladen: 1" in markdown
    assert "Heldenleben: Du 27 | Gegner 24" in markdown
    assert "Rüstung: Du 2 | Gegner 0" in markdown
    assert "- Hex" in markdown
    assert "Hand (Gegner): 5 Karten" in markdown
    assert "- Skywall Sentinel (0/2, Spott)" in markdown


def test_render_match_summary_omits_hand_and_armor_from_end_snapshot() -> None:
    # Hand/armor changes are already visible as their own action/effect
    # lines during the turn -- repeating the full Start picture in Ende
    # would just duplicate the next turn's Start (or the previous action's
    # diff lines), so Ende only carries Mana/Leben/Board.
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=[],
        result="WON",
        drawn_card_ids=[],
        game_index=1,
        turns=[
            _turn(
                number=1,
                start=_snapshot(
                    hand=HandState(own_cards=["Fireball"], opponent_count=3),
                    life=LifeState(
                        own_health=30, own_armor=5, opponent_health=30, opponent_armor=0
                    ),
                ),
                end=_snapshot(
                    hand=HandState(own_cards=[], opponent_count=4),
                    life=LifeState(
                        own_health=28, own_armor=5, opponent_health=25, opponent_armor=0
                    ),
                ),
            )
        ],
    )

    markdown = render_match_summary(game)

    end_section = markdown.split("### Ende")[1]
    assert "### Start" not in end_section  # sanity: split landed after Start
    assert "Hand (" not in end_section
    assert "Rüstung" not in end_section
    assert "Heldenleben: Du 28 | Gegner 25" in end_section


def test_render_match_summary_shows_opening_draw_before_start_not_as_an_action() -> None:
    # The turn's own automatic draw is already baked into `start.hand` --
    # showing it again as action #1 would misrepresent it as a decision
    # made *after* the point `start` describes, when it's already-known
    # context by then.
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=[],
        result="WON",
        drawn_card_ids=[],
        game_index=1,
        turns=[
            _turn(
                number=3,
                opening_draws=["Fireball gezogen"],
                actions=[Action(headline="Du: Frostbolt gespielt (Mana: 2 → 0)")],
            )
        ],
    )

    markdown = render_match_summary(game)

    assert "Zugbeginn: Fireball gezogen" in markdown
    turn_section = markdown.split("## Zug 3")[1]
    assert turn_section.index("Zugbeginn:") < turn_section.index("### Start")
    assert "1. Fireball gezogen" not in markdown
    assert "1. Du: Frostbolt gespielt" in markdown


def test_render_match_summary_omits_opening_draw_line_when_none() -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=[],
        result="WON",
        drawn_card_ids=[],
        game_index=1,
        turns=[_turn(number=1, opening_draws=[])],
    )

    markdown = render_match_summary(game)

    assert "Zugbeginn:" not in markdown


def test_render_match_summary_includes_actions_with_indented_effects() -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=[],
        result="WON",
        drawn_card_ids=[],
        game_index=1,
        turns=[
            _turn(
                actions=[
                    Action(
                        headline="Du: Ritual of Power gespielt (Mana: 4 → 2)",
                        effects=["Breezling beschworen"],
                    )
                ]
            )
        ],
    )

    markdown = render_match_summary(game)

    assert "1. Du: Ritual of Power gespielt (Mana: 4 → 2)" in markdown
    assert "   → Breezling beschworen" in markdown


def test_export_match_summary_writes_a_file(tmp_path: Path) -> None:
    game = ParsedGame(
        own_class="MAGE",
        opponent_class="WARRIOR",
        starting_deck=[],
        result="WON",
        drawn_card_ids=[],
        game_index=1,
    )

    written = export_match_summary(game, export_dir=tmp_path)

    assert written.exists()
    assert written.parent == tmp_path
    assert written.read_text().startswith("# Hearthstone Match")
