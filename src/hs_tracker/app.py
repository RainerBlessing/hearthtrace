"""Application entry point."""

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw  # noqa: E402

from hs_tracker.config import ConfigError, load_config  # noqa: E402
from hs_tracker.ui import TrackerWindow  # noqa: E402

CONFIG_PATH = Path.home() / ".config" / "hs-tracker" / "config.toml"


def main() -> int:
    app = Adw.Application(application_id="dev.rainer.hs-tracker")

    def on_activate(app: Adw.Application) -> None:
        try:
            config = load_config(CONFIG_PATH)
        except ConfigError as e:
            print(e)
            app.quit()
            return
        win = TrackerWindow(app, config)
        win.present()

    app.connect("activate", on_activate)
    return app.run(None)  # type: ignore[no-any-return]


if __name__ == "__main__":
    raise SystemExit(main())
