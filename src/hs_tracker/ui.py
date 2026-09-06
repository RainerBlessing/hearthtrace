"""The live deck-list window."""

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from hs_tracker.config import Config  # noqa: E402
from hs_tracker.deck_state import remaining_deck  # noqa: E402
from hs_tracker.log_reader import find_latest_power_log  # noqa: E402
from hs_tracker.markdown_export import export_match_summary  # noqa: E402
from hs_tracker.parser import NoGameFoundError, ParsedGame, parse_log  # noqa: E402


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
        self._last_exported_result: str | None = None

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(Adw.HeaderBar())

        self._status_label = Gtk.Label(label="Warte auf Hearthstone …")
        self._deck_list = Gtk.ListBox()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(self._status_label)
        box.append(self._deck_list)
        toolbar_view.set_content(box)
        self.set_content(toolbar_view)

        GLib.timeout_add_seconds(2, self._poll)

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
        """
        log_path = find_latest_power_log(self._config.logs_dir)
        if log_path is None or not log_path.exists():
            self._status_label.set_label("Warte auf Hearthstone …")
            return True

        mtime = log_path.stat().st_mtime
        if log_path == self._last_log_path and mtime == self._last_log_mtime:
            return True  # no change since last poll
        self._last_log_path = log_path
        self._last_log_mtime = mtime

        try:
            game = parse_log(log_path)
        except NoGameFoundError:
            self._status_label.set_label("Warte auf Hearthstone …")
            return True

        self._refresh_deck_list(game)
        self._maybe_export(game)
        return True

    def _refresh_deck_list(self, game: ParsedGame) -> None:
        self._status_label.set_label(
            f"{game.own_class} vs. {game.opponent_class} — Ergebnis: {game.result}"
        )
        while (row := self._deck_list.get_row_at_index(0)) is not None:
            self._deck_list.remove(row)
        for card_id in remaining_deck(game):
            self._deck_list.append(Gtk.Label(label=card_id, xalign=0))

    def _maybe_export(self, game: ParsedGame) -> None:
        """Export a Markdown summary once per finished match.

        A match is "finished" when its result is a known terminal state.
        `_last_exported_result` is a coarse dedup key: since `parse_log`
        re-parses the whole file every tick, an already-finished match
        would otherwise be re-exported on every subsequent poll while the
        same log file is still the newest one on disk.
        """
        if game.result == "UNKNOWN":
            return
        export_key = f"{self._last_log_path}:{game.result}"
        if export_key == self._last_exported_result:
            return
        self._last_exported_result = export_key
        export_match_summary(game, export_dir=self._config.export_dir)
