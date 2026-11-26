"""Rileva quando un'app (Meet, Teams, Zoom, Slack…) sta usando il microfono.

L'implementazione dipende dal sistema operativo (`taccuino/platforms/`): PipeWire su Linux,
registro "privacy microfono" su Windows.
"""
from __future__ import annotations

from .platforms import mic_users  # noqa: F401
from .platforms.base import MicUser  # noqa: F401
