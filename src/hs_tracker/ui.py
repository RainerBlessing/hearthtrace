"""The live deck-list window."""

import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402
from hearthstone.cardxml import load as load_cards  # noqa: E402

from hs_tracker.config import Config  # noqa: E402
from hs_tracker.deck_state import remaining_deck  # noqa: E402
from hs_tracker.log_reader import find_latest_power_log  # noqa: E402
from hs_tracker.markdown_export import export_match_summary  # noqa: E402
from hs_tracker.match_history import format_history_row, load_match_history  # noqa: E402
from hs_tracker.parser import NoGameFoundError, ParsedGame, parse_log  # noqa: E402

# Terminal `PlayState` values (mirrors `markdown_export.RESULT_LABELS`'
# terminal set). Anything else -- PLAYING, WINNING, LOSING, DISCONNECTED,
# UNKNOWN, INVALID -- means the match is still in progress and must not
# trigger an export yet.
_FINISHED_RESULTS = {"WON", "LOST", "TIED", "CONCEDED"}


class TrackerWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, config: Config) -> None:
        super().__init__(application=app, title="HS Tracker")
        self.set_default_size(320, 480)
        # Deviation from the plan: the plan's sketch called a non-existent
        # `set_widget_name` (AttributeError against the installed GTK4
        # bindings). `Gtk.Widget.set_name` is the real API for a stable CSS
        # node name; the actual Wayland/Hyprland window class comes from
        # the application id passed to `Adw.Application` in app.py.
        self.set_name("hs-tracker")

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
        self._card_db, _ = load_cards()

        header_bar = Adw.HeaderBar()
        history_toggle = Gtk.ToggleButton(label="Verlauf")
        history_toggle.connect("toggled", self._on_history_toggled)
        header_bar.pack_end(history_toggle)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header_bar)

        self._status_label = Gtk.Label(label="Warte auf Hearthstone …")
        self._deck_list = Gtk.ListBox()

        tracker_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        tracker_box.append(self._status_label)
        tracker_box.append(self._deck_list)

        self._history_list = Gtk.ListBox()
        history_scroller = Gtk.ScrolledWindow()
        history_scroller.set_child(self._history_list)

        # A plain Gtk.Stack switched by one header-bar toggle button rather
        # than Adw.ViewStack/ViewSwitcher: this window is only 320px wide,
        # too narrow for the switcher chrome to look right, and there are
        # only ever these two views.
        self._stack = Gtk.Stack()
        self._stack.add_named(tracker_box, "tracker")
        self._stack.add_named(history_scroller, "history")
        toolbar_view.set_content(self._stack)
        self.set_content(toolbar_view)

        GLib.timeout_add_seconds(2, self._poll)

    def _on_history_toggled(self, button: Gtk.ToggleButton) -> None:
        if button.get_active():
            self._refresh_history_list()
            self._stack.set_visible_child_name("history")
        else:
            self._stack.set_visible_child_name("tracker")

    def _refresh_history_list(self) -> None:
        while (row := self._history_list.get_row_at_index(0)) is not None:
            self._history_list.remove(row)
        entries = load_match_history(self._config.export_dir)
        if not entries:
            self._history_list.append(Gtk.Label(label="Noch keine Partien gespeichert", xalign=0))
            return
        for entry in entries:
            self._history_list.append(Gtk.Label(label=format_history_row(entry), xalign=0))

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
            print(f"hs-tracker: error while polling log: {exc}", file=sys.stderr)
            self._status_label.set_label("Fehler beim Lesen des Logs — siehe Terminal")
            return True  # keep polling so a transient condition can recover

    def _poll_once(self) -> bool:
        log_path = find_latest_power_log(self._config.logs_dir)
        if log_path is None or not log_path.exists():
            self._status_label.set_label("Warte auf Hearthstone …")
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
            self._status_label.set_label("Warte auf Hearthstone …")
            return True

        # Only remember this mtime as "handled" once parsing actually
        # succeeded. A transient failure (e.g. hslog choking on a torn
        # read while Hearthstone is mid-write) is reported by `_poll`'s
        # broad `except Exception` -- but if this method had already
        # updated `_last_log_mtime` before that, the very next tick's
        # early-return above would skip retrying entirely until the file
        # changes again, potentially getting stuck on a stale error and
        # missing the match's true final state.
        self._last_log_path = log_path
        self._last_log_mtime = mtime
        self._refresh_deck_list(game)
        self._maybe_export(game)
        return True

    def _refresh_deck_list(self, game: ParsedGame) -> None:
        status = f"{game.own_class} vs. {game.opponent_class} — Ergebnis: {game.result}"
        if game.log_truncated and game.result not in _FINISHED_RESULTS:
            # Hearthstone itself stopped writing to Power.log once it hit
            # its 10MB size limit -- `game.result` is whatever it last was
            # before that, not the match's true (possibly already decided)
            # current state. Showing it bare would be actively misleading.
            status += " (Hearthstone-Log abgeschnitten — Status evtl. veraltet)"
        self._status_label.set_label(status)
        while (row := self._deck_list.get_row_at_index(0)) is not None:
            self._deck_list.remove(row)
        for card_id in remaining_deck(game):
            card = self._card_db.get(card_id)
            card_name = card.name if card else card_id
            self._deck_list.append(Gtk.Label(label=card_name, xalign=0))

    def _maybe_export(self, game: ParsedGame) -> None:
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
        """
        if game.result not in _FINISHED_RESULTS:
            return
        export_key = f"{self._last_log_path}:{game.game_index}"
        if export_key == self._last_exported_key:
            return
        export_match_summary(game, export_dir=self._config.export_dir)
        # Only recorded (in memory and on disk) once the export actually
        # succeeded -- same reasoning as `_poll_once`'s mtime handling: a
        # failed export (e.g. a full disk) must not be silently treated as
        # "already done", or it would never be retried.
        self._last_exported_key = export_key
        self._save_last_exported_key(export_key)
        if self._stack.get_visible_child_name() == "history":
            self._refresh_history_list()

    def _last_export_marker_path(self) -> Path:
        return self._config.export_dir / ".last_export"

    def _load_last_exported_key(self) -> str | None:
        try:
            return self._last_export_marker_path().read_text().strip() or None
        except FileNotFoundError:
            return None

    def _save_last_exported_key(self, export_key: str) -> None:
        marker = self._last_export_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(export_key)
