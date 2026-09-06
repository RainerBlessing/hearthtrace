"""Load the application config from `~/.config/hs-tracker/config.toml`."""

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

    with path.open("rb") as f:
        data = tomllib.load(f)

    return Config(
        logs_dir=Path(data["logs_dir"]).expanduser(),
        export_dir=Path(data["export_dir"]).expanduser(),
    )
