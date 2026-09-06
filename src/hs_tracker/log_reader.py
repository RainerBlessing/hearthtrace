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

        try:
            new_bytes = self._read_new_bytes()
        except FileNotFoundError:
            # The file existed a moment ago but vanished before we could
            # open it (e.g. concurrent session-rotation cleanup). The next
            # poll() will pick up whatever session exists then.
            return

        yield from self._complete_lines(new_bytes)

    def _read_new_bytes(self) -> bytes:
        """Read the file's growth since the last poll, in bytes.

        Byte offsets (rather than text-mode offsets) let us reliably detect
        truncation by comparing against the file's current size, and let us
        find line boundaries without depending on decoding.
        """
        assert self._current_path is not None
        with self._current_path.open("rb") as f:
            size = f.seek(0, 2)
            if size < self._offset:
                # The file shrank below our stored offset: it was truncated
                # or rewritten in place without a session-folder rotation.
                # Treat it as a fresh file rather than seeking past EOF.
                self._offset = 0
            f.seek(self._offset)
            return f.read()

    def _complete_lines(self, new_bytes: bytes) -> Iterator[str]:
        """Yield only newline-terminated lines from new_bytes, decoded as
        UTF-8. Any trailing fragment without a newline is left unconsumed
        (the offset is not advanced past it) so the next poll() re-reads and
        completes it instead of yielding a corrupt partial line."""
        last_newline = new_bytes.rfind(b"\n")
        if last_newline == -1:
            return
        complete = new_bytes[: last_newline + 1]
        self._offset += len(complete)
        text = complete.decode("utf-8", errors="replace")
        for line in text.split("\n")[:-1]:
            yield line + "\n"
