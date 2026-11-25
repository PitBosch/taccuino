"""Piccole differenze tra Linux e Windows, tenute in un posto solo."""
from __future__ import annotations

import subprocess
import sys

IS_WIN = sys.platform == "win32"

# Su Windows il daemon gira con pythonw (senza console): senza questo flag ogni ffmpeg/claude
# aprirebbe per un attimo una finestra nera.
NO_WINDOW: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}


def try_lock(f) -> bool:
    """Lock esclusivo NON bloccante su un file aperto in scrittura. False se lo tiene un altro processo."""
    if IS_WIN:
        import msvcrt
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def command(exe: str, *args: str) -> list[str]:
    """Su Windows un .cmd/.bat (es. claude.cmd installato da npm) va lanciato tramite cmd.exe."""
    if IS_WIN and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", exe, *args]
    return [exe, *args]
