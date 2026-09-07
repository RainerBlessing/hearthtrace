"""Build a list of past matches from previously exported Markdown summaries.

Deliberately reads back `export_match_summary`'s own output instead of
keeping a separate history store: the exported file already carries
everything a history entry needs (date/time and result in the filename,
classes and turn count in the body), and it's the one thing that's
guaranteed to exist for every match ever tracked -- including matches
exported before this feature existed.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from hs_tracker.markdown_export import RESULT_LABELS

_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_(\w+)\.md$")
_CLASS_LINE_RE = re.compile(r"\*\*Eigene Klasse:\*\* (\w+) \| \*\*Gegner-Klasse:\*\* (\w+)")
_TURN_HEADING_RE = re.compile(r"^## Zug (\d+)", re.MULTILINE)

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


def load_match_history(export_dir: Path) -> list[MatchHistoryEntry]:
    """Every previously exported match summary in `export_dir`, newest
    first. Returns an empty list if the directory doesn't exist yet (e.g.
    no match has ever been exported).

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
    entries = [
        entry
        for entry in (_parse_export(path) for path in export_dir.glob("*.md"))
        if entry is not None
    ]
    entries.sort(key=lambda entry: entry.when, reverse=True)
    return entries


def format_history_row(entry: MatchHistoryEntry) -> str:
    return (
        f"{entry.when:%d.%m. %H:%M}   {entry.result_label}   "
        f"{entry.own_class} vs. {entry.opponent_class}   {entry.turn_count} Züge"
    )
