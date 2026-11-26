"""Windows: lock di file, percorsi, RAM."""
from __future__ import annotations

import ctypes
import os
import subprocess
from pathlib import Path

IS_WIN = True
# Il daemon gira con pythonw (senza console): senza questo flag ogni ffmpeg/claude aprirebbe per un attimo
# una finestra nera.
NO_WINDOW: dict = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
claude_exe_name = "claude.exe"
ffmpeg_hint = "winget install Gyan.FFmpeg"


def try_lock(f) -> bool:
    """Lock esclusivo NON bloccante su un file aperto in scrittura. False se lo tiene un altro processo."""
    import msvcrt
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def command(exe: str, *args: str) -> list[str]:
    """Un .cmd/.bat (es. claude.cmd installato da npm) va lanciato tramite cmd.exe."""
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", exe, *args]
    return [exe, *args]


def data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "taccuino"


def documents_roots() -> list[Path]:
    """Documenti è spesso spostato su OneDrive: va cercato prima lì."""
    roots = [Path.home()]
    if os.environ.get("OneDrive"):
        roots.insert(0, Path(os.environ["OneDrive"]))
    return roots


def total_ram_gb() -> float:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return st.ullTotalPhys / 1024 ** 3
    return 0.0
