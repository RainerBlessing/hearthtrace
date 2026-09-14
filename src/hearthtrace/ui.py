"""The live deck-list window."""

import sys
import textwrap
from html import escape
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk  # noqa: E402
from hearthstone.cardxml import load as load_cards  # noqa: E402

from hearthtrace.card_info import CardInfo, card_info  # noqa: E402
from hearthtrace.config import Config  # noqa: E402
from hearthtrace.deck_state import remaining_deck  # noqa: E402
from hearthtrace.log_reader import find_latest_power_log  # noqa: E402
from hearthtrace.log_setup import check_log_setup, disable_log_size_limit  # noqa: E402
from hearthtrace.markdown_export import RESULT_LABELS, export_match_summary  # noqa: E402
from hearthtrace.match_history import (  # noqa: E402
    MatchHistoryEntry,
    format_history_row,
    load_match_history,
    record_replay_source,
)
from hearthtrace.parser import (  # noqa: E402
    Action,
    HandCard,
    MatchEnded,
    MinionState,
    NoGameFoundError,
    ParsedGame,
    Turn,
    TurnSnapshot,
    parse_log,
    parse_log_at_index,
)

# Terminal `PlayState` values (mirrors `markdown_export.RESULT_LABELS`'
# terminal set). Anything else -- PLAYING, WINNING, LOSING, DISCONNECTED,
# UNKNOWN, INVALID -- means the match is still in progress and must not
# trigger an export yet.
_FINISHED_RESULTS = {"WON", "LOST", "TIED", "CONCEDED"}

# UI-only German class-name relabeling for the Live match-status card --
# `game.own_class`/`opponent_class` are the raw `CardClass` enum name
# (e.g. "SHAMAN"), used as-is everywhere else in the app (Markdown export
# included) since that text isn't user-facing prose there. Here it's the
# most prominent thing on the page, right next to an already-German result
# label, so leaving it in English would read as an inconsistency the user
# actually flagged. Same precedent as `_KEYWORD_CHIP_LABELS` below: a
# presentational-only remap, parser.py's own data is untouched.
_CLASS_LABELS = {
    "DEATHKNIGHT": "Todesritter",
    "DEMONHUNTER": "Dämonenjäger",
    "DRUID": "Druide",
    "HUNTER": "Jäger",
    "MAGE": "Magier",
    "PALADIN": "Paladin",
    "PRIEST": "Priester",
    "ROGUE": "Schurke",
    "SHAMAN": "Schamane",
    "WARLOCK": "Hexenmeister",
    "WARRIOR": "Krieger",
    "UNKNOWN": "Unbekannt",
}

# Dedicated app-state location for the export dedup marker (see
# `_last_export_marker_path`) -- deliberately *not* inside `export_dir`,
# which is a user-facing folder whose only documented purpose is holding
# human-readable match exports. A user archiving or clearing that folder
# (a reasonable thing to do with "my exports") must not silently resurrect
# the re-export-on-restart bug this marker exists to prevent. Hardcoded
# rather than reading `$XDG_STATE_HOME`, matching `app.py`'s equally
# hardcoded `CONFIG_PATH` -- no other part of this project reads XDG env
# vars, so doing it only here would be inconsistent for no real benefit.
_STATE_DIR = Path.home() / ".local" / "state" / "hearthtrace"

# The three Replay stages, in viewing order. Kept as separate, ordered
# stages (not e.g. a bool) specifically so "Start" never shows this turn's
# actions -- an analysis workflow ("what would I have played here?") needs
# to see only what was actually knowable *before* the decision, the same
# "no future information" principle `parser.py`'s `_card_name` already
# follows for card reveals. "Aktionen" is deliberately its own stage
# (rather than folded into "Ende") so it can be read on its own, without
# the resulting board state also answering the question at the same time.
_REPLAY_STAGE_START = "start"
_REPLAY_STAGE_ACTIONS = "actions"
_REPLAY_STAGE_END = "end"
_REPLAY_STAGES = (_REPLAY_STAGE_START, _REPLAY_STAGE_ACTIONS, _REPLAY_STAGE_END)

# UI-only relabeling for a minion-keyword chip's text -- purely cosmetic,
# doesn't touch `parser.py`'s wording (also used, unchanged, by the
# Markdown export). "kann angreifen" gets a plain "●" dot rather than the
# word "bereit": user feedback on a real screenshot -- it's shown on most
# board rows (every minion that can act at all), so a compact glyph scans
# faster there than the word does, and it's a proposed replacement of
# their own. "nur Diener" deliberately keeps its word: it's the rarer,
# less obvious case (Rush-restricted to minions only), where a second
# unlabeled glyph next to the first would trade a small win in density for
# a real loss in clarity.
_KEYWORD_CHIP_LABELS = {"kann angreifen": "●"}

# Temporary attack-readiness state ("can this minion attack right now, and
# legally hit the enemy hero?"), as opposed to an actual printed card
# keyword (Spott, Gottesschild, ...). User feedback on a real screenshot:
# rendered as an identical bordered chip, "bereit" read as equally
# important as "Spott" -- `_build_keyword_row` renders these as plain dim
# text instead of a chip specifically because they're transient board
# state, not a fact about the card.
_STATE_KEYWORDS = frozenset({"kann angreifen", "nur Diener"})

# Fixed width for a board minion's frame, so the board reads as a steady
# grid instead of every box being exactly as wide as its own longest line
# (a long name vs. a short one, or one extra keyword chip).
_MINION_FRAME_WIDTH = 76

# Every page's content is wrapped in an Adw.Clamp at this width: on a wide
# or ultrawide window, the outer whitespace grows instead of the cards,
# gaps, and line lengths themselves -- below this width (i.e. this
# window's normal size) a Clamp does nothing at all, so this has no effect
# on the layout already tuned for a normal-width window.
_CONTENT_MAX_WIDTH = 1100

# The Live page's own, narrower clamp width: user feedback on a real
# screenshot -- the match-status card only ever holds two or three short
# lines, so clamping it to the same 1100px as Replay/History (which have
# genuinely wide content: board flowboxes, action timelines) just moved
# the empty space from "around the card" to "inside the card, and below
# it" instead of removing it.
_TRACKER_MAX_WIDTH = 720

# Below this window width, the Replay board switches from a card grid to
# compact text rows (see `_build_board`/`_build_board_compact`). User
# feedback on a real screenshot: at this app's normal (deliberately
# narrow, so it fits comfortably next to Hearthstone or a terminal) width,
# 6-7 minions in a 2-column card grid squeezed every card so narrow that
# names like "Der Wirbelnde Nether" barely fit -- a real breakpoint,
# switching the whole layout strategy rather than shrinking the same
# layout further, keeps names fully readable at any width.
_COMPACT_BOARD_WIDTH = 700

# Caps a Replay Aktionen label's width (in characters, pre-wrapped via
# `textwrap.fill` -- see `_build_wrapping_label`) so a long combined line
# (a hero-attack's "Attacker → Defender", or an effect naming a long card)
# actually wraps instead of overflowing the available width. Calibrated
# empirically against this window's real content column width at each
# size (~300px compact, ~700px wide inside `_ACTIONS_READING_COLUMN_WIDTH`):
# bold ".heading" glyphs (the headline) are noticeably wider per character
# than plain ".caption" ones (an effect line), so a single shared value
# either wrapped headlines too late (overflowing) or effects too early
# (choppier than the space in front of them actually needed) -- two
# values per width, not one. User feedback: the wide view shouldn't need
# to wrap most real headlines/effects at all -- its column is wide enough
# that these two are generous ceilings, not an active constraint, the same
# way `_COMPACT_BOARD_WIDTH`'s wide board grid isn't either.
_ACTION_HEADLINE_MAX_WIDTH_CHARS_COMPACT = 36
_ACTION_EFFECT_MAX_WIDTH_CHARS_COMPACT = 44
_ACTION_HEADLINE_MAX_WIDTH_CHARS_WIDE = 80
_ACTION_EFFECT_MAX_WIDTH_CHARS_WIDE = 95

# A centered reading column for the Aktionen list specifically, narrower
# than the other Replay stages' shared `_CONTENT_MAX_WIDTH` -- user
# feedback: actions read like a log/document, not a dashboard, and a very
# wide window just made each line harder to scan, not more useful. Doesn't
# affect Start/Ende (their own board/hand content still uses the wider
# clamp) since this wraps only the Aktionen list itself, and has no effect
# at all in the compact view (the window is already narrower than this).
_ACTIONS_READING_COLUMN_WIDTH = 750

# Semantic Adwaita color classes for the match-status card's result dot --
# "success"/"warning"/"error" are stock libadwaita style classes (like
# "accent"/"dim-label" elsewhere in this file), not custom CSS. A tie is
# deliberately left uncolored (neither a win nor a loss).
_RESULT_DOT_CSS_CLASS = {
    "WON": "success",
    "LOST": "error",
    "CONCEDED": "error",  # the friendly player's own PLAYSTATE -- a loss
}

# Past tense for the Replay conclusion block ("Gegner hat aufgegeben"),
# distinct from markdown_export's own present-tense action-headline
# wording ("Gegner gibt auf") -- this isn't rendered as a numbered action
# here, it's a standalone summary sentence. Missing (reason, actor)
# combinations (reason == "UNKNOWN", or actor is None -- see MatchEnded's
# own docstring for why those happen) deliberately have no entry; the
# block still shows, just without a specific cause.
_MATCH_END_REASON_TEXT: dict[tuple[str, str | None], str] = {
    ("CONCEDE", "YOU"): "Du hast aufgegeben",
    ("CONCEDE", "OPPONENT"): "Gegner hat aufgegeben",
    ("DISCONNECT", "YOU"): "Du hast die Verbindung verloren",
    ("DISCONNECT", "OPPONENT"): "Gegner hat die Verbindung verloren",
}

# Adw.ToggleGroup's own vertical padding (confirmed via its real CSS node,
# `toggle-group` containing `toggle` children -- inspected directly rather
# than guessed) reads as noticeably tall against this window's small
# fixed size. Trimmed here rather than left at the library default; safe
# to fail silently (an unmatched/ineffective rule just does nothing) if a
# future libadwaita version restructures this internally.
#
# `.tabular-nums` (`_build_board_compact`'s stats column): user feedback on
# a real screenshot -- with the compact board's stats genuinely aligned
# into one Gtk.Grid column, "6 / 12" and "0 / 1" still looked slightly
# offset from each other, because ordinary proportional digits aren't all
# the same width ("1" is narrower than "6"). OpenType tabular figures give
# every digit the same advance width, so numbers in the same column truly
# line up instead of just starting at the same x position.
_REPLAY_CSS = (
    "toggle-group toggle { padding-top: 2px; padding-bottom: 2px; }"
    ' .tabular-nums { font-feature-settings: "tnum" 1; }'
)


def _install_replay_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_string(_REPLAY_CSS)
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )


def _tooltip_meta_line(info: CardInfo) -> str | None:
    meta = []
    if info.cost is not None:
        meta.append(f"{info.cost} Mana")
    if info.type_label:
        meta.append(info.type_label)
    if info.rarity_label:
        meta.append(info.rarity_label)
    return escape(" · ".join(meta)) if meta else None


def _tooltip_stats_line(info: CardInfo) -> str | None:
    # Spells have neither -- an empty stats line would just be a blank
    # row between the meta line and the keywords/text, exactly the
    # "empty stat block" the user asked to avoid for non-minions.
    stats = []
    if info.attack is not None and info.health is not None:
        stats.append(f"{info.attack}/{info.health}")
    elif info.health is not None:  # a weapon's durability/Location -- no attack
        stats.append(str(info.health))
    if info.race_label:
        stats.append(info.race_label)
    return escape(" · ".join(stats)) if stats else None


def _split_action_headline(headline: str, player_name: str) -> tuple[str, str | None]:
    """UI-only display transform for the Aktionen renderer (both window
    widths -- see `TrackerWindow._build_action_content`) -- never touches
    `Action.headline` itself (shared with the Markdown export, which wants
    the full sentence). Strips the leading "{player_name}: " (redundant
    here: the page's own "Zug N – {player_name}" heading already says
    whose turn it is), and -- when present -- moves " → Ziel: X" onto its
    own line rather than letting it run on past the main sentence. Both
    markers are ones `parser.py`'s own headline-building code always emits
    verbatim, not free-form card text, so matching them exactly is safe.
    """
    prefix = f"{player_name}: "
    text = headline[len(prefix) :] if headline.startswith(prefix) else headline
    marker = " → Ziel: "
    if marker not in text:
        return text, None
    main, _, target = text.partition(marker)
    return main, f"→ Ziel: {target}"


def _card_tooltip_markup(info: CardInfo) -> str:
    # `info.text` is already sanitized into valid Pango markup (its own
    # <b>/<i> tags are meant to render, not show up literally) -- every
    # other field here is plain text and must be escaped, or a card name
    # or keyword that happens to contain "&"/"<" would break the whole
    # tooltip instead of just that one field.
    #
    # Deliberately laid out as four visually distinct lines (title / meta
    # / stats / keywords) instead of one running line of comma-separated
    # facts -- user feedback on a real screenshot found the previous
    # single-line layout too "raw"/unstructured to scan at a glance.
    header = [f'<span size="large" weight="bold">{escape(info.name)}</span>']

    meta_line = _tooltip_meta_line(info)
    if meta_line is not None:
        header.append(meta_line)

    stats_line = _tooltip_stats_line(info)
    if stats_line is not None:
        header.append(stats_line)

    if info.keywords:
        header.append(f'<b>{escape(", ".join(info.keywords))}</b>')

    if not info.text:
        return "\n".join(header)
    # A small explicit spacer (rather than just a blank line) between the
    # header block and the effect text, plus a slightly smaller size for
    # the text itself -- user feedback: wanted a bit more breathing room
    # there, and the body text read as competing with the header rather
    # than clearly secondary to it.
    spacer = '<span size="4000"> </span>'
    return "\n".join(header) + f"\n{spacer}\n" + f'<span size="small">{info.text}</span>'


class TrackerWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, config: Config) -> None:
        super().__init__(application=app, title="HearthTrace")
        self.set_default_size(320, 480)
        # Deviation from the plan: the plan's sketch called a non-existent
        # `set_widget_name` (AttributeError against the installed GTK4
        # bindings). `Gtk.Widget.set_name` is the real API for a stable CSS
        # node name; the actual Wayland/Hyprland window class comes from
        # the application id passed to `Adw.Application` in app.py.
        self.set_name("hearthtrace")
        _install_replay_css()

        self._config = config
        self._last_log_path: Path | None = None
        self._last_log_mtime: float | None = None
        # Persisted to disk (not just kept in memory): restarting the app
        # while the most recently played match's log is still the newest
        # one on disk must not re-export it a second time -- an in-memory-
        # only key would forget that it was already exported the moment
        # the process restarts.
        self._last_exported_key = self._load_last_exported_key()
        # Loaded once here rather than per poll tick: it's a static XML
        # dataset (the same one parser.py/markdown_export.py load), so
        # re-loading it every 2s would be pure waste.
        self._card_db, _ = load_cards(locale="deDE")
        # A card's tooltip markup is a pure function of (card_id,
        # self._card_db), and self._card_db never changes for the life of
        # this window -- caching it here avoids recomputing the same
        # card_info() lookup and markup string on every single Replay
        # re-render (every ~2s poll tick while following the live match,
        # on top of every turn/stage navigation).
        # Keyed by (card_id, script_data_num_1), not just card_id: a
        # multi-variant card's tooltip text depends on the specific
        # entity's own live tag value too (see `card_info.card_info`'s
        # `script_data_num_1` parameter) -- caching by card_id alone would
        # serve one entity's variant text to a different entity of the
        # same card at a different upgrade stage.
        self._card_tooltip_markup_cache: dict[tuple[str, int], str] = {}
        # The most recently parsed game and which of its turns Replay is
        # currently showing -- kept independent of `_last_log_path`/
        # `_last_log_mtime` (which gate *whether* to re-parse) since Replay
        # needs the actual `Turn` data, not just "did the file change".
        # Live/current match only (no persistence): once the app restarts
        # or a new match starts, only turns still reachable by re-parsing
        # the current session's Power.log are ever available.
        self._replay_game: ParsedGame | None = None
        self._replay_turn_index = 0
        self._replay_stage = _REPLAY_STAGE_START
        # Non-None while Replay is pinned to a specific past match opened
        # from Verlauf (log_path, game_index) -- while pinned, the live
        # poll (`_update_replay_state`) must not silently replace what's
        # being reviewed, the same way it already won't yank the viewer
        # back to "now" mid-match (see that method's own docstring). The
        # live game is still stashed on every poll regardless
        # (`_live_replay_game`), so "back to live" has something to
        # restore without waiting for the next poll tick.
        self._replay_source: tuple[Path, int] | None = None
        self._live_replay_game: ParsedGame | None = None
        # Updated by the `Adw.Breakpoint` set up below, once the window is
        # actually realized/sized -- `True` is the correct value up until
        # then too, since `set_default_size` below (320px) is already
        # narrower than `_COMPACT_BOARD_WIDTH`.
        self._compact_board = True

        header_bar = Adw.HeaderBar()
        # Adw.ToggleGroup (libadwaita >=1.7): a real segmented control, not
        # Adw.ViewStack/ViewSwitcher -- this window is only 320px wide, too
        # narrow for switcher chrome to look right.
        self._view_group = Adw.ToggleGroup()
        view_group = self._view_group
        for name, label in (("tracker", "Live"), ("history", "Verlauf"), ("replay", "Replay")):
            toggle = Adw.Toggle()
            toggle.set_name(name)
            toggle.set_label(label)
            view_group.add(toggle)
        view_group.connect("notify::active-name", self._on_view_toggled)
        # App name at the start, the view switcher as the header's own
        # (centered) title widget -- the standard GNOME/libadwaita header
        # hierarchy, instead of the window's own title fighting the
        # switcher for the header's one centered slot.
        app_name_label = Gtk.Label(label="HearthTrace")
        app_name_label.add_css_class("heading")
        header_bar.pack_start(app_name_label)
        header_bar.set_title_widget(view_group)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header_bar)

        # Rebuilt in place on every poll tick by `_render_tracker_*` below
        # (same "clear and re-add" convention already used for
        # `_history_list`/`_deck_list`'s rows), rather than a nested
        # Gtk.Stack -- the three states share no widgets worth preserving
        # between them.
        self._tracker_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._render_tracker_empty("Warte auf Hearthstone …")

        self._history_entries: list[MatchHistoryEntry] = []
        self._history_list = Gtk.ListBox()
        self._history_list.connect("row-activated", self._on_history_row_activated)
        history_scroller = Gtk.ScrolledWindow()
        history_scroller.set_child(self._clamp(self._history_list))

        replay_scroller = Gtk.ScrolledWindow()
        # Gtk.ScrolledWindow's default policy is AUTOMATIC on *both* axes --
        # found live in the compact Aktionen view: a wrapping label whose
        # natural (unwrapped) width slightly exceeds the window's actual
        # width doesn't get squeezed down and wrapped the way a fixed-size
        # window would force it to; the scroller just lets it overflow
        # sideways instead (invisibly, since there's no visible horizontal
        # scrollbar to hint that's what happened). Forcing NEVER here means
        # content always has to fit -- and therefore actually wrap -- within
        # the window's own width, which is exactly what every wrapping
        # label in this view already assumes.
        replay_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        replay_scroller.set_child(self._clamp(self._build_replay_box()))

        self._stack = Gtk.Stack()
        tracker_clamp = Adw.Clamp()
        tracker_clamp.set_maximum_size(_TRACKER_MAX_WIDTH)
        tracker_clamp.set_child(self._tracker_box)
        self._stack.add_named(tracker_clamp, "tracker")
        self._stack.add_named(history_scroller, "history")
        self._stack.add_named(replay_scroller, "replay")
        toolbar_view.set_content(self._stack)
        self.set_content(toolbar_view)

        # Drives `_compact_board` (see `_build_board`) off the window's
        # actual current width rather than a size guessed once at startup
        # -- this app is normally used docked narrow next to Hearthstone or
        # a terminal, but resizing it wider should still switch the Replay
        # board back to the full card grid, live, not just on the next
        # poll tick.
        board_breakpoint = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse(f"max-width: {_COMPACT_BOARD_WIDTH - 1}px")
        )
        board_breakpoint.connect("apply", self._on_board_breakpoint_apply)
        board_breakpoint.connect("unapply", self._on_board_breakpoint_unapply)
        self.add_breakpoint(board_breakpoint)

        GLib.timeout_add_seconds(2, self._poll)
        # Deferred rather than shown right here: the window isn't presented
        # yet at this point in construction (`app.py` calls `win.present()`
        # only after this constructor returns), and `Adw.AlertDialog.present`
        # needs a real, visible parent. `idle_add` runs this once the main
        # loop is idle -- i.e. after `present()` has already happened.
        GLib.idle_add(self._maybe_offer_log_size_limit_fix)

    def _maybe_offer_log_size_limit_fix(self) -> bool:
        # Only the 10MB size limit is ever offered a fix here -- the other
        # two checks (whether Power.log is being written at all) require
        # setting up `log.config` from scratch, which is a bigger one-time
        # manual step (see the project's design doc) than this tracker can
        # safely automate. If the size limit is already fine, say nothing:
        # a dialog confirming "everything is fine" on every single startup
        # would just be noise.
        check = check_log_setup(self._config.logs_dir.parent)
        if check.size_limit_disabled:
            return GLib.SOURCE_REMOVE

        def _mark(ok: bool) -> str:
            return "✓" if ok else "✗"

        dialog = Adw.AlertDialog(
            heading="Hearthstone-Logging",
            body=(
                f"{_mark(check.power_log_enabled)} Power.log aktiviert\n"
                f"{_mark(check.verbose_enabled)} Verbose Power-Logging aktiviert\n"
                f"{_mark(check.size_limit_disabled)} 10-MB-Loglimit aktiv\n\n"
                "Hearthstone schneidet Power.log bei 10MB ab und schreibt danach "
                "gar nichts mehr hinein -- ein noch laufendes Match ist ab diesem "
                "Punkt für keinen Log-Tracker mehr rekonstruierbar. "
                "client.config wird vorher gesichert (client.config.bak). "
                "Hearthstone muss danach neu gestartet werden, damit die Änderung "
                "wirkt."
            ),
        )
        dialog.add_response("later", "Später")
        dialog.add_response("fix", "Loglimit deaktivieren")
        dialog.set_response_appearance("fix", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("fix")
        dialog.set_close_response("later")
        dialog.connect("response", self._on_log_size_limit_response)
        dialog.present(self)
        return GLib.SOURCE_REMOVE

    def _on_log_size_limit_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response != "fix":
            return
        try:
            disable_log_size_limit(self._config.logs_dir.parent)
        except OSError as e:
            self._show_error_dialog("Fehler", f"client.config konnte nicht geschrieben werden: {e}")

    @staticmethod
    def _clamp(child: Gtk.Widget) -> Adw.Clamp:
        clamp = Adw.Clamp()
        clamp.set_maximum_size(_CONTENT_MAX_WIDTH)
        clamp.set_child(child)
        return clamp

    def _build_replay_box(self) -> Gtk.Box:
        nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        nav_box.set_halign(Gtk.Align.CENTER)
        self._replay_prev_button = Gtk.Button(icon_name="go-previous-symbolic")
        self._replay_prev_button.add_css_class("flat")
        self._replay_prev_button.add_css_class("circular")
        self._replay_prev_button.connect("clicked", self._on_replay_prev)
        self._replay_turn_label = Gtk.Label(label="Keine Züge verfügbar")
        self._replay_turn_label.add_css_class("heading")
        self._replay_next_button = Gtk.Button(icon_name="go-next-symbolic")
        self._replay_next_button.add_css_class("flat")
        self._replay_next_button.add_css_class("circular")
        self._replay_next_button.connect("clicked", self._on_replay_next)
        nav_box.append(self._replay_prev_button)
        nav_box.append(self._replay_turn_label)
        nav_box.append(self._replay_next_button)

        self._replay_position_label = Gtk.Label(label="")
        self._replay_position_label.add_css_class("caption")
        # Not the theme's own ".dim-label" (opacity ~0.55) -- user feedback
        # on a real screenshot: "33 / 33" is relevant information while
        # navigating (how far into the match this turn is), not filler
        # text, and read as too faint at that opacity. A tick more visible,
        # same convention already used for a Replay action's effect line.
        self._replay_position_label.set_opacity(0.75)
        self._replay_position_label.set_halign(Gtk.Align.CENTER)

        self._replay_stage_group = Adw.ToggleGroup()
        for name, label in (
            (_REPLAY_STAGE_START, "Start"),
            (_REPLAY_STAGE_ACTIONS, "Aktionen"),
            (_REPLAY_STAGE_END, "Ende"),
        ):
            toggle = Adw.Toggle()
            toggle.set_name(name)
            toggle.set_label(label)
            self._replay_stage_group.add(toggle)
        self._replay_stage_group.connect("notify::active-name", self._on_replay_stage_toggled)
        self._replay_stage_group.set_halign(Gtk.Align.CENTER)

        self._replay_content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._replay_content_box.set_valign(Gtk.Align.START)

        # Nav/position/stage-toggle are one tight visual cluster (spacing
        # 2, not the 8 used between unrelated sections) -- they're all one
        # "where am I" readout, not three separate blocks, and packing
        # them closer shaves real height off the header area.
        header_cluster = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        header_cluster.append(nav_box)
        header_cluster.append(self._replay_position_label)
        header_cluster.append(self._replay_stage_group)

        # Hidden unless Replay is pinned to a past match opened from
        # Verlauf (see `_replay_source`) -- otherwise this row would just
        # be empty chrome for the overwhelmingly common "watching the
        # current match" case.
        self._replay_pin_label = Gtk.Label(xalign=0.5)
        self._replay_pin_label.add_css_class("caption")
        self._replay_pin_label.add_css_class("dim-label")
        self._replay_unpin_button = Gtk.Button(label="Zur aktuellen Partie")
        self._replay_unpin_button.add_css_class("flat")
        self._replay_unpin_button.connect("clicked", self._on_replay_unpin)
        pin_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        pin_row.set_halign(Gtk.Align.CENTER)
        pin_row.append(self._replay_pin_label)
        pin_row.append(self._replay_unpin_button)
        pin_row.set_visible(False)
        self._replay_pin_row = pin_row

        replay_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        replay_box.append(header_cluster)
        replay_box.append(pin_row)
        replay_box.append(Gtk.Separator())
        replay_box.append(self._replay_content_box)

        # Left/Right to step turns; S/A/E to jump straight to a stage,
        # Space to step forward through Start -> Aktionen -> Ende -- paging
        # through many turns via mouse clicks alone is tedious for the
        # exact "scan through the match" workflow Replay exists for.
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_replay_key_pressed)
        self.add_controller(key_controller)

        return replay_box

    def _on_board_breakpoint_apply(self, _breakpoint: Adw.Breakpoint) -> None:
        self._compact_board = True
        self._refresh_replay_view()

    def _on_board_breakpoint_unapply(self, _breakpoint: Adw.Breakpoint) -> None:
        self._compact_board = False
        self._refresh_replay_view()

    def _on_view_toggled(self, group: Adw.ToggleGroup, _pspec: GObject.ParamSpec) -> None:
        page_name = group.get_active_name()
        self._stack.set_visible_child_name(page_name)
        if page_name == "history":
            self._refresh_history_list()
        elif page_name == "replay":
            self._refresh_replay_view()

    def _refresh_history_list(self) -> None:
        """Rebuild the Verlauf list from every exported match on disk.

        Wrapped in a broad `except Exception`, unlike a plain read: this
        runs from a GTK signal handler (the toggle button), not from
        `_poll`'s own already-guarded call site -- an uncaught exception
        here (e.g. a export-dir file that's unreadable, non-UTF-8, or gets
        deleted mid-scan) would otherwise propagate straight out of the
        signal handler and could leave the toggle unresponsive for the
        rest of the session, with no visible error.
        """
        while (row := self._history_list.get_row_at_index(0)) is not None:
            self._history_list.remove(row)
        self._history_entries = []
        try:
            entries = load_match_history(self._config.export_dir, state_dir=_STATE_DIR)
        except Exception as exc:  # noqa: BLE001 - must never break the toggle
            print(f"hearthtrace: error while loading match history: {exc}", file=sys.stderr)
            self._history_list.append(Gtk.Label(label="Fehler beim Laden des Verlaufs", xalign=0))
            return
        if not entries:
            self._history_list.append(Gtk.Label(label="Noch keine Partien gespeichert", xalign=0))
            return
        self._history_entries = entries
        for entry in entries:
            self._history_list.append(Gtk.Label(label=format_history_row(entry), xalign=0))

    def _on_history_row_activated(self, _list_box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        """Opens the clicked Verlauf entry's match in Replay, pinned (see
        `_replay_source`) so the live poll doesn't silently swap it back
        out. Rows are appended in the same order as `_history_entries`, so
        the row's own index is the entry's index -- no per-row data
        attachment needed."""
        index = row.get_index()
        if index < 0 or index >= len(self._history_entries):
            return
        entry = self._history_entries[index]
        if entry.log_path is None or entry.game_index is None:
            self._show_error_dialog(
                "Replay nicht verfügbar",
                "Für dieses Match wurde keine Replay-Quelle aufgezeichnet "
                "(z. B. ein Export von vor dieser Funktion).",
            )
            return
        if not entry.log_path.exists():
            self._show_error_dialog(
                "Replay nicht verfügbar",
                f"Das ursprüngliche Log existiert nicht mehr: {entry.log_path}",
            )
            return
        try:
            game = parse_log_at_index(entry.log_path, entry.game_index)
        except Exception as exc:  # noqa: BLE001 - must never crash this signal handler
            # Code-review-caught gap: this used to catch only
            # (NoGameFoundError, OSError), narrower than the live-poll
            # path's own blanket `except Exception` -- but hslog can raise
            # other exception types from a malformed packet (see
            # `export_packet`'s own `except TypeError` fix elsewhere in
            # this file for a real example), which would otherwise
            # propagate uncaught out of this GTK signal handler instead of
            # showing an error dialog.
            self._show_error_dialog("Replay nicht verfügbar", str(exc))
            return

        self._replay_game = game
        self._replay_turn_index = 0
        self._replay_stage = _REPLAY_STAGE_START
        self._replay_source = (entry.log_path, entry.game_index)
        self._replay_stage_group.set_active_name(_REPLAY_STAGE_START)
        self._view_group.set_active_name("replay")

    def _show_error_dialog(self, heading: str, body: str) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", "OK")
        dialog.present(self)

    def _on_replay_prev(self, _button: Gtk.Button) -> None:
        self._replay_turn_index = max(0, self._replay_turn_index - 1)
        self._refresh_replay_view()

    def _on_replay_next(self, _button: Gtk.Button) -> None:
        if self._replay_game is not None:
            last = len(self._replay_game.turns) - 1
            self._replay_turn_index = min(last, self._replay_turn_index + 1)
        self._refresh_replay_view()

    def _on_replay_stage_toggled(self, group: Adw.ToggleGroup, _pspec: GObject.ParamSpec) -> None:
        self._replay_stage = group.get_active_name()
        self._refresh_replay_view()

    def _update_replay_state(self, game: ParsedGame) -> None:
        """Track the latest parsed game for Replay, called on every
        successful poll regardless of which view is currently visible (so
        switching to Replay always shows up-to-date data).

        Resets to the latest turn when a genuinely different match starts
        (`game_index` changed) or the previously-viewed index no longer
        exists; also *follows* the latest turn while the viewer was already
        looking at it (mirrors "auto-scroll only if already at the
        bottom") -- but leaves the index alone if the user had navigated
        back to inspect an earlier turn, so a live-updating match doesn't
        keep yanking them back to "now" every 2 seconds.

        `game` is stashed into `_live_replay_game` unconditionally, even
        while pinned to a past match from Verlauf -- see `_replay_source`.
        """
        self._live_replay_game = game
        if self._replay_source is not None:
            return
        previous = self._replay_game
        is_new_match = previous is None or game.game_index != previous.game_index
        was_following_latest = previous is not None and (
            self._replay_turn_index >= len(previous.turns) - 1
        )
        self._replay_game = game
        if is_new_match or was_following_latest or self._replay_turn_index >= len(game.turns):
            self._replay_turn_index = max(0, len(game.turns) - 1)
        if self._stack.get_visible_child_name() == "replay":
            self._refresh_replay_view()

    def _on_replay_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if self._stack.get_visible_child_name() != "replay":
            return False
        if keyval == Gdk.KEY_Left:
            self._on_replay_prev(self._replay_prev_button)
        elif keyval == Gdk.KEY_Right:
            self._on_replay_next(self._replay_next_button)
        elif keyval in (Gdk.KEY_s, Gdk.KEY_S):
            self._replay_stage_group.set_active_name(_REPLAY_STAGE_START)
        elif keyval in (Gdk.KEY_a, Gdk.KEY_A):
            self._replay_stage_group.set_active_name(_REPLAY_STAGE_ACTIONS)
        elif keyval in (Gdk.KEY_e, Gdk.KEY_E):
            self._replay_stage_group.set_active_name(_REPLAY_STAGE_END)
        elif keyval == Gdk.KEY_space:
            next_index = (_REPLAY_STAGES.index(self._replay_stage) + 1) % len(_REPLAY_STAGES)
            self._replay_stage_group.set_active_name(_REPLAY_STAGES[next_index])
        else:
            return False
        return True

    def _on_replay_unpin(self, _button: Gtk.Button) -> None:
        self._replay_source = None
        game = self._live_replay_game
        self._replay_game = game
        self._replay_turn_index = max(0, len(game.turns) - 1) if game is not None else 0
        self._refresh_replay_view()

    def _refresh_replay_view(self) -> None:
        self._replay_pin_row.set_visible(self._replay_source is not None)
        if self._replay_source is not None:
            self._replay_pin_label.set_label("Verlauf-Ansicht (nicht die aktuelle Partie)")
        game = self._replay_game
        if game is None or not game.turns:
            # Code-review-caught gap: a concede/disconnect during mulligan
            # (before STEP ever reaches MAIN_ACTION) leaves game.turns
            # empty -- silently showing the generic "no turns" placeholder
            # here dropped the match's own ending, the exact "log just
            # stops with no explanation" gap this feature exists to fix.
            match_ended = game.match_ended if game is not None else None
            self._replay_turn_label.set_label(
                "Kein Zug aufgezeichnet" if match_ended is not None else "Keine Züge verfügbar"
            )
            self._replay_position_label.set_label("")
            self._replay_prev_button.set_sensitive(False)
            self._replay_next_button.set_sensitive(False)
            self._clear_box(self._replay_content_box)
            if match_ended is not None:
                self._replay_content_box.append(self._build_match_ended_block(match_ended))
            return
        turn = game.turns[self._replay_turn_index]
        self._replay_turn_label.set_label(f"Zug {turn.number} – {turn.player_name}")
        self._replay_position_label.set_label(
            f"{self._replay_turn_index + 1} / {len(game.turns)}"
        )
        self._replay_prev_button.set_sensitive(self._replay_turn_index > 0)
        self._replay_next_button.set_sensitive(self._replay_turn_index < len(game.turns) - 1)
        self._render_replay_turn(turn)

    def _render_replay_turn(self, turn: Turn) -> None:
        """Renders exactly one of the three stages -- never more than one.

        Deliberately *not* "Start always shows this turn's actions too":
        for the "what would I have played here?" analysis workflow, Start
        must show only what was actually knowable *before* the decision,
        the same "no future information" principle `parser.py`'s
        `_card_name` already follows for card reveals -- showing this
        turn's outcome (its actions, or the Ende board) while still on
        Start would silently answer the question being asked.
        """
        self._clear_box(self._replay_content_box)
        if self._replay_stage == _REPLAY_STAGE_ACTIONS:
            self._render_replay_actions(turn)
        else:
            snapshot = turn.end if self._replay_stage == _REPLAY_STAGE_END else turn.start
            self._render_replay_snapshot(turn, snapshot)

    def _render_replay_snapshot(self, turn: Turn, snapshot: TurnSnapshot) -> None:
        box = self._replay_content_box

        # `snapshot.mana` is whoever's turn it *was* -- not always "Du" --
        # so it's attached to whichever side's line actually matches, not
        # hardcoded onto "Du" (that previously mislabeled the opponent's
        # own mana as the player's during an opponent turn).
        mana_text = f"Mana {snapshot.mana.available}/{snapshot.mana.maximum}"
        opponent_mana = mana_text if turn.player_name == "Gegner" else None
        own_mana = mana_text if turn.player_name == "Du" else None

        box.append(
            self._build_side_header(
                "GEGNER",
                self._format_side_detail(
                    snapshot.life.opponent_health, opponent_mana, snapshot.hand.opponent_count
                ),
                accent=False,
            )
        )
        box.append(self._build_board(snapshot.board.opponent))
        box.append(Gtk.Separator())

        own_header = self._build_side_header(
            "DU", self._format_side_detail(snapshot.life.own_health, own_mana, None), accent=True
        )
        own_header.set_margin_top(8)
        box.append(own_header)
        box.append(self._build_own_body(turn, snapshot))

    def _build_own_body(self, turn: Turn, snapshot: TurnSnapshot) -> Gtk.Widget:
        # User feedback on a real screenshot: an empty board followed by
        # its own "Hand" heading plus a separate "(leer)" line read as two
        # headings for nothing, with a lot of resulting blank space below
        # DU once both were genuinely empty (late in a lost/won match) --
        # one compact line says the same thing.
        if not snapshot.board.own and not snapshot.hand.own_cards:
            empty_label = Gtk.Label(label="Board leer · Hand leer", xalign=0)
            empty_label.add_css_class("caption")
            empty_label.add_css_class("dim-label")
            return empty_label

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.append(self._build_board(snapshot.board.own))
        if snapshot is turn.end:
            # User-requested review hint, generalized around
            # `MinionState.attacks_remaining` (not tailored to Windfury/
            # Al'Akir specifically -- verified against a real match that
            # any minion, not just a Windfury one, can end a turn with an
            # attack left unused). Only shown on the Ende stage: at Start
            # or during Aktionen, "still has an attack" doesn't mean
            # anything yet -- the turn isn't over.
            unused_attack_hint = self._build_unused_attack_hint(snapshot.board.own)
            if unused_attack_hint is not None:
                body.append(unused_attack_hint)

        hand_caption = Gtk.Label(label="Hand", xalign=0)
        hand_caption.add_css_class("caption")
        hand_caption.add_css_class("dim-label")
        hand_caption.set_margin_top(4)
        body.append(hand_caption)
        if snapshot.hand.own_cards:
            body.append(self._build_hand(snapshot.hand.own_cards))
        else:
            empty_hand = Gtk.Label(label="(leer)", xalign=0)
            empty_hand.add_css_class("dim-label")
            body.append(empty_hand)
        return body

    @staticmethod
    def _format_side_detail(hp: int, mana: str | None, hand_count: int | None) -> str:
        # Fixed labels ("Mana", "Hand") for every segment except HP's own
        # heart glyph, joined with one consistent separator -- so the line
        # reads as a small table of facts, not a string of differently-
        # shaped tokens. A bare 3-space gap (no visible separator) read as
        # too dense on a narrow window per user feedback on a real
        # screenshot -- a "·" between facts (matching `_tooltip_meta_line`'s
        # own separator convention) breaks the line up without needing a
        # narrower window to do it.
        parts = [f"♥ {hp}"]
        if mana is not None:
            parts.append(mana)
        if hand_count is not None:
            parts.append(f"Hand {hand_count}")
        return "   ·   ".join(parts)

    @staticmethod
    def _build_side_header(name: str, detail: str, *, accent: bool) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_label = Gtk.Label(label=name, xalign=0)
        # Fixed to "GEGNER"'s own length (the longer of the two side
        # names): user feedback on a real screenshot -- "DU"'s short name
        # left the detail line (♥/Mana/Hand) starting well left of where
        # it starts on the GEGNER line above/below it, when both should
        # read as the same kind of row.
        name_label.set_width_chars(len("GEGNER"))
        name_label.add_css_class("heading")
        if accent:
            # A single subtle, theme-provided accent (Adwaita's semantic
            # ".accent" class, not a hardcoded color) on the player's own
            # side only -- distinguishes the two sections without
            # introducing arbitrary colors that could clash with whatever
            # accent Omarchy's active theme happens to use.
            name_label.add_css_class("accent")
        detail_label = Gtk.Label(label=detail, xalign=0)
        detail_label.add_css_class("dim-label")
        row.append(name_label)
        row.append(detail_label)
        # User feedback on a real screenshot: the board directly below sat
        # right against this line with no breathing room at all, at odds
        # with the actual separation between the GEGNER/DU sections.
        row.set_margin_bottom(6)
        return row

    def _render_replay_actions(self, turn: Turn) -> None:
        # Everything below goes through one inner column, clamped to
        # `_ACTIONS_READING_COLUMN_WIDTH` -- see that constant's own
        # comment for why Aktionen specifically gets a narrower reading
        # column than Start/Ende's board content.
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        if not turn.actions:
            placeholder = Gtk.Label(label="(keine Aktionen diesen Zug)", xalign=0)
            placeholder.add_css_class("dim-label")
            column.append(placeholder)
        total = len(turn.actions)
        for index, action in enumerate(turn.actions, start=1):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.set_valign(Gtk.Align.START)
            # Explicitly override this row's own expand flag rather than
            # leaving it computed: without this, the connector's own
            # vexpand below would propagate all the way up through row
            # into `column`, which is exactly the "stretches across the
            # whole page" bug fixed previously. Setting it here stops that
            # propagation at the row boundary, so vexpand can still do its
            # actual job -- filling *this row's own* height, which is
            # however tall its `content` column naturally is -- without
            # also inflating the page.
            row.set_vexpand(False)
            row.append(self._build_action_badge_column(index, total))
            row.append(self._build_action_content(action, turn))
            column.append(row)

        game = self._replay_game
        if (
            game is not None
            and game.match_ended is not None
            and game.turns
            and turn is game.turns[-1]
        ):
            column.append(self._build_match_ended_block(game.match_ended))

        clamp = Adw.Clamp()
        clamp.set_maximum_size(_ACTIONS_READING_COLUMN_WIDTH)
        clamp.set_child(column)
        self._replay_content_box.append(clamp)

    @staticmethod
    def _build_action_badge_column(index: int, total: int) -> Gtk.Box:
        number_label = Gtk.Label(label=str(index))
        number_label.add_css_class("heading")
        number_label.set_margin_top(2)
        number_label.set_margin_bottom(2)
        number_label.set_margin_start(6)
        number_label.set_margin_end(6)
        number_frame = Gtk.Frame()
        number_frame.set_child(number_label)
        number_frame.set_halign(Gtk.Align.CENTER)
        number_frame.set_valign(Gtk.Align.START)

        # A connector spanning down to the next badge, so the numbered
        # list reads as one continuous sequence rather than separate
        # unrelated rows -- its height follows the row's actual content
        # height (vexpand, but scoped to this row only -- see
        # `_render_replay_actions`'s own comment), not a fixed guess, so a
        # multi-effect action's longer text doesn't leave the line
        # stopping short of the next number.
        badge_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        badge_column.set_valign(Gtk.Align.FILL)
        badge_column.append(number_frame)
        if index < total:
            connector = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
            # A vertical Box's child defaults to filling the column's full
            # (perpendicular) width -- without an explicit narrow width and
            # centered halign, this renders as a solid gray block as wide
            # as the badge circle above it, not a thin connecting line.
            connector.set_size_request(2, -1)
            connector.set_halign(Gtk.Align.CENTER)
            connector.set_vexpand(True)
            connector.set_margin_top(4)
            connector.set_margin_bottom(4)
            badge_column.append(connector)
        return badge_column

    @staticmethod
    def _build_wrapping_label(text: str, *, max_chars: int) -> Gtk.Label:
        # `wrap=True` alone isn't enough -- found live in the compact
        # Aktionen view: a long combined action line (e.g. an attacker's
        # and a defender's name both in one headline) ran right off the
        # window's actual edge instead of wrapping. Root cause: a
        # Gtk.Label's *natural* width (what it reports wanting, before any
        # wrapping) is its full unwrapped single-line width, and nothing
        # in this view's container chain (Gtk.ScrolledWindow, Adw.Clamp)
        # forces a child down to less than its own natural width -- both
        # exist to cap an *upper* bound or allow scrolling past a lower
        # one, not to shrink content that wants to be wider than the
        # window. `set_max_width_chars` alone didn't change that in
        # practice either. Pre-breaking the text ourselves with
        # `textwrap.fill` sidesteps the whole question: the label's own
        # natural width is then only ever as wide as its longest already-
        # broken line, which does fit. `max_chars` is a call-site choice
        # rather than one shared value, since a bold ".heading" line and a
        # plain ".caption" one fit meaningfully different character counts
        # in the same pixel width.
        label = Gtk.Label(label=textwrap.fill(text, width=max_chars), xalign=0)
        label.set_wrap(True)
        return label

    def _action_headline_max_chars(self) -> int:
        return (
            _ACTION_HEADLINE_MAX_WIDTH_CHARS_COMPACT
            if self._compact_board
            else _ACTION_HEADLINE_MAX_WIDTH_CHARS_WIDE
        )

    def _action_effect_max_chars(self) -> int:
        return (
            _ACTION_EFFECT_MAX_WIDTH_CHARS_COMPACT
            if self._compact_board
            else _ACTION_EFFECT_MAX_WIDTH_CHARS_WIDE
        )

    def _build_action_effect_label(self, text: str) -> Gtk.Label:
        effect_label = self._build_wrapping_label(text, max_chars=self._action_effect_max_chars())
        effect_label.add_css_class("caption")
        # A tick lighter than the theme's own ".dim-label" (which reads as
        # too dark for a secondary line at this size) -- a plain opacity
        # reduction instead of overriding that semantic class app-wide.
        effect_label.set_opacity(0.75)
        return effect_label

    def _build_action_content(self, action: Action, turn: Turn) -> Gtk.Box:
        """One content renderer for a Replay action, used at every window
        width. User feedback on a real screenshot: the wide and compact
        Aktionen views had drifted into two different information
        hierarchies -- a repeated "Gegner: "/"Du: " on every line (already
        said once by the page's own "Zug N - Gegner" heading), a target/
        cost folded into the headline sentence, effects reading as
        appended prose rather than a distinct group -- instead of the same
        semantics at two sizes. Responsive only changes layout knobs here
        (wrap width via `_action_headline_max_chars`/
        `_action_effect_max_chars`), never the structure itself.
        """
        action_lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        effects = action.effects

        if action.hero_attack is not None:
            # `Action.headline` folds this into one sentence with the
            # damage number inline (for the Markdown export) -- `hero_attack`
            # carries the same facts already split apart, so this doesn't
            # need to parse that sentence back out.
            detail = action.hero_attack
            headline_label = self._build_wrapping_label(
                f"{detail.attacker_name} → {detail.defender_name}",
                max_chars=self._action_headline_max_chars(),
            )
            headline_label.add_css_class("heading")
            action_lines.append(headline_label)
            effects = [
                f"{detail.damage} Schaden · {detail.health_before} → {detail.health_after}",
                *action.effects,
            ]
        else:
            main_text, target_text = _split_action_headline(action.headline, turn.player_name)
            headline_label = self._build_wrapping_label(
                main_text, max_chars=self._action_headline_max_chars()
            )
            headline_label.add_css_class("heading")
            action_lines.append(headline_label)
            if target_text is not None:
                action_lines.append(
                    self._build_wrapping_label(
                        target_text, max_chars=self._action_effect_max_chars()
                    )
                )

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        content.append(action_lines)
        if effects:
            # Its own tightly-spaced group, one notch further indented and
            # set apart from `action_lines` above by `content`'s own wider
            # spacing -- user feedback: without a clearer gap here, an
            # action's results and the next action's headline read as one
            # undifferentiated block instead of visually distinct groups.
            effects_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            effects_box.set_margin_start(8)
            for effect in effects:
                effects_box.append(self._build_action_effect_label(effect))
            content.append(effects_box)

        content.set_margin_bottom(6)
        return content

    @staticmethod
    def _build_match_ended_block(match_ended: MatchEnded) -> Gtk.Box:
        # Deliberately not another numbered/badged row like the actions
        # above -- a concede/disconnect is a match-end event, not a
        # Hearthstone turn action ("played a card", "attacked"), so it
        # gets its own visually separate block instead.
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.add_css_class("card")
        box.set_margin_top(12)

        icon = Gtk.Label(label="✓")
        icon.add_css_class(_RESULT_DOT_CSS_CLASS.get(match_ended.result, "dim-label"))
        icon.set_margin_start(12)
        box.append(icon)

        text_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_column.set_margin_top(8)
        text_column.set_margin_bottom(8)
        text_column.set_margin_end(12)
        title = Gtk.Label(label="Partie beendet", xalign=0)
        title.add_css_class("heading")
        text_column.append(title)

        result_label = RESULT_LABELS.get(match_ended.result, match_ended.result)
        reason_text = _MATCH_END_REASON_TEXT.get((match_ended.reason, match_ended.actor))
        subtitle_text = f"{reason_text} · {result_label}" if reason_text else result_label
        subtitle = Gtk.Label(label=subtitle_text, xalign=0)
        subtitle.add_css_class("dim-label")
        text_column.append(subtitle)

        box.append(text_column)
        return box

    @staticmethod
    def _build_unused_attack_hint(minions: list[MinionState]) -> Gtk.Label | None:
        # User feedback on a real screenshot: naming every minion inline
        # ran off the edge of the page with a wide board. For a review
        # hint, the count alone is enough to notice "something was left
        # on the table" while scanning turns -- the board itself (already
        # visible right above this) shows exactly which minion once you
        # go looking. Counts *attacks*, not minions, so a Windfury minion
        # with both attacks unused contributes 2, not 1.
        total = sum(m.attacks_remaining for m in minions)
        if total == 0:
            return None
        hint = Gtk.Label(label=f"⚠ Ungenutzte Angriffe: {total}", xalign=0)
        hint.add_css_class("caption")
        hint.add_css_class("warning")
        hint.set_margin_top(4)
        return hint

    def _build_board(self, minions: list[MinionState]) -> Gtk.Widget:
        """Dispatches between the two Replay board layouts (see
        `_COMPACT_BOARD_WIDTH`) -- never both at once, and never a shrunk
        version of the other; the two are different layout strategies for
        the same data, not the same layout at two sizes."""
        if self._compact_board:
            return self._build_board_compact(minions)
        return self._build_board_grid(minions)

    def _build_board_grid(self, minions: list[MinionState]) -> Gtk.FlowBox:
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_min_children_per_line(1)
        flow.set_max_children_per_line(7)  # Hearthstone's own board-size cap
        for minion in minions:
            flow.append(self._build_minion_frame(minion))
        return flow

    def _build_board_compact(self, minions: list[MinionState]) -> Gtk.Grid:
        # User feedback on a real screenshot: at this window's normal
        # width, `_build_board_grid`'s 2-column card grid squeezed 6-7
        # minions so narrow that names barely fit. One row per minion
        # (name, stats, keywords, left to right) reads as a small table
        # instead -- more information-dense, and every name stays fully
        # readable regardless of how many minions are on the board.
        #
        # A Gtk.Grid, not a Box per row: follow-up feedback on that same
        # table -- stats/keywords should sit at the same horizontal
        # position regardless of how long each row's own name happens to
        # be. A Grid's columns each size themselves to the widest cell in
        # that column across *every* row attached to it, which independent
        # per-row Boxes can't do on their own.
        grid = Gtk.Grid()
        grid.set_row_spacing(6)
        grid.set_column_spacing(8)
        for row_index, minion in enumerate(minions):
            # Deliberately *not* `name_label.set_hexpand(True)`: a wrapping
            # label's natural width collapses toward its minimum once
            # hexpand lets it, which previously under-reported this grid's
            # actual natural width to the window -- it opened too narrow
            # for its own content, clipping stats/keywords off the right
            # edge instead of wrapping them. Each column sizing itself off
            # the cells' own natural (unwrapped) width avoids that; `wrap`
            # stays on purely as a safety net for a name genuinely too long
            # for the available space.
            name_label = Gtk.Label(label=minion.name, xalign=0)
            name_label.set_wrap(True)
            grid.attach(name_label, 0, row_index, 1, 1)

            stats_label = Gtk.Label(label=f"{minion.attack} / {minion.health}", xalign=0)
            stats_label.add_css_class("heading")
            stats_label.add_css_class("tabular-nums")
            grid.attach(stats_label, 1, row_index, 1, 1)

            keyword_row = self._build_keyword_row(minion.keywords)
            if keyword_row is not None:
                grid.attach(keyword_row, 2, row_index, 1, 1)

            self._attach_card_tooltip(name_label, minion.card_id, minion.script_data_num_1)
        return grid

    def _build_minion_frame(self, minion: MinionState) -> Gtk.Frame:
        # Name/stats/keywords deliberately don't share one font size: the
        # numbers (what matters for "can this trade/kill?") are the most
        # prominent, the name is de-emphasized, keywords are separate,
        # smaller chips pulled close to the stats rather than more
        # same-weight text. A fixed frame width keeps the whole board a
        # steady grid instead of every box sized to its own longest line.
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        name_label = Gtk.Label(label=minion.name, xalign=0.5)
        name_label.add_css_class("caption")
        name_label.add_css_class("dim-label")
        name_label.set_justify(Gtk.Justification.CENTER)
        name_label.set_wrap(True)
        content.append(name_label)

        stats_label = Gtk.Label(label=f"{minion.attack} / {minion.health}")
        stats_label.add_css_class("title-3")
        content.append(stats_label)

        keyword_row = self._build_keyword_row(minion.keywords, halign=Gtk.Align.CENTER)
        if keyword_row is not None:
            content.append(keyword_row)

        for setter in (
            content.set_margin_top,
            content.set_margin_bottom,
            content.set_margin_start,
            content.set_margin_end,
        ):
            setter(6)
        frame = Gtk.Frame()
        frame.set_child(content)
        frame.set_size_request(_MINION_FRAME_WIDTH, -1)
        # Without this, a FlowBoxChild's default fill alignment stretches
        # the frame to its whole cell's width whenever a row has leftover
        # space (e.g. only 2-3 minions on a wide board row) -- exactly the
        # "cards become huge empty rectangles" symptom reported. START
        # keeps it at its own natural/requested width regardless of how
        # much room the row actually has.
        frame.set_halign(Gtk.Align.START)
        self._attach_card_tooltip(frame, minion.card_id, minion.script_data_num_1)
        return frame

    def _build_hand(self, cards: list[HandCard]) -> Gtk.Widget:
        """Same layout split as `_build_board`: below `_COMPACT_BOARD_WIDTH`
        the two-column pill-chip grid is exactly the "cards too narrow"
        problem the board already had -- worse here, since a hand name is
        typically longer than a minion's. A plain list reads fine at any
        width instead."""
        if self._compact_board:
            return self._build_hand_list(cards)
        return self._build_hand_flowbox(cards)

    def _build_hand_list(self, cards: list[HandCard]) -> Gtk.Grid:
        # Same reasoning as `_build_board_compact`'s Grid: a mana-cost
        # column (when resolvable) should sit at one consistent horizontal
        # position, not directly after each card's own differently-long
        # name.
        grid = Gtk.Grid()
        grid.set_row_spacing(4)
        grid.set_column_spacing(8)
        for row_index, card in enumerate(cards):
            name_label = Gtk.Label(label=card.name, xalign=0)
            name_label.set_wrap(True)
            grid.attach(name_label, 0, row_index, 1, 1)

            cost = self._hand_card_cost(card.card_id)
            if cost is not None:
                cost_label = Gtk.Label(label=str(cost), xalign=1)
                cost_label.add_css_class("dim-label")
                grid.attach(cost_label, 1, row_index, 1, 1)

            self._attach_card_tooltip(name_label, card.card_id)
        return grid

    def _hand_card_cost(self, card_id: str) -> int | None:
        # Printed base cost only (no entity-specific cost-reduction tags,
        # unlike the board's `script_data_num_1` variant text) -- the same
        # tradeoff `_attach_card_tooltip`'s own cost line already makes for
        # a hand card, just surfaced as its own column here instead of
        # buried in the tooltip.
        if not card_id:
            return None
        return card_info(card_id, self._card_db).cost

    def _build_hand_flowbox(self, cards: list[HandCard]) -> Gtk.FlowBox:
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_min_children_per_line(1)
        flow.set_max_children_per_line(10)
        for card in cards:
            flow.append(self._build_hand_chip(card.name, card.card_id))
        return flow

    def _build_hand_chip(self, text: str, card_id: str) -> Gtk.Frame:
        # Same chip shape as a keyword badge, but visually quieter (lower
        # opacity, no separate weight class) than a board minion's frame --
        # hand cards are reference information here, not the thing being
        # actively evaluated the way board state is.
        frame = TrackerWindow._build_keyword_chip(text)
        frame.set_opacity(0.8)
        self._attach_card_tooltip(frame, card_id)
        return frame

    @staticmethod
    def _build_keyword_chip(text: str) -> Gtk.Frame:
        label = Gtk.Label(label=text)
        label.add_css_class("caption")
        label.set_margin_top(0)
        label.set_margin_bottom(0)
        label.set_margin_start(4)
        label.set_margin_end(4)
        frame = Gtk.Frame()
        frame.set_child(label)
        # Same reasoning as the minion frame's own halign: a FlowBox (hand
        # chips) or box (keyword row) would otherwise stretch this to fill
        # leftover space in a sparse row/hand instead of sizing to content.
        frame.set_halign(Gtk.Align.START)
        return frame

    @staticmethod
    def _build_keyword_row(
        keywords: list[str], *, halign: Gtk.Align = Gtk.Align.START
    ) -> Gtk.Box | None:
        """Splits a minion's keywords into real, printed card keywords
        (Spott, Gottesschild, ...), shown as bordered chips, and temporary
        attack-readiness state (`_STATE_KEYWORDS` -- "bereit"/"nur
        Diener"), shown as plain dim text instead. User feedback on a real
        screenshot: as identical bordered chips, "bereit" read as equally
        important as "Spott" even though it isn't a fact about the card,
        just this instant's board state.
        """
        if not keywords:
            return None
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        row.set_halign(halign)
        state_labels = []
        for keyword in keywords:
            chip_text = _KEYWORD_CHIP_LABELS.get(keyword, keyword)
            if keyword in _STATE_KEYWORDS:
                state_labels.append(chip_text)
            else:
                row.append(TrackerWindow._build_keyword_chip(chip_text))
        if state_labels:
            state_label = Gtk.Label(label=" · ".join(state_labels))
            state_label.add_css_class("caption")
            state_label.add_css_class("dim-label")
            row.append(state_label)
        return row

    def _attach_card_tooltip(
        self, widget: Gtk.Widget, card_id: str, script_data_num_1: int = 0
    ) -> None:
        """Hover shows a native GTK tooltip (built-in delay/positioning/
        dismiss); click pins the same content open in a popover (GTK's
        default `autohide` closes it on an outside click, matching "click
        elsewhere to dismiss" for free). Opponent hand cards never call
        this at all -- they're rendered as a bare count, never as
        per-card chips, so there's nothing to attach a tooltip to."""
        if not card_id:
            return
        cache_key = (card_id, script_data_num_1)
        markup = self._card_tooltip_markup_cache.get(cache_key)
        if markup is None:
            info = card_info(card_id, self._card_db, script_data_num_1=script_data_num_1)
            markup = _card_tooltip_markup(info)
            self._card_tooltip_markup_cache[cache_key] = markup
        widget.set_tooltip_markup(markup)

        label = Gtk.Label(label=markup, use_markup=True, wrap=True, xalign=0)
        # Narrower than before (40) -- user feedback: "eher schmaler und
        # höher als breit und gedrungen" now that the header is its own
        # multi-line block rather than one long comma-joined line, a
        # narrower width no longer forces awkward mid-fact wrapping.
        label.set_max_width_chars(30)
        for setter in (
            label.set_margin_top,
            label.set_margin_bottom,
            label.set_margin_start,
            label.set_margin_end,
        ):
            setter(8)
        popover = Gtk.Popover()
        popover.set_child(label)
        popover.set_parent(widget)
        popover.set_autohide(True)
        # `set_parent` doesn't make GTK unparent the popover automatically
        # when `widget` itself is torn down (e.g. `_clear_box` rebuilding
        # the board every turn navigation) -- without this, GTK warns
        # "Finalizing GtkFrame ... but it still has children left" on
        # every single re-render.
        widget.connect("destroy", lambda _widget: popover.unparent())
        click = Gtk.GestureClick()
        click.connect("released", lambda *_args: popover.popup())
        widget.add_controller(click)

    @staticmethod
    def _clear_box(box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            box.remove(child)
            child = next_child

    def _poll(self) -> bool:
        """Check the current Power.log for updates and refresh the UI.

        Deviation from the plan's `ui.py` sketch: rather than driving this
        off `LogWatcher.poll()` (a line-by-line tailing API built for
        incremental consumption), this re-parses the whole current
        Power.log via `parse_log()` whenever its mtime changes. `parse_log`
        always re-derives a full `ParsedGame` from scratch, so feeding it
        individual lines would gain nothing -- and Power.log is a small
        text file, so re-parsing it every 2s is cheap. This also sidesteps
        `LogWatcher`'s offset-tracking, which is designed to avoid
        re-reading lines already seen, not to detect "has this file
        changed since I last looked".

        The whole body is wrapped in a broad `except Exception`: PyGObject
        permanently stops a `GLib.timeout_add_seconds` source if its
        callback raises, so an uncaught error here (e.g. `hslog` choking on
        a torn/concurrent read of a growing Power.log, or a failed export
        due to a bad `export_dir`) would otherwise silently freeze the UI
        with stale data forever, with no visible error for a user who
        launched via the .desktop entry (no attached terminal).
        """
        try:
            return self._poll_once()
        except Exception as exc:  # noqa: BLE001 - must never kill the poll loop
            print(f"hearthtrace: error while polling log: {exc}", file=sys.stderr)
            self._render_tracker_error("Fehler beim Lesen des Logs — siehe Terminal")
            return True  # keep polling so a transient condition can recover

    def _poll_once(self) -> bool:
        log_path = find_latest_power_log(self._config.logs_dir)
        if log_path is None or not log_path.exists():
            self._render_tracker_empty("HearthTrace wartet auf das nächste Hearthstone-Spiel.")
            return True

        mtime = log_path.stat().st_mtime
        if log_path == self._last_log_path and mtime == self._last_log_mtime:
            return True  # no change since last poll

        try:
            # Accepted MVP simplification: this re-parses the entire session
            # Power.log from scratch on every tick, which could grow more
            # expensive over a very long play session -- not redesigned now.
            game = parse_log(log_path)
        except NoGameFoundError:
            # A stable, expected state (no CREATE_GAME yet) -- fine to
            # remember this mtime, since re-parsing unchanged content
            # would only give the same result again.
            self._last_log_path = log_path
            self._last_log_mtime = mtime
            self._render_tracker_empty("HearthTrace wartet auf das nächste Hearthstone-Spiel.")
            return True

        # Only remember this mtime as "handled" once *everything* this tick
        # does with `game` has actually succeeded -- parsing, refreshing
        # the deck list, and exporting. A transient failure anywhere in
        # that chain (e.g. hslog choking on a torn read, or `_maybe_export`
        # failing to write to a suddenly-unwritable export dir) is reported
        # by `_poll`'s broad `except Exception`, but if this method had
        # already updated `_last_log_mtime` before that, the very next
        # tick's early-return above would skip retrying entirely until the
        # file changes again -- for an already-finished match sitting in
        # the newest log file, that can mean never, silently dropping the
        # export for good.
        self._render_tracker_match(game)
        self._maybe_export(game, log_path)
        self._update_replay_state(game)
        self._last_log_path = log_path
        self._last_log_mtime = mtime
        return True

    def _render_tracker_empty(self, description: str) -> None:
        self._clear_box(self._tracker_box)
        status_page = Adw.StatusPage(title="Keine laufende Partie", description=description)
        status_page.set_vexpand(True)
        self._tracker_box.append(status_page)

    def _render_tracker_error(self, message: str) -> None:
        self._clear_box(self._tracker_box)
        status_page = Adw.StatusPage(title="Fehler", description=message)
        status_page.set_vexpand(True)
        self._tracker_box.append(status_page)

    def _render_tracker_match(self, game: ParsedGame) -> None:
        """Render the Live page's one "Aktuelle Partie" status card, plus
        state-specific content below it: an in-progress match shows live
        health/deck state below the card; a finished one puts its result
        and a way into Replay directly in the card, with nothing else
        below -- an empty box (the old behaviour) never explained *why*
        nothing else was shown, which read as unfinished rather than
        intentionally minimal.
        """
        self._clear_box(self._tracker_box)
        finished = game.result in _FINISHED_RESULTS
        own_class = _CLASS_LABELS.get(game.own_class, game.own_class)
        opponent_class = _CLASS_LABELS.get(game.opponent_class, game.opponent_class)
        turn_number = game.turns[-1].number if game.turns else 0

        if finished:
            detail_text = f"{turn_number} Züge"
        else:
            detail_text = f"Zug {turn_number}"
        if game.log_truncated and not finished:
            # Hearthstone itself stopped writing to Power.log once it hit
            # its 10MB size limit -- `game.result` is whatever it last was
            # before that, not the match's true (possibly already decided)
            # current state. Showing it bare would be actively misleading.
            detail_text += " (Hearthstone-Log abgeschnitten — Status evtl. veraltet)"
        result = game.result if finished else None
        status_card = self._build_match_status_card(own_class, opponent_class, detail_text, result)
        self._tracker_box.append(status_card)

        if not finished and game.turns:
            self._tracker_box.append(self._build_in_progress_match_body(game))

    def _build_match_status_card(
        self, own_class: str, opponent_class: str, detail_text: str, result: str | None
    ) -> Gtk.Box:
        # "Aktuelle Partie" only while Hearthstone is actually still
        # running this match -- user feedback on a real screenshot: once
        # it's over (`result` is set), the card is showing a finished
        # result, not something ongoing, and "Letzte Partie" says that.
        heading_text = "Aktuelle Partie" if result is None else "Letzte Partie"
        heading = Gtk.Label(label=heading_text, xalign=0)
        heading.add_css_class("caption")
        heading.add_css_class("dim-label")

        matchup = Gtk.Label(label=f"{own_class} vs. {opponent_class}", xalign=0)
        matchup.add_css_class("title-4")

        detail_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        if result is not None:
            dot = Gtk.Label(label="●")
            dot.add_css_class(_RESULT_DOT_CSS_CLASS.get(result, "dim-label"))
            detail_row.append(dot)
            detail_text = f"{RESULT_LABELS.get(result, result)} · {detail_text}"
        detail = Gtk.Label(label=detail_text, xalign=0)
        detail.add_css_class("dim-label")
        detail_row.append(detail)

        text_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        text_column.append(heading)
        text_column.append(matchup)
        text_column.append(detail_row)

        replay_button = None
        if result is not None:
            replay_button = Gtk.Button(label="Replay ansehen")
            replay_button.connect("clicked", self._on_view_finished_replay)

        # User feedback on a real screenshot: at this window's normal
        # (narrow) width, the title, result and "Replay ansehen" button
        # all competed for the same horizontal line and the button ran
        # off the window's edge. Stacking the button below the summary
        # instead -- full width, easy to hit -- rather than squeezing it
        # into that one crowded row.
        if self._compact_board:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            card.add_css_class("card")
            for setter in (
                card.set_margin_top,
                card.set_margin_bottom,
                card.set_margin_start,
                card.set_margin_end,
            ):
                setter(12)
            card.append(text_column)
            if replay_button is not None:
                replay_button.set_hexpand(True)
                card.append(replay_button)
            return card

        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        card.add_css_class("card")
        text_column.set_margin_top(12)
        text_column.set_margin_bottom(12)
        text_column.set_margin_start(12)
        text_column.set_margin_end(12)
        text_column.set_hexpand(True)
        card.append(text_column)
        if replay_button is not None:
            replay_button.set_valign(Gtk.Align.END)
            replay_button.set_margin_end(12)
            replay_button.set_margin_bottom(12)
            card.append(replay_button)
        return card

    def _on_view_finished_replay(self, _button: Gtk.Button) -> None:
        # `_update_replay_state` already keeps `_replay_game` following the
        # live match (unless pinned to a past one from Verlauf, which this
        # button is never reachable from) -- nothing to set up beyond
        # switching pages.
        self._view_group.set_active_name("replay")

    def _build_in_progress_match_body(self, game: ParsedGame) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        life = game.turns[-1].end.life
        life_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        life_box.append(self._build_life_row("Du", life.own_health, life.own_armor))
        life_box.append(self._build_life_row("Gegner", life.opponent_health, life.opponent_armor))
        box.append(life_box)

        deck = remaining_deck(game)
        deck_caption = Gtk.Label(label=f"Deck — {len(deck)} Karten verbleibend", xalign=0)
        deck_caption.add_css_class("caption")
        deck_caption.add_css_class("dim-label")
        box.append(deck_caption)

        deck_list = Gtk.ListBox()
        deck_list.add_css_class("boxed-list")
        for card_id in deck:
            card = self._card_db.get(card_id)
            card_name = card.name if card else card_id
            deck_list.append(Gtk.Label(label=card_name, xalign=0, margin_top=4, margin_bottom=4))
        box.append(deck_list)
        return box

    @staticmethod
    def _build_life_row(name: str, health: int, armor: int) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_label = Gtk.Label(label=name, xalign=0)
        name_label.set_hexpand(True)
        row.append(name_label)
        text = f"{health} Leben"
        if armor:
            text += f" (+{armor} Rüstung)"
        row.append(Gtk.Label(label=text, xalign=1))
        return row

    def _maybe_export(self, game: ParsedGame, log_path: Path) -> None:
        """Export a Markdown summary once per finished match.

        A match is "finished" only once its result is one of the terminal
        `_FINISHED_RESULTS`. `PlayState` also has non-terminal, truthy
        values (PLAYING, WINNING, LOSING, DISCONNECTED) that are not
        "UNKNOWN" either -- exporting on those would produce a premature
        summary for a match still in progress.

        `_last_exported_key` is a dedup key keyed on `game.game_index`
        (rather than `game.result`) because a single session's Power.log
        can contain multiple matches (parser.games grows with each
        CREATE_GAME) -- keying on result alone would silently skip
        exporting a second match that happens to end the same way as the
        previous one (e.g. two wins in a row). Since `parse_log` re-parses
        the whole file every tick, an already-finished match would
        otherwise also be re-exported on every subsequent poll while the
        same log file is still the newest one on disk.

        `log_path` is taken as a parameter (the same value `_poll_once`
        just computed), not read back from `self._last_log_path` --
        `_poll_once` now only commits that once this whole call has
        already returned, so reading it here would still see the
        *previous* tick's value (or `None` on the very first tick),
        producing a wrong key that can never match a future genuine repeat
        and silently defeats the dedup entirely.
        """
        if game.result not in _FINISHED_RESULTS:
            return
        export_key = f"{log_path}:{game.game_index}"
        if export_key == self._last_exported_key:
            return
        export_path = export_match_summary(game, export_dir=self._config.export_dir)
        # Best-effort: a failure here must not stop the export itself from
        # being marked done (see below) -- losing the Verlauf-to-Replay
        # link for one match is far less bad than re-exporting it forever
        # because this raised before `_save_last_exported_key` ran.
        try:
            record_replay_source(_STATE_DIR, export_path, log_path, game.game_index)
        except OSError as exc:
            print(f"hearthtrace: could not record replay source: {exc}", file=sys.stderr)
        # Marker saved to disk *before* the in-memory key is updated: if
        # `_save_last_exported_key` itself throws (e.g. the state dir just
        # became unwritable), the in-memory key must stay unset too, or a
        # later restart would read back the still-stale on-disk marker and
        # re-export the same match again -- the exact bug this exists to
        # prevent. Same reasoning as `_poll_once`'s mtime handling more
        # generally: nothing is marked "already done" until the write that
        # makes it durable has actually succeeded.
        self._save_last_exported_key(export_key)
        self._last_exported_key = export_key
        if self._stack.get_visible_child_name() == "history":
            self._refresh_history_list()

    @staticmethod
    def _last_export_marker_path() -> Path:
        return _STATE_DIR / "last_export"

    def _load_last_exported_key(self) -> str | None:
        # Any read failure (missing file, no permission, a directory
        # somehow sitting at that path, ...) is treated the same as "no
        # marker known" -- this is a best-effort dedup hint, not something
        # that should be able to crash startup. `TrackerWindow.__init__`
        # has no surrounding try/except (unlike the ConfigError path in
        # app.py), so an uncaught exception here would take the whole app
        # down before a window ever appears.
        try:
            return self._last_export_marker_path().read_text().strip() or None
        except OSError:
            return None

    def _save_last_exported_key(self, export_key: str) -> None:
        marker = self._last_export_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(export_key)
