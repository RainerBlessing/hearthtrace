"""Checks and (optionally) fixes Hearthstone's own logging configuration.

Two separate config files control whether this tracker can work at all:

- `log.config` (`[Power]` section) makes Hearthstone write `Power.log` in
  the first place, and with enough detail (`Verbose=True`) to reconstruct
  a match. Both are a one-time manual setup step (see the project's design
  doc) -- this module only *checks* them, since fixing a missing one from
  scratch (no `Power.log` has ever been written yet) is a bigger
  bootstrapping problem than this tracker can safely solve unattended.
- `client.config` (`[Log]` section, `FileSizeLimit.Int`) caps `Power.log`
  at 10MB by default. Once a session hits that cap, Hearthstone stops
  writing to the file *entirely* -- including the rest of whatever match
  was in progress -- with no way for any tool to recover what's lost.
  Setting `FileSizeLimit.Int=-1` disables the cap, and this module *can*
  safely apply that fix (with a backup) since it's just one value in one
  section, offered to the user rather than changed silently.
"""

import configparser
from dataclasses import dataclass
from pathlib import Path


@dataclass
class LogSetupCheck:
    power_log_enabled: bool
    verbose_enabled: bool
    size_limit_disabled: bool

    @property
    def all_ok(self) -> bool:
        return self.power_log_enabled and self.verbose_enabled and self.size_limit_disabled


def _read_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    # Hearthstone's own config keys (e.g. `FileSizeLimit.Int`) contain
    # dots; ConfigParser's default `optionxform` lowercases keys, which
    # would still round-trip fine for reading but needlessly departs from
    # the file's real casing -- keep it as-is.
    parser.optionxform = str  # type: ignore[method-assign,assignment]
    if path.exists():
        parser.read(path)
    return parser


def check_log_setup(hearthstone_dir: Path) -> LogSetupCheck:
    """Checks `log.config` and `client.config` in `hearthstone_dir` (the
    Hearthstone install directory -- the parent of the configured
    `logs_dir`, since `Logs/` sits right next to `Hearthstone.exe`)."""
    log_config = _read_ini(hearthstone_dir / "log.config")
    client_config = _read_ini(hearthstone_dir / "client.config")
    return LogSetupCheck(
        power_log_enabled=log_config.getboolean("Power", "FilePrinting", fallback=False),
        verbose_enabled=log_config.getboolean("Power", "Verbose", fallback=False),
        size_limit_disabled=client_config.get("Log", "FileSizeLimit.Int", fallback="") == "-1",
    )


def _find_section_body(lines: list[str], section: str) -> tuple[int, int] | None:
    """Returns the (start, end) line-index range of `[section]`'s body --
    right after its header line, up to (not including) the next `[...]`
    header or end of file. `None` if the section doesn't exist."""
    header = f"[{section}]"
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        return None
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].strip().startswith("[") and lines[i].strip().endswith("]")
        ),
        len(lines),
    )
    return start + 1, end


def _find_key_line(lines: list[str], body: tuple[int, int], key: str) -> int | None:
    start, end = body
    return next(
        (i for i in range(start, end) if lines[i].split("=", 1)[0].strip() == key), None
    )


def _upsert_ini_value(text: str, section: str, key: str, value: str) -> str:
    """Sets `key = value` inside `[section]` of INI-style config text,
    creating the section if it's missing -- without touching anything else
    in the file (other sections, comments, formatting). Deliberately a
    plain text edit rather than a `ConfigParser` read-modify-write: the
    latter would reformat/reorder the *whole* file, which is too invasive
    for a config file that belongs to someone else's application, not this
    project."""
    lines = text.splitlines()
    body = _find_section_body(lines, section)
    if body is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"[{section}]", f"{key}={value}"]
        return "\n".join(lines) + "\n"

    key_line = _find_key_line(lines, body, key)
    if key_line is not None:
        lines[key_line] = f"{key}={value}"
    else:
        lines.insert(body[1], f"{key}={value}")
    return "\n".join(lines) + "\n"


def disable_log_size_limit(hearthstone_dir: Path) -> None:
    """Sets `FileSizeLimit.Int=-1` in `client.config`'s `[Log]` section.
    Backs up any existing file first (`client.config.bak`) -- this edits a
    file that belongs to Hearthstone itself, not this project. Hearthstone
    must be restarted for the change to take effect."""
    client_config_path = hearthstone_dir / "client.config"
    existing = client_config_path.read_text() if client_config_path.exists() else ""
    if existing:
        client_config_path.with_name(client_config_path.name + ".bak").write_text(existing)
    client_config_path.write_text(_upsert_ini_value(existing, "Log", "FileSizeLimit.Int", "-1"))
