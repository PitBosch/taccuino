"""Tipi condivisi dalle implementazioni per sistema operativo."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MicUser:
    node_id: int
    app: str
    binary: str
    pid: int | None
