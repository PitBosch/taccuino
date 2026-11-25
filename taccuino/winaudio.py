"""Cattura audio su Windows (WASAPI), solo registrazione.

Due thread leggono microfono e audio di sistema (loopback dell'uscita predefinita) con `soundcard`;
un terzo thread li allinea all'orologio, li interlaccia e li passa a ffmpeg, che scrive lo stesso
FLAC stereo 16 kHz di Linux: canale L = microfono, canale R = audio di sistema.
Allineare all'orologio serve perché il loopback WASAPI non manda nulla quando non suona niente:
il canale mancante viene riempito di silenzio.
"""
from __future__ import annotations

import collections
import os
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from .compat import NO_WINDOW

SR = 16000
CAPTURE_SR = 48000            # formato di mix tipico di Windows; si riduce a 16 kHz mediando 3 campioni
BLOCK = CAPTURE_SR // 10      # 100 ms
MAX_LAG = SR // 2             # oltre 0,5 s di arretrato su un canale si scarta il vecchio (deriva dei clock)


def _open_devices(mic: str, system: str):
    import soundcard as sc
    m = sc.default_microphone() if mic in ("", "default") else sc.get_microphone(mic)
    spk = sc.default_speaker() if system in ("", "default", "@DEFAULT_MONITOR@") else sc.get_speaker(system)
    loop = sc.get_microphone(id=str(spk.name), include_loopback=True)
    return m, loop


class Recorder:
    """Stessa interfaccia del Recorder Linux: start / stop / alive / pid."""

    def __init__(self, mic: str, system: str, out: Path):
        self.mic, self.system, self.out = mic, system, out
        self.proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._bufs = [collections.deque(), collections.deque()]
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self.error = ""

    @property
    def pid(self) -> int | None:
        # è questo processo (python) ad aprire il microfono, non ffmpeg
        return os.getpid()

    def _capture(self, dev, idx: int, channels: int):
        try:
            with dev.recorder(samplerate=CAPTURE_SR, channels=channels, blocksize=BLOCK) as rec:
                while not self._stop.is_set():
                    x = rec.record(numframes=BLOCK)
                    if x is None or len(x) == 0:
                        continue
                    x = np.asarray(x, dtype=np.float32)
                    x = x.mean(axis=1) if x.ndim == 2 else x
                    x = x[: len(x) // 3 * 3].reshape(-1, 3).mean(axis=1)  # 48 kHz → 16 kHz
                    with self._lock:
                        self._bufs[idx].append(x)
        except Exception as e:  # noqa: BLE001
            self.error = f"cattura {'microfono' if idx == 0 else 'sistema'}: {e}"
            self._stop.set()

    def _take(self, idx: int, n: int) -> np.ndarray:
        with self._lock:
            buf = self._bufs[idx]
            have = sum(len(b) for b in buf)
            while have - n > MAX_LAG and buf:  # troppo arretrato: scarta il più vecchio
                have -= len(buf.popleft())
            out, got = [], 0
            while buf and got < n:
                b = buf.popleft()
                if got + len(b) > n:
                    buf.appendleft(b[n - got:])
                    b = b[: n - got]
                out.append(b)
                got += len(b)
        x = np.concatenate(out) if out else np.zeros(0, np.float32)
        return np.pad(x, (0, n - len(x))) if len(x) < n else x

    def _writer(self):
        t0, written = time.monotonic(), 0
        try:
            while not self._stop.is_set():
                time.sleep(0.1)
                n = int((time.monotonic() - t0) * SR) - written
                if n <= 0:
                    continue
                st = np.stack([self._take(0, n), self._take(1, n)], axis=1)
                pcm = (np.clip(st, -1, 1) * 32767).astype("<i2").tobytes()
                self.proc.stdin.write(pcm)
                self.proc.stdin.flush()
                written += n
        except (BrokenPipeError, OSError, ValueError) as e:
            self.error = self.error or f"ffmpeg: {e}"
            self._stop.set()

    def start(self) -> None:
        mic, loop = _open_devices(self.mic, self.system)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "s16le", "-ar", str(SR), "-ac", "2", "-i", "pipe:0",
               "-c:a", "flac", "-flush_packets", "1", str(self.out)]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, **NO_WINDOW)
        self._threads = [threading.Thread(target=self._capture, args=(mic, 0, 1), daemon=True),
                         threading.Thread(target=self._capture, args=(loop, 1, 2), daemon=True),
                         threading.Thread(target=self._writer, daemon=True)]
        for t in self._threads:
            t.start()

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None and not self._stop.is_set())

    def stop(self) -> str:
        if not self.proc:
            return ""
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3)
        try:
            self.proc.stdin.close()  # EOF: ffmpeg chiude il FLAC in modo pulito (niente SIGINT su Windows)
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        err = (self.proc.stderr.read() if self.proc.stderr else b"").decode(errors="ignore")
        return (self.error + "\n" + err).strip()


def list_devices() -> str:
    import soundcard as sc
    rows = ["Microfoni:"]
    dm, ds = sc.default_microphone(), sc.default_speaker()
    for m in sc.all_microphones():
        rows.append(f"  {'*' if m.name == dm.name else ' '} {m.name}")
    rows.append("Uscite (l'audio di sistema si registra dal loopback di quella predefinita):")
    for s in sc.all_speakers():
        rows.append(f"  {'*' if s.name == ds.name else ' '} {s.name}")
    rows.append("(* = predefinito; per cambiarli: taccuino config mic_source \"nome\" / system_source \"nome\")")
    return "\n".join(rows)
