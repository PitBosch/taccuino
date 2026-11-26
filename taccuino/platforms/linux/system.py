"""Linux: lock di file, percorsi, RAM."""
from __future__ import annotations

import fcntl
from pathlib import Path

IS_WIN = False
NO_WINDOW: dict = {}
claude_exe_name = "claude"
ffmpeg_hint = ""


def try_lock(f) -> bool:
    """Lock esclusivo NON bloccante su un file aperto in scrittura. False se lo tiene un altro processo."""
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def command(exe: str, *args: str) -> list[str]:
    return [exe, *args]


def data_dir() -> Path:
    return Path.home() / ".local/share/taccuino"


def documents_roots() -> list[Path]:
    return [Path.home()]


def total_ram_gb() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024 / 1024
    except (OSError, ValueError):
        pass
    return 0.0
