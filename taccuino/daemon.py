"""Daemon sempre attivo: registra le riunioni quando un'app usa il microfono e le elabora alla fine.

Controllo manuale (anche per riunioni dal vivo, senza app):
  taccuino start "titolo"  → scrive "start" nel file di controllo
  taccuino stop            → "stop"
  taccuino note "testo"    → aggiunge un appunto alla riunione in corso
"""
from __future__ import annotations

import json
import queue
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from .audio import Recorder
from .config import CONTROL_PATH, MEETINGS_DIR, STATE_PATH, load_settings
from .detect import mic_users
from .pipeline import cleanup_audio


def log(msg: str):
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


class Meeting:
    def __init__(self, manual: bool, title: str = "", apps: list[str] | None = None):
        self.started = time.time()
        self.manual = manual
        self.dir = MEETINGS_DIR / datetime.fromtimestamp(self.started).strftime("%Y-%m-%d_%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.apps = set(apps or [])
        self.title = title
        self.rec: Recorder | None = None
        self.live: subprocess.Popen | None = None
        self.save_meta()

    def save_meta(self):
        p = self.dir / "meta.json"
        try:
            meta = json.loads(p.read_text())  # conserva cartella/nota scritte dal live worker
        except (FileNotFoundError, ValueError):
            meta = {}
        meta.update(started=self.started, manual=self.manual, title=self.title, apps=sorted(self.apps))
        p.write_text(json.dumps(meta, indent=1))


class Daemon:
    def __init__(self):
        self.s = load_settings()
        self.meeting: Meeting | None = None
        self.jobs: queue.Queue[Path] = queue.Queue()
        self.processing: str | None = None
        self.mic_since: float | None = None
        self.free_since: float | None = None
        self.running = True
        threading.Thread(target=self._worker, daemon=True).start()

    # ---------- registrazione ----------
    def start(self, manual: bool, title="", apps=None):
        if self.meeting:
            return
        m = Meeting(manual, title, apps)
        m.rec = Recorder(self.s.mic_source, self.s.system_source, m.dir / "audio.flac")
        m.rec.start()
        if self.s.live_transcribe:  # trascrizione + appunti live in un processo a parte
            m.live = subprocess.Popen([sys.executable, "-m", "taccuino.cli", "live-worker", str(m.dir)],
                                      stdin=subprocess.DEVNULL)
        self.meeting = m
        log(f"● registrazione avviata ({'manuale' if manual else ', '.join(m.apps)}) → {m.dir.name}")

    def stop(self, reason=""):
        m = self.meeting
        if not m:
            return
        self.meeting = None
        err = m.rec.stop() if m.rec else ""
        dur = time.time() - m.started
        m.save_meta()
        if err.strip():
            log(f"ffmpeg: {err.strip()[-300:]}")
        short = dur < self.s.min_meeting_seconds and not m.manual
        if m.live and m.live.poll() is None:
            if short:
                m.live.kill()
                m.live.wait()
            else:
                m.live.terminate()  # fa l'ultimo giro di correzione/appunti, poi esce
        if short:
            log(f"■ registrazione di {dur:.0f}s scartata (troppo corta)")
            for f in m.dir.iterdir():
                f.unlink()
            m.dir.rmdir()
            return
        log(f"■ registrazione chiusa dopo {dur / 60:.1f} min {reason}; in coda per l'elaborazione")

        def enqueue(live=m.live, d=m.dir):  # aspetta il live worker senza bloccare il rilevamento
            if live:
                try:
                    live.wait(timeout=150)
                except subprocess.TimeoutExpired:
                    live.kill()
            self.jobs.put(d)
        threading.Thread(target=enqueue, daemon=True).start()

    # ---------- elaborazione ----------
    def _worker(self):
        while True:
            d = self.jobs.get()
            self.processing = d.name
            self.write_state()
            try:
                # processo separato: diarization e Whisper non bloccano il rilevamento delle call
                # (le librerie native tengono il GIL e fermerebbero il ciclo principale)
                r = subprocess.run([sys.executable, "-m", "taccuino.cli", "process", str(d)],
                                   stdin=subprocess.DEVNULL)
                if r.returncode:
                    log(f"[{d.name}] elaborazione fallita (exit {r.returncode})")
            except Exception:  # noqa: BLE001
                log(f"[{d.name}] errore:\n{traceback.format_exc()}")
            finally:
                self.processing = None
                self.write_state()
                cleanup_audio(MEETINGS_DIR, self.s.keep_audio_days)

    # ---------- stato e controllo ----------
    def write_state(self, users=None):
        m = self.meeting
        STATE_PATH.write_text(json.dumps({
            "pid": __import__("os").getpid(), "updated": time.time(),
            "recording": bool(m), "meeting_dir": str(m.dir) if m else None,
            "manual": m.manual if m else None, "since": m.started if m else None,
            "apps": sorted(m.apps) if m else [], "mic_users": users or [],
            "processing": self.processing, "queue": self.jobs.qsize()}, indent=1))

    def _control(self):
        if not CONTROL_PATH.exists():
            return
        cmd = CONTROL_PATH.read_text().strip()
        CONTROL_PATH.unlink(missing_ok=True)
        verb, _, arg = cmd.partition(" ")
        if verb == "start":
            if self.meeting and not self.meeting.manual:
                self.meeting.manual = True  # da ora non si ferma più da sola
                self.meeting.title = arg or self.meeting.title
                self.meeting.save_meta()
            else:
                self.start(True, arg)
        elif verb == "stop":
            self.stop("(manuale)")
        elif verb == "reload":
            self.s = load_settings()
            log("impostazioni ricaricate")

    def loop(self):
        MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
        log(f"taccuino in ascolto (ASR {self.s.asr[0].split('/')[-1]} @ {self.s.asr[1]}, "
            f"auto-detect {'on' if self.s.auto_detect else 'off'}, output {self.s.output_dir})")
        # riprendi elaborazioni rimaste a metà (es. dopo un riavvio)
        for d in sorted(MEETINGS_DIR.glob("*/audio.flac")):
            if not (d.parent / "transcript.json").exists():
                self.jobs.put(d.parent)
        try:
            while self.running:
                self._control()
                pids = {self.meeting.rec.pid} if self.meeting and self.meeting.rec and self.meeting.rec.pid else set()
                users = mic_users(self.s.ignore_apps, pids) if self.s.auto_detect else []
                now = time.time()
                if users:
                    self.free_since = None
                    self.mic_since = self.mic_since or now
                    if self.meeting:
                        self.meeting.apps |= {u.app for u in users}
                    elif now - self.mic_since >= self.s.start_delay:
                        self.start(False, apps=[u.app for u in users])
                else:
                    self.mic_since = None
                    self.free_since = self.free_since or now
                    if self.meeting and not self.meeting.manual and now - self.free_since >= self.s.stop_delay:
                        self.stop("(microfono libero)")
                if self.meeting and self.meeting.rec and not self.meeting.rec.alive():
                    log("ffmpeg terminato inaspettatamente")
                    self.stop("(errore cattura)")
                self.write_state([f"{u.app} ({u.binary})" for u in users])
                time.sleep(2)
        finally:
            # le elaborazioni non finite riprendono al prossimo avvio
            self.stop("(daemon fermato)")
            STATE_PATH.unlink(missing_ok=True)


def run():
    Daemon().loop()
