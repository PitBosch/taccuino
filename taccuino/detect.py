"""Rileva quando un'app (Meet, Teams, Zoom, Slack…) sta usando il microfono.

Linux: via PipeWire (`pw-dump`). Windows: dal registro "privacy microfono" (CapabilityAccessManager),
dove ogni app che sta usando il microfono ha LastUsedTimeStop = 0.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass

from .compat import IS_WIN, NO_WINDOW


@dataclass
class MicUser:
    node_id: int
    app: str
    binary: str
    pid: int | None


def mic_users(ignore_apps: list[str], ignore_pids: set[int] = frozenset()) -> list[MicUser]:
    if IS_WIN:
        return _win_mic_users(ignore_apps)
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=10, **NO_WINDOW).stdout
        objs = json.loads(out or "[]")
    except Exception:  # noqa: BLE001
        return []
    ignore = {a.lower() for a in ignore_apps}
    users = []
    for o in objs:
        info = o.get("info") or {}
        p = info.get("props") or {}
        if p.get("media.class") != "Stream/Input/Audio":
            continue
        # stream che registrano l'audio di sistema (monitor), non il microfono
        if str(p.get("stream.capture.sink", "")).lower() == "true":
            continue
        app = str(p.get("application.name") or p.get("node.name") or "?")
        binary = str(p.get("application.process.binary") or "")
        pid = p.get("application.process.id")
        pid = int(pid) if str(pid or "").isdigit() else None
        if pid in ignore_pids or app == "taccuino" or p.get("node.name") == "taccuino":
            continue
        if app.lower() in ignore or binary.lower() in ignore:
            continue
        users.append(MicUser(o.get("id", 0), app, binary, pid))
    return users


# ---------- Windows ----------
CONSENT_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"


def _win_entries() -> list[tuple[str, bool, int, int]]:
    """(nome chiave, non-pacchettizzata?, LastUsedTimeStart, LastUsedTimeStop) per ogni app nel registro."""
    import winreg
    out = []

    def scan(path: str, nonpackaged: bool):
        try:
            root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            return
        with root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if name == "NonPackaged":
                    continue
                try:
                    with winreg.OpenKey(root, name) as k:
                        start = int(winreg.QueryValueEx(k, "LastUsedTimeStart")[0])
                        stop = int(winreg.QueryValueEx(k, "LastUsedTimeStop")[0])
                except OSError:
                    continue
                out.append((name, nonpackaged, start, stop))

    scan(CONSENT_KEY, False)
    scan(CONSENT_KEY + r"\NonPackaged", True)
    return out


def _app_name(key: str, nonpackaged: bool) -> tuple[str, str]:
    """Nome leggibile e "binario": C:#Program Files#Google#Chrome#Application#chrome.exe → (chrome, chrome.exe);
    MSTeams_8wekyb3d8bbwe → (MSTeams, MSTeams)."""
    if nonpackaged:
        exe = key.replace("#", "\\").rsplit("\\", 1)[-1]
        return exe.rsplit(".", 1)[0], exe
    pkg = key.split("_", 1)[0]
    return pkg.rsplit(".", 1)[-1], pkg


def _users_from_entries(entries, ignore_apps: list[str], own_exes: set[str]) -> list[MicUser]:
    ignore = {a.lower() for a in ignore_apps}
    users = []
    for key, nonpackaged, start, stop in entries:
        if not (start > 0 and stop == 0):  # in uso adesso
            continue
        app, binary = _app_name(key, nonpackaged)
        path = key.replace("#", "\\").lower()
        if nonpackaged and path in own_exes:
            continue
        if {app.lower(), binary.lower(), binary.lower().removesuffix(".exe")} & ignore:
            continue
        users.append(MicUser(0, app, binary, None))
    return users


def _win_mic_users(ignore_apps: list[str]) -> list[MicUser]:
    try:
        entries = _win_entries()
    except Exception:  # noqa: BLE001
        return []
    # la cattura la fa questo stesso python (soundcard): non è una riunione
    own = {os.path.normcase(os.path.abspath(p)).lower() for p in (sys.executable, getattr(sys, "_base_executable", ""))
           if p}
    own |= {o.replace("python.exe", "pythonw.exe") for o in own} | {o.replace("pythonw.exe", "python.exe") for o in own}
    return _users_from_entries(entries, ignore_apps, own)
