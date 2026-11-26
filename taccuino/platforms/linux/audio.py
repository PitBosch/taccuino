"""Cattura audio su Linux (solo registrazione, mai riproduzione).

ffmpeg legge due sorgenti PipeWire/PulseAudio e scrive un FLAC stereo 16 kHz
(canale L = microfono, tu + chi è in stanza; canale R = audio di sistema, chi è in call).
"""
from __future__ import annotations

import signal
import subprocess
from pathlib import Path

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
               "-map", "[out]", "-ar", str(SR), "-c:a", "flac",
               "-flush_packets", "1",  # scrive subito su disco: la console può trascrivere in tempo reale
               str(self.out)]
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


def list_audio_devices() -> str:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-sources", "pulse"], capture_output=True, text=True)
    return "Sorgenti audio:\n" + (out.stdout or out.stderr)
