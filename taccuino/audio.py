"""Cattura audio (solo registrazione, nessuna riproduzione) e segmentazione.

ffmpeg legge due sorgenti PipeWire/PulseAudio e produce PCM stereo 16 kHz:
  canale L = microfono ("Io"), canale R = audio di sistema ("Altri").
Ogni canale viene segmentato sui silenzi e passato a Whisper separatamente.
"""
from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

import numpy as np

SR = 16000
FRAME = 480  # 30 ms


@dataclass
class Segment:
    speaker: str  # "me" | "them"
    t0: float  # secondi dall'inizio della registrazione
    audio: np.ndarray  # float32 mono 16 kHz


class Segmenter:
    """VAD a energia: chiude un segmento dopo una pausa o a lunghezza massima."""

    def __init__(self, speaker: str, on_segment: Callable[[Segment], None],
                 min_len=4.0, max_len=25.0, pause=0.7, threshold=0.006):
        self.speaker, self.on_segment = speaker, on_segment
        self.min_len, self.max_len, self.pause, self.threshold = min_len, max_len, pause, threshold
        self.buf: list[np.ndarray] = []
        self.voiced = 0
        self.silence = 0.0
        self.t = 0.0
        self.t0 = 0.0
        self.noise = threshold

    def feed(self, x: np.ndarray) -> None:
        for i in range(0, len(x) - FRAME + 1, FRAME):
            f = x[i:i + FRAME]
            rms = float(np.sqrt(np.mean(f * f)) + 1e-9)
            # soglia adattiva: rumore di fondo stimato lentamente
            if rms < self.noise * 2:
                self.noise = 0.995 * self.noise + 0.005 * max(rms, 1e-4)
            is_voice = rms > max(self.threshold, self.noise * 3)
            if not self.buf and not is_voice:
                self.t += FRAME / SR
                continue
            if not self.buf:
                self.t0 = self.t
            self.buf.append(f)
            self.voiced += is_voice
            self.silence = 0.0 if is_voice else self.silence + FRAME / SR
            self.t += FRAME / SR
            dur = len(self.buf) * FRAME / SR
            if (dur >= self.min_len and self.silence >= self.pause) or dur >= self.max_len:
                self.flush()
            elif dur < self.min_len and self.silence >= 1.5:
                # solo rumore breve: scarta
                if self.voiced * FRAME / SR < 0.4:
                    self._reset()

    def flush(self) -> None:
        if self.buf and self.voiced * FRAME / SR >= 0.5:
            self.on_segment(Segment(self.speaker, self.t0, np.concatenate(self.buf)))
        self._reset()

    def _reset(self):
        self.buf, self.voiced, self.silence = [], 0, 0.0


class Recorder:
    def __init__(self, mic: str, system: str | None, on_segment: Callable[[Segment], None],
                 wav_path: str | None = None):
        self.mic, self.system, self.wav_path = mic, system, wav_path
        self.segs = [Segmenter("me", on_segment)]
        if system:
            self.segs.append(Segmenter("them", on_segment))
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.level = [0.0, 0.0]

    def _cmd(self) -> list[str]:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-f", "pulse", "-i", self.mic]
        if self.system:
            cmd += ["-f", "pulse", "-i", self.system,
                    "-filter_complex",
                    "[0:a]aresample=16000,pan=mono|c0=0.5*c0+0.5*c1[m];"
                    "[1:a]aresample=16000,pan=mono|c0=0.5*c0+0.5*c1[s];"
                    "[m][s]join=inputs=2:channel_layout=stereo[out]",
                    "-map", "[out]"]
        else:
            cmd += ["-ac", "1"]
        cmd += ["-ar", str(SR), "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
        return cmd

    def start(self) -> None:
        self.proc = subprocess.Popen(self._cmd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        nch = len(self.segs)
        wav = open(self.wav_path, "wb") if self.wav_path else None
        try:
            while self.proc and self.proc.stdout:
                raw = self.proc.stdout.read(SR // 5 * 2 * nch)  # 200 ms
                if not raw:
                    break
                if wav:
                    wav.write(raw)
                x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                x = x[: len(x) // nch * nch].reshape(-1, nch)
                for c, seg in enumerate(self.segs):
                    ch = x[:, c]
                    self.level[c] = float(np.sqrt(np.mean(ch * ch)))
                    seg.feed(ch)
        finally:
            for seg in self.segs:
                seg.flush()
            if wav:
                wav.close()

    def stop(self) -> str:
        err = ""
        if self.proc:
            self.proc.terminate()  # ffmpeg chiude stdout → il thread legge EOF e fa flush
            if self.thread:
                self.thread.join(timeout=5)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            if self.proc.stderr:
                err = self.proc.stderr.read().decode(errors="ignore")
        return err


def load_file(path: str) -> np.ndarray:
    """Decodifica qualsiasi file audio/video in float32 mono 16 kHz (senza riprodurlo)."""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", path,
                          "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.int16).astype(np.float32) / 32768.0


def segment_array(x: np.ndarray, speaker="them") -> list[Segment]:
    out: list[Segment] = []
    s = Segmenter(speaker, out.append)
    s.feed(x)
    s.flush()
    return out
