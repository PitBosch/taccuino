"""Cattura audio (solo registrazione, mai riproduzione).

ffmpeg legge due sorgenti PipeWire/PulseAudio e scrive un FLAC stereo 16 kHz:
  canale L = microfono (tu + chi è in stanza), canale R = audio di sistema (chi è in call).
"""
from __future__ import annotations

import signal
import subprocess
from pathlib import Path

import numpy as np

SR = 16000


class Recorder:
    def __init__(self, mic: str, system: str, out: Path):
        self.mic, self.system, self.out = mic, system, out
        self.proc: subprocess.Popen | None = None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def start(self) -> None:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-f", "pulse", "-name", "taccuino", "-i", self.mic,
               "-f", "pulse", "-name", "taccuino", "-i", self.system,
               "-filter_complex",
               "[0:a]aresample=16000,pan=mono|c0=0.5*c0+0.5*c1[m];"
               "[1:a]aresample=16000,pan=mono|c0=0.5*c0+0.5*c1[s];"
               "[m][s]join=inputs=2:channel_layout=stereo[out]",
               "-map", "[out]", "-ar", str(SR), "-c:a", "flac", str(self.out)]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE)

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def stop(self) -> str:
        if not self.proc:
            return ""
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)  # ffmpeg chiude il FLAC in modo pulito
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        return (self.proc.stderr.read() if self.proc.stderr else b"").decode(errors="ignore")


def load_channels(path: str | Path) -> np.ndarray:
    """Decodifica (senza riprodurre) in float32 16 kHz, forma (n, canali)."""
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                            "stream=channels", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    ch = 2 if probe.stdout.strip() == "2" else 1
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
                          "-ac", str(ch), "-ar", str(SR), "-f", "s16le", "pipe:1"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return x[: len(x) // ch * ch].reshape(-1, ch)


def speech_ratio(x: np.ndarray, threshold=0.01) -> float:
    """Frazione approssimativa di frame da 30 ms con energia sopra soglia."""
    n = len(x) // 480
    if n == 0:
        return 0.0
    f = x[: n * 480].reshape(n, 480)
    return float(np.mean(np.sqrt(np.mean(f * f, axis=1)) > threshold))
