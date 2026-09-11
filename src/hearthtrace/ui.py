"""The live deck-list window."""

import sys
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
    HandCard,
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
# Markdown export). Only "kann angreifen" has a shorter, chip-friendly
# alternative worth using; every other keyword is already short.
_KEYWORD_CHIP_LABELS = {"kann angreifen": "bereit"}

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

# Semantic Adwaita color classes for the match-status card's result dot --
# "success"/"warning"/"error" are stock libadwaita style classes (like
# "accent"/"dim-label" elsewhere in this file), not custom CSS. A tie is
# deliberately left uncolored (neither a win nor a loss).
_RESULT_DOT_CSS_CLASS = {
    "WON": "success",
    "LOST": "error",
    "CONCEDED": "error",  # the friendly player's own PLAYSTATE -- a loss
}

# Adw.ToggleGroup's own vertical padding (confirmed via its real CSS node,
# `toggle-group` containing `toggle` children -- inspected directly rather
# than guessed) reads as noticeably tall against this window's small
# fixed size. Trimmed here rather than left at the library default; safe
# to fail silently (an unmatched/ineffective rule just does nothing) if a
# future libadwaita version restructures this internally.
_REPLAY_CSS = "toggle-group toggle { padding-top: 2px; padding-bottom: 2px; }"


def _install_replay_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_string(_REPLAY_CSS)
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )


def _card_tooltip_markup(info: CardInfo) -> str:
    # `info.text` is already sanitized into valid Pango markup (its own
    # <b>/<i> tags are meant to render, not show up literally) -- every
    # other field here is plain text and must be escaped, or a card name
    # or keyword that happens to contain "&"/"<" would break the whole
    # tooltip instead of just that one field.
    lines = [f"<b>{escape(info.name)}</b>"]
    stats = []
    if info.cost is not None:
        stats.append(f"{info.cost} Mana")
    if info.attack is not None and info.health is not None:
        stats.append(f"{info.attack}/{info.health}")
    elif info.health is not None:  # a Location's durability -- no attack value
        stats.append(str(info.health))
    if info.type_label:
        stats.append(info.type_label)
    if info.rarity_label:
        stats.append(info.rarity_label)
    if stats:
        lines.append(escape("   ".join(stats)))
    if info.keywords:
        lines.append(escape(", ".join(info.keywords)))
    if info.text:
        lines.append(info.text)
    return "\n".join(lines)


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
        self._card_tooltip_markup_cache: dict[str, str] = {}
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
        self._replay_position_label.add_css_class("dim-label")
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
        except (NoGameFoundError, OSError) as exc:
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
            self._replay_turn_label.set_label("Keine Züge verfügbar")
            self._replay_position_label.set_label("")
            self._replay_prev_button.set_sensitive(False)
            self._replay_next_button.set_sensitive(False)
            self._clear_box(self._replay_content_box)
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
        box.append(self._build_board_flowbox(snapshot.board.opponent))
        box.append(Gtk.Separator())

        own_header = self._build_side_header(
            "DU", self._format_side_detail(snapshot.life.own_health, own_mana, None), accent=True
        )
        own_header.set_margin_top(8)
        box.append(own_header)
        box.append(self._build_board_flowbox(snapshot.board.own))

        hand_caption = Gtk.Label(label="Hand", xalign=0)
        hand_caption.add_css_class("caption")
        hand_caption.add_css_class("dim-label")
        hand_caption.set_margin_top(4)
        box.append(hand_caption)
        if snapshot.hand.own_cards:
            box.append(self._build_hand_flowbox(snapshot.hand.own_cards))
        else:
            empty_hand = Gtk.Label(label="(leer)", xalign=0)
            empty_hand.add_css_class("dim-label")
            box.append(empty_hand)

    @staticmethod
    def _format_side_detail(hp: int, mana: str | None, hand_count: int | None) -> str:
        # Fixed labels ("Mana", "Hand") for every segment except HP's own
        # heart glyph, joined with one consistent separator -- so the line
        # reads as a small table of facts, not a string of differently-
        # shaped tokens.
        parts = [f"♥ {hp}"]
        if mana is not None:
            parts.append(mana)
        if hand_count is not None:
            parts.append(f"Hand {hand_count}")
        return "   ".join(parts)

    @staticmethod
    def _build_side_header(name: str, detail: str, *, accent: bool) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_label = Gtk.Label(label=name, xalign=0)
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
        return row

    def _render_replay_actions(self, turn: Turn) -> None:
        box = self._replay_content_box
        if not turn.actions:
            placeholder = Gtk.Label(label="(keine Aktionen diesen Zug)", xalign=0)
            placeholder.add_css_class("dim-label")
            box.append(placeholder)
            return
        for index, action in enumerate(turn.actions, start=1):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.set_valign(Gtk.Align.START)
            # Explicitly override this row's own expand flag rather than
            # leaving it computed: without this, the connector's own
            # vexpand below would propagate all the way up through row
            # into `_replay_content_box`, which is exactly the "stretches
            # across the whole page" bug fixed previously. Setting it here
            # stops that propagation at the row boundary, so vexpand can
            # still do its actual job -- filling *this row's own* height,
            # which is however tall its `content` column naturally is --
            # without also inflating the page.
            row.set_vexpand(False)

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
            # height (vexpand, but scoped to this row only -- see above),
            # not a fixed guess, so a multi-effect action's longer text
            # doesn't leave the line stopping short of the next number.
            badge_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            badge_column.set_valign(Gtk.Align.FILL)
            badge_column.append(number_frame)
            if index < len(turn.actions):
                connector = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
                # A vertical Box's child defaults to filling the column's
                # full (perpendicular) width -- without an explicit narrow
                # width and centered halign, this renders as a solid gray
                # block as wide as the badge circle above it, not a thin
                # connecting line.
                connector.set_size_request(2, -1)
                connector.set_halign(Gtk.Align.CENTER)
                connector.set_vexpand(True)
                connector.set_margin_top(4)
                connector.set_margin_bottom(4)
                badge_column.append(connector)

            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            headline_label = Gtk.Label(label=action.headline, xalign=0)
            headline_label.set_wrap(True)
            content.append(headline_label)
            for effect in action.effects:
                effect_label = Gtk.Label(label=effect, xalign=0)
                effect_label.set_wrap(True)
                effect_label.add_css_class("caption")
                # A tick lighter than the theme's own ".dim-label" (which
                # reads as too dark for a secondary line at this size) --
                # a plain opacity reduction instead of overriding that
                # semantic class app-wide.
                effect_label.set_opacity(0.75)
                effect_label.set_margin_start(8)
                content.append(effect_label)

            row.append(badge_column)
            row.append(content)
            box.append(row)

    def _build_board_flowbox(self, minions: list[MinionState]) -> Gtk.FlowBox:
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_min_children_per_line(1)
        flow.set_max_children_per_line(7)  # Hearthstone's own board-size cap
        for minion in minions:
            flow.append(self._build_minion_frame(minion))
        return flow

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

        if minion.keywords:
            chip_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
            chip_row.set_halign(Gtk.Align.CENTER)
            for keyword in minion.keywords:
                chip_text = _KEYWORD_CHIP_LABELS.get(keyword, keyword)
                chip_row.append(TrackerWindow._build_keyword_chip(chip_text))
            content.append(chip_row)

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
        self._attach_card_tooltip(frame, minion.card_id)
        return frame

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

    def _attach_card_tooltip(self, widget: Gtk.Widget, card_id: str) -> None:
        """Hover shows a native GTK tooltip (built-in delay/positioning/
        dismiss); click pins the same content open in a popover (GTK's
        default `autohide` closes it on an outside click, matching "click
        elsewhere to dismiss" for free). Opponent hand cards never call
        this at all -- they're rendered as a bare count, never as
        per-card chips, so there's nothing to attach a tooltip to."""
        if not card_id:
            return
        markup = self._card_tooltip_markup_cache.get(card_id)
        if markup is None:
            markup = _card_tooltip_markup(card_info(card_id, self._card_db))
            self._card_tooltip_markup_cache[card_id] = markup
        widget.set_tooltip_markup(markup)

        label = Gtk.Label(label=markup, use_markup=True, wrap=True)
        label.set_max_width_chars(40)
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
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        card.add_css_class("card")

        text_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        text_column.set_margin_top(12)
        text_column.set_margin_bottom(12)
        text_column.set_margin_start(12)
        text_column.set_margin_end(12)
        text_column.set_hexpand(True)
        card.append(text_column)

        heading = Gtk.Label(label="Aktuelle Partie", xalign=0)
        heading.add_css_class("caption")
        heading.add_css_class("dim-label")
        text_column.append(heading)

        matchup = Gtk.Label(label=f"{own_class} vs. {opponent_class}", xalign=0)
        matchup.add_css_class("title-4")
        text_column.append(matchup)

        detail_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        if result is not None:
            dot = Gtk.Label(label="●")
            dot.add_css_class(_RESULT_DOT_CSS_CLASS.get(result, "dim-label"))
            detail_row.append(dot)
            detail_text = f"{RESULT_LABELS.get(result, result)} · {detail_text}"
        detail = Gtk.Label(label=detail_text, xalign=0)
        detail.add_css_class("dim-label")
        detail_row.append(detail)
        text_column.append(detail_row)

        if result is not None:
            replay_button = Gtk.Button(label="Replay ansehen")
            replay_button.set_valign(Gtk.Align.END)
            replay_button.set_margin_end(12)
            replay_button.set_margin_bottom(12)
            replay_button.connect("clicked", self._on_view_finished_replay)
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
