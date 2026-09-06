from collections.abc import Iterator
from pathlib import Path


def find_latest_power_log(logs_dir: Path) -> Path | None:
    """Return the Power.log inside the most recently created
    Hearthstone_* session folder, or None if there isn't one."""
    session_dirs = [p for p in logs_dir.glob("Hearthstone_*") if p.is_dir()]
    if not session_dirs:
        return None
    latest = max(session_dirs, key=lambda p: p.name)
    return latest / "Power.log"


class LogWatcher:
    """Tails the newest Power.log under logs_dir, following session rotation."""

    def __init__(self, logs_dir: Path) -> None:
        self._logs_dir = logs_dir
        self._current_path: Path | None = None
        self._offset = 0

    def poll(self) -> Iterator[str]:
        latest = find_latest_power_log(self._logs_dir)
        if latest is None or not latest.exists():
            return
        if latest != self._current_path:
            self._current_path = latest
            self._offset = 0
        with self._current_path.open() as f:
            f.seek(self._offset)
            yield from f
            self._offset = f.tell()
