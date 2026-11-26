"""Cattura audio (solo registrazione, mai riproduzione).

Linux: ffmpeg legge due sorgenti PipeWire/PulseAudio e scrive un FLAC stereo 16 kHz
  (canale L = microfono, tu + chi è in stanza; canale R = audio di sistema, chi è in call).
Windows: stesso FLAC, catturato via WASAPI (vedi winaudio.py).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from .platforms import NO_WINDOW, Recorder  # noqa: F401

SR = 16000


def load_channels(path: str | Path) -> np.ndarray:
    """Decodifica (senza riprodurre) in float32 16 kHz, forma (n, canali)."""
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                            "stream=channels", "-of", "csv=p=0", str(path)], capture_output=True, text=True,
                           **NO_WINDOW)
    ch = 2 if probe.stdout.strip() == "2" else 1
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
                          "-ac", str(ch), "-ar", str(SR), "-f", "s16le", "pipe:1"],
                         capture_output=True, check=True, **NO_WINDOW).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return x[: len(x) // ch * ch].reshape(-1, ch)


def speech_ratio(x: np.ndarray, threshold=0.01) -> float:
    """Frazione approssimativa di frame da 30 ms con energia sopra soglia."""
    n = len(x) // 480
    if n == 0:
        return 0.0
    f = x[: n * 480].reshape(n, 480)
    return float(np.mean(np.sqrt(np.mean(f * f, axis=1)) > threshold))

