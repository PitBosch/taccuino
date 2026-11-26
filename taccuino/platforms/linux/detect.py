"""Linux: quali app stanno usando il microfono, via PipeWire (`pw-dump`)."""
from __future__ import annotations

import json
import subprocess

from ..base import MicUser


def mic_users(ignore_apps: list[str], ignore_pids: set[int] = frozenset()) -> list[MicUser]:
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=10).stdout
        objs = json.loads(out or "[]")
    except Exception:  # noqa: BLE001
        return []
    return users_from_objects(objs, ignore_apps, ignore_pids)


def users_from_objects(objs: list[dict], ignore_apps: list[str], ignore_pids: set[int] = frozenset()) -> list[MicUser]:
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
