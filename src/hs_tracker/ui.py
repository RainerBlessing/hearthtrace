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

# Dedicated app-state location for the export dedup marker (see
# `_last_export_marker_path`) -- deliberately *not* inside `export_dir`,
# which is a user-facing folder whose only documented purpose is holding
# human-readable match exports. A user archiving or clearing that folder
# (a reasonable thing to do with "my exports") must not silently resurrect
# the re-export-on-restart bug this marker exists to prevent. Hardcoded
# rather than reading `$XDG_STATE_HOME`, matching `app.py`'s equally
# hardcoded `CONFIG_PATH` -- no other part of this project reads XDG env
# vars, so doing it only here would be inconsistent for no real benefit.
_STATE_DIR = Path.home() / ".local" / "state" / "hs-tracker"


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
        try:
            entries = load_match_history(self._config.export_dir)
        except Exception as exc:  # noqa: BLE001 - must never break the toggle
            print(f"hs-tracker: error while loading match history: {exc}", file=sys.stderr)
            self._history_list.append(Gtk.Label(label="Fehler beim Laden des Verlaufs", xalign=0))
            return
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
        self._refresh_deck_list(game)
        self._maybe_export(game, log_path)
        self._last_log_path = log_path
        self._last_log_mtime = mtime
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
        export_match_summary(game, export_dir=self._config.export_dir)
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
