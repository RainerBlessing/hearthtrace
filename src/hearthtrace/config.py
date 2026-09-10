"""Load the application config from `~/.config/hearthtrace/config.toml`."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

_TEMPLATE = (
    '# Trag hier den Pfad zum "Logs"-Ordner deiner Hearthstone-Installation ein\n'
    '# (der Ordner, der die Hearthstone_* Unterordner enthaelt). Das kann auf\n'
    '# einem beliebigen Wine-Laufwerk/Mountpoint liegen, nicht zwingend unter C:.\n'
    'logs_dir = "/pfad/zum/Logs-ordner"\n'
    'export_dir = "~/HearthstoneAnalysis"\n'
)


class ConfigError(Exception):
    """Raised when the config file is missing or invalid."""


@dataclass
class Config:
    logs_dir: Path
    export_dir: Path


def load_config(path: Path) -> Config:
    """Load `Config` from a TOML file at `path`.

    If the file doesn't exist, a template is written in its place and a
    `ConfigError` is raised so the caller can prompt the user to fill it in
    before trying again.
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_TEMPLATE)
        raise ConfigError(f"Config template created at {path} — please fill in logs_dir")

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Fehlerhafte config.toml: {e}") from e

    try:
        logs_dir = data["logs_dir"]
    except KeyError as e:
        raise ConfigError(f"config.toml fehlt den Eintrag '{e.args[0]}'") from e

    try:
        export_dir = data["export_dir"]
    except KeyError as e:
        raise ConfigError(f"config.toml fehlt den Eintrag '{e.args[0]}'") from e

    for key, value in (("logs_dir", logs_dir), ("export_dir", export_dir)):
        if not isinstance(value, str):
            raise ConfigError(
                f"config.toml: Eintrag '{key}' muss eine Zeichenkette (String) sein, "
                f"ist aber {type(value).__name__} ({value!r})"
            )

    return Config(
        logs_dir=Path(logs_dir).expanduser(),
        export_dir=Path(export_dir).expanduser(),
    )
