"""Application entry point."""

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw  # noqa: E402

from hs_tracker.config import ConfigError, load_config  # noqa: E402
from hs_tracker.ui import TrackerWindow  # noqa: E402

CONFIG_PATH = Path.home() / ".config" / "hs-tracker" / "config.toml"


def _show_config_error_dialog(app: Adw.Application, message: str) -> None:
    """Show a minimal, dismissible dialog reporting a config error.

    Needed because a user launching via `hs-tracker.desktop` has no
    attached terminal, so the `print(e)` alone (kept for anyone running
    from a terminal) would otherwise be invisible -- the app would just
    quit silently. A bare `Adw.AlertDialog` can't be presented without a
    parent widget, so a tiny throwaway window is created to host it; both
    are closed and the app quit once the user dismisses the dialog.
    """
    host = Adw.ApplicationWindow(application=app, title="HS Tracker")
    host.set_default_size(1, 1)
    host.present()

    dialog = Adw.AlertDialog(
        heading="Konfigurationsfehler",
        body=f"Konfiguration fehlt/unvollständig: {message}. "
        "Bitte ~/.config/hs-tracker/config.toml bearbeiten.",
    )
    dialog.add_response("ok", "OK")
    dialog.set_default_response("ok")
    dialog.set_close_response("ok")
    dialog.connect("response", lambda _d, _r: app.quit())
    dialog.present(host)


def main() -> int:
    app = Adw.Application(application_id="dev.rainer.hs-tracker")

    def on_activate(app: Adw.Application) -> None:
        try:
            config = load_config(CONFIG_PATH)
        except ConfigError as e:
            print(e)
            _show_config_error_dialog(app, str(e))
            return
        win = TrackerWindow(app, config)
        win.present()

    app.connect("activate", on_activate)
    return app.run(None)  # type: ignore[no-any-return]


if __name__ == "__main__":
    raise SystemExit(main())
