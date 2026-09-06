from pathlib import Path


def find_latest_power_log(logs_dir: Path) -> Path | None:
    """Return the Power.log inside the most recently created
    Hearthstone_* session folder, or None if there isn't one."""
    session_dirs = [p for p in logs_dir.glob("Hearthstone_*") if p.is_dir()]
    if not session_dirs:
        return None
    latest = max(session_dirs, key=lambda p: p.name)
    return latest / "Power.log"
