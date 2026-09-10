"""Build a list of past matches from previously exported Markdown summaries.

Deliberately reads back `export_match_summary`'s own output instead of
keeping a separate history store: the exported file already carries
everything a history entry needs (date/time and result in the filename,
classes and turn count in the body), and it's the one thing that's
guaranteed to exist for every match ever tracked -- including matches
exported before this feature existed.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from hearthtrace.markdown_export import RESULT_LABELS

_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_(\w+)\.md$")
_CLASS_LINE_RE = re.compile(r"\*\*Eigene Klasse:\*\* (\w+) \| \*\*Gegner-Klasse:\*\* (\w+)")
_TURN_HEADING_RE = re.compile(r"^## Zug (\d+)", re.MULTILINE)

# Which session log (and which match within it, for a session that holds
# several) a given export came from -- kept separately from the export
# itself, not as a header line in the Markdown: that file is meant to be
# pasted into a chat as-is, and re-parsing an old, possibly-since-rotated
# session log is an app-internal concern the export's own reader has no
# use for. Keyed by export filename (stable, already the identity
# `_parse_export` matches on) rather than by match time -- simpler than
# reconciling clock drift between the two.
_REPLAY_INDEX_FILENAME = "replay_index.json"

# Only the two multi-word class names need special-casing -- every other
# class name (SHAMAN, MAGE, ...) already reads correctly through
# `str.title()`.
_CLASS_DISPLAY_NAMES = {
    "DEATHKNIGHT": "Death Knight",
    "DEMONHUNTER": "Demon Hunter",
}


@dataclass
class MatchHistoryEntry:
    when: datetime
    result_label: str
    own_class: str
    opponent_class: str
    turn_count: int
    path: Path
    # None for a match exported before this feature existed, or if the
    # index entry was otherwise never recorded -- Replay must then simply
    # not be offered for that row, not guess.
    log_path: Path | None = None
    game_index: int | None = None


def _display_class(class_name: str) -> str:
    return _CLASS_DISPLAY_NAMES.get(class_name, class_name.title())


def _parse_export(path: Path) -> MatchHistoryEntry | None:
    """Returns None for anything in `export_dir` that isn't one of our own
    exports (wrong filename shape, or missing the class line this needs) --
    e.g. a stray unrelated .md file someone happens to drop in there."""
    name_match = _FILENAME_RE.match(path.name)
    if name_match is None:
        return None

    text = path.read_text()
    class_match = _CLASS_LINE_RE.search(text)
    if class_match is None:
        return None
    own_class, opponent_class = class_match.groups()

    when = datetime.strptime(name_match.group(1), "%Y-%m-%d_%H-%M-%S")
    result = name_match.group(2)
    turn_numbers = [int(n) for n in _TURN_HEADING_RE.findall(text)]

    return MatchHistoryEntry(
        when=when,
        result_label=RESULT_LABELS.get(result, result),
        own_class=_display_class(own_class),
        opponent_class=_display_class(opponent_class),
        turn_count=max(turn_numbers, default=0),
        path=path,
    )


def record_replay_source(
    state_dir: Path, export_path: Path, log_path: Path, game_index: int
) -> None:
    """Remembers which session log -- and which match within it, for a
    session that holds several -- `export_path` came from, so that
    match's history entry can later be reopened in Replay. Called right
    after `export_match_summary` writes the file.

    Read-modify-write of the whole (small: one entry per ever-exported
    match) index rather than a database -- consistent with this project's
    existing state files (`ui.py`'s own last-export marker); a corrupt or
    unreadable index is treated as empty rather than raised, since losing
    the replay *link* for past matches must never take down exporting the
    current one.
    """
    index_path = state_dir / _REPLAY_INDEX_FILENAME
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index = _load_replay_index(state_dir)
    index[export_path.name] = {"log_path": str(log_path), "game_index": game_index}
    # Write-then-rename, not a direct write_text: a crash/power loss mid-
    # write must not leave the whole index (every past match's replay
    # link, not just the one being added) truncated or invalid. os.replace
    # is atomic on the same filesystem, which a sibling temp file always
    # is.
    tmp_path = index_path.with_suffix(index_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(index))
    tmp_path.replace(index_path)


def _load_replay_index(state_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        loaded = json.loads((state_dir / _REPLAY_INDEX_FILENAME).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _replay_source_fields(source: dict[str, Any] | None) -> tuple[Path | None, int | None]:
    """Pulls (log_path, game_index) out of one index entry, or (None,
    None) if it's missing or malformed (a partial write, a hand edit, a
    future schema change) -- one bad entry must only cost *that* row its
    replay link, not raise and take the whole Verlauf list down with it
    (`_refresh_history_list`'s except would otherwise replace every past
    match with a bare error message over a single corrupt row)."""
    if source is None:
        return None, None
    log_path = source.get("log_path")
    game_index = source.get("game_index")
    if not isinstance(log_path, str) or not isinstance(game_index, int):
        return None, None
    return Path(log_path), game_index


def load_match_history(export_dir: Path, state_dir: Path | None = None) -> list[MatchHistoryEntry]:
    """Every previously exported match summary in `export_dir`, newest
    first. Returns an empty list if the directory doesn't exist yet (e.g.
    no match has ever been exported).

    `state_dir` (optional) is where `record_replay_source` keeps its
    index -- passed in, not hardcoded, matching every other directory
    this module already takes as a parameter. Omit it (or pass a
    directory with no index yet) to get entries with `log_path`/
    `game_index` left at `None`, e.g. in a test that doesn't care about
    the replay link.

    Accepted MVP simplification (same trade-off `ui.py`'s `_poll_once`
    already makes for `parse_log`): re-reads and re-parses every export on
    every call rather than caching, which grows linearly with total match
    count instead of being O(1) for "one more match got added". Not
    redesigned now -- at personal-tracker scale (one file per match, each
    a few hundred KB at most) this is well under human-perceptible latency
    even at a few hundred matches; revisit only if that stops being true.
    """
    if not export_dir.exists():
        return []
    replay_index = _load_replay_index(state_dir) if state_dir is not None else {}
    entries = []
    for path in export_dir.glob("*.md"):
        entry = _parse_export(path)
        if entry is None:
            continue
        source = replay_index.get(path.name)
        log_path, game_index = _replay_source_fields(source)
        entry.log_path = log_path
        entry.game_index = game_index
        entries.append(entry)
    entries.sort(key=lambda entry: entry.when, reverse=True)
    return entries


def format_history_row(entry: MatchHistoryEntry) -> str:
    return (
        f"{entry.when:%d.%m. %H:%M}   {entry.result_label}   "
        f"{entry.own_class} vs. {entry.opponent_class}   {entry.turn_count} Züge"
    )
