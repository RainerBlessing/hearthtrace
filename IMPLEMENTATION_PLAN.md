# Card Tooltips

User request: hover (or click-to-pin) a card in the Replay view's board/hand
to see its full info (cost, attack/health, type, keywords, card text).
Opponent hand stays hidden as always -- nothing new is exposed there, since
it's already just a count.

Data source (decided): the already-bundled `python-hearthstone` card
database (`hearthstone_data/CardDefs.xml`, offline, no network), loaded with
`locale="deDE"` (already switched project-wide in a prior step). No new
dependency, no download/cache subsystem.

## Stage 1: Thread card_id through the board/hand/weapon data model
**Goal**: `MinionState`, `WeaponState`, and hand cards carry the real
`card_id` alongside their display name, not just the name string.
**Success Criteria**: `parse_log()`'s output exposes a `card_id` for every
board minion, weapon, and own-hand card; existing display behavior
(markdown export, ui.py) unchanged.
**Tests**: update existing construction sites in test_parser.py/
test_markdown_export.py/test_match_history.py to the new shape; no new
tests needed here (pure plumbing).
**Status**: Complete

## Stage 1.5: Switch card_db locale to deDE project-wide
**Goal**: card names/scaffolding text match what the user actually calls
things (confirmed: "Prinz Renathal" not "Prince Renathal").
**Status**: Complete (prerequisite decision, done before Stage 1)

## Stage 2: `card_info.py` -- pure formatting module
**Goal**: given a `card_id` and the (deDE) card_db, return structured
tooltip data: name, cost, attack/health or durability, type label,
keywords, cleaned card text (HTML tags -> Pango markup is fine as-is;
strip stray underscore placeholders Hearthstone's XML uses for spacing).
**Success Criteria**: pure functions, fully unit tested against real
card_ids from the existing fixtures (no invented data).
**Tests**: TDD -- description sanitizing, keyword-list building, unknown
card_id fallback.
**Status**: Not Started

## Stage 3: Hover/click UI in the Replay view
**Goal**: mouse-over a board minion or own-hand chip shows a small popover
with the Stage 2 data; click pins it open; moving away/clicking elsewhere
closes it (native GTK popover dismiss behavior).
**Success Criteria**: verified live (smoke test against the running
TrackerWindow, then a live restart + visual check) -- opponent hand still
shows no per-card info.
**Tests**: live-object smoke test (ui.py has no unit coverage by
convention).
**Status**: Not Started
