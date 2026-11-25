"""Trascrizione live della riunione in corso, leggendo il FLAC che ffmpeg sta ancora scrivendo.

Il daemon non trascrive nulla durante la call (costo zero). Mentre la console è aperta:
  1. `step()` ogni ~1 s: trascrive l'audio dopo l'ultimo punto fermo → **bozza** che si aggiorna mentre parli;
     alla prima pausa (o dopo 20 s) la frase viene **fissata** in `live.jsonl`.
  2. `Cleaner`: nelle pause Claude Haiku corregge le frasi fissate (parole sbagliate, punteggiatura,
     allucinazioni tipo "Grazie." sul rumore) → `live.clean.json`. Riassunti e domande usano il testo corretto.
Niente diarization qui: le righe sono "Io" (microfono) e "Call" (audio di sistema). Il transcript completo
con gli speaker separati arriva comunque a fine riunione.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import numpy as np

from .audio import SR
from .compat import NO_WINDOW, try_lock
from .config import Settings
from .pipeline import Line, _dedupe_echo

FRAME = 480          # 30 ms
FS = FRAME / SR
THRESH = 0.006       # RMS sopra cui un frame è sicuramente "parlato"
MIN_THRESH = 0.0015  # soglia minima con audio molto basso (si adatta al rumore di fondo)
MAX_SEG = 25.0       # Whisper lavora su finestre da 30 s
COMMIT_PAUSE = 0.7   # pausa che chiude una frase
FORCE_COMMIT = 12.0  # oltre questa durata la frase viene fissata comunque (bozza sempre veloce)
LABELS = ("Io", "Call")
# Whisper su rumore/respiri "sente" queste frasi: scartale se il parlato rilevato è poco
_HALLU_SHORT = re.compile(r"^\W*(grazie( mille| a tutti| per l'attenzione| per la visione)?|ok|okay|ciao|"
                          r"sottotitoli.*|amara\.org.*)\W*$", re.I)


def _plausible(text: str, speech_secs: float) -> str:
    return "" if speech_secs < 2.0 and _HALLU_SHORT.match(text or "") else text


def _decode(path: Path, start: float) -> np.ndarray:
    """Decodifica da `start` secondi in poi, forma (n, 2). Tollera un file ancora in scrittura."""
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "quiet", "-nostdin", "-ss", f"{start:.3f}",
                          "-i", str(path), "-ac", "2", "-ar", str(SR), "-f", "s16le", "pipe:1"],
                         capture_output=True, **NO_WINDOW).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return x[: len(x) // 2 * 2].reshape(-1, 2)


def _speech(x: np.ndarray) -> np.ndarray:
    """Frame da 30 ms con parlato: soglia adattiva (3× il rumore di fondo), tra MIN_THRESH e THRESH."""
    n = len(x) // FRAME
    if n == 0:
        return np.zeros(0, dtype=bool)
    f = x[: n * FRAME].reshape(n, FRAME)
    rms = np.sqrt(np.mean(f * f, axis=1))
    thr = min(THRESH, max(MIN_THRESH, 3 * float(np.percentile(rms, 15))))
    return rms > thr


def _segments(mask: np.ndarray) -> list[tuple[float, float]]:
    """Regioni di parlato (secondi): unisce pause brevi, spezza oltre MAX_SEG."""
    regs: list[list[float]] = []
    for i in np.flatnonzero(mask):
        t = i * FS
        if regs and t - regs[-1][1] < 0.6:
            regs[-1][1] = t + FS
        else:
            regs.append([t, t + FS])
    merged: list[list[float]] = []
    for a, b in regs:
        if merged and a - merged[-1][1] < 2.0 and b - merged[-1][0] <= MAX_SEG:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    out = []
    for a, b in merged:
        if b - a < 0.4:
            continue
        while b - a > MAX_SEG:
            out.append((a, a + MAX_SEG - 5))
            a += MAX_SEG - 5
        out.append((a, b))
    return out


class LiveTranscript:
    def __init__(self, mdir: Path, s: Settings):
        self.mdir, self.s = Path(mdir), s
        self.path = self.mdir / "live.jsonl"
        self.cursor_path = self.mdir / "live.cursor"
        self.clean_path = self.mdir / "live.clean.json"
        self.draft_path = self.mdir / "live.draft"
        self.asr = None
        self.driving = False  # True se questo processo ha il turno di trascrizione (lock)
        self._draft, self._draft_len = "", 0

    def _set_draft(self, text: str, n: int):
        if text != self._draft:
            self.draft_path.write_text(text)
        self._draft, self._draft_len = text, n

    # ---------- chi trascrive ----------
    # Durante le call trascrive il "live worker" lanciato dal daemon; console e sottotitoli leggono soltanto.
    def beat(self):
        (self.mdir / "live.driver").write_text(f"{os.getpid()} {time.time():.1f}")

    def driver_alive(self) -> bool:
        """True se un ALTRO processo sta trascrivendo (heartbeat recente)."""
        try:
            pid, ts = (self.mdir / "live.driver").read_text().split()
            return int(pid) != os.getpid() and time.time() - float(ts) < 5
        except (FileNotFoundError, ValueError):
            return False

    def shared_draft(self) -> str:
        """Bozza corrente, anche se la sta producendo un altro processo (console o sottotitoli)."""
        try:
            return self.draft_path.read_text()
        except FileNotFoundError:
            return ""

    # ---------- dati ----------
    def lines(self) -> list[Line]:
        if not self.path.exists():
            return []
        return [Line(**json.loads(l)) for l in self.path.read_text().splitlines() if l.strip()]

    def cleaned(self) -> dict[int, str]:
        try:
            return {int(k): v for k, v in json.loads(self.clean_path.read_text()).items()}
        except (FileNotFoundError, ValueError):
            return {}

    def clean_lines(self) -> list[Line]:
        """Righe con la correzione di Haiku dove c'è già (le righe svuotate da Haiku spariscono)."""
        fixed = self.cleaned()
        out = []
        for i, l in enumerate(self.lines()):
            t = fixed.get(i, l.text)
            if t:
                out.append(Line(l.start, l.end, l.speaker, t))
        return out

    @property
    def cursor(self) -> float:
        try:
            return float(self.cursor_path.read_text())
        except (FileNotFoundError, ValueError):
            return 0.0

    def load(self, log=lambda _: None):
        if self.asr is None:
            from .engines import ASR
            self.asr = ASR(*self.s.asr, language=self.s.language, vocabulary=self.s.vocabulary, quiet=True)

    # ---------- trascrizione ----------
    def _commit(self, x: np.ndarray, start: float, pause: bool = False) -> list[Line]:
        """Trascrive x (fissato), lo aggiunge a live.jsonl e sposta il cursore.
        pause=True: il blocco finisce in una pausa, quindi la bozza (se lo copre) è già il testo giusto."""
        if len(x) / SR <= MAX_SEG:
            return self._write(self._short(x, start, pause), start + len(x) / SR)
        per_ch: list[list[Line]] = []
        for c in range(2):
            ch = x[:, c]
            rows = []
            for a, b in _segments(_speech(ch)):
                seg = ch[int(max(0, a - 0.2) * SR): int((b + 0.2) * SR)]
                text = self.asr.transcribe(seg)
                if text:
                    rows.append(Line(round(start + a, 1), round(start + b, 1), LABELS[c], text))
            per_ch.append(rows)
        return self._write(sorted(_dedupe_echo(per_ch[0], per_ch[1]) + per_ch[1], key=lambda l: l.start),
                           start + len(x) / SR)

    def _short(self, x: np.ndarray, start: float, pause: bool = False) -> list[Line]:
        """Blocco dello streaming (≤ 25 s): di norma UNA sola chiamata a Whisper, riusando la bozza se la copre.
        L'etichetta Io/Call viene dal canale con più parlato; solo se parlano entrambi si trascrivono separati."""
        masks = [_speech(x[:, c]) for c in range(2)]
        secs = [m.sum() * FS for m in masks]
        same = float(np.corrcoef(x[:, 0], x[:, 1])[0, 1]) > 0.5 if min(secs) > 0 else False  # eco / mono
        if min(secs) >= 1.5 and not same:
            rows = []
            for c in range(2):
                idx = np.flatnonzero(masks[c])
                if (text := _plausible(self.asr.transcribe(x[:, c]), secs[c])):
                    rows.append(Line(round(start + idx[0] * FS, 1), round(start + (idx[-1] + 1) * FS, 1),
                                     LABELS[c], text))
            mic = [r for r in rows if r.speaker == "Io"]
            call = [r for r in rows if r.speaker == "Call"]
            return sorted(_dedupe_echo(mic, call) + call, key=lambda l: l.start)
        c = int(secs[1] > secs[0])
        idx = np.flatnonzero(masks[c])
        if len(idx) * FS < 0.3:
            return []
        if pause and self._draft and self._draft_len >= len(x) - int(0.2 * SR):
            text = self._draft  # la bozza era già su questo audio: niente seconda chiamata
        else:
            text = self.asr.transcribe(x[:, 0] * 0.5 + x[:, 1] * 0.5)
        text = _plausible(text, max(secs))
        return [Line(round(start + idx[0] * FS, 1), round(start + (idx[-1] + 1) * FS, 1), LABELS[c], text)] \
            if text else []

    def _write(self, new: list[Line], cursor: float) -> list[Line]:
        with open(self.path, "a") as f:
            for l in new:
                f.write(json.dumps(l.__dict__, ensure_ascii=False) + "\n")
        self.cursor_path.write_text(f"{cursor:.3f}")
        self._set_draft("", 0)
        return new

    def _locked(self):
        f = open(self.mdir / "live.lock", "w")
        if not try_lock(f):
            f.close()
            return None
        return f

    def update(self, final: bool = False, log=lambda _: None) -> int:
        """Recupera in blocco tutto l'audio non ancora trascritto (apertura tardiva, riassunti da script)."""
        audio = self.mdir / "audio.flac"
        if not audio.exists() or (not final and self.driver_alive()) or not (lock := self._locked()):
            return 0
        with lock:
            start = self.cursor
            x = _decode(audio, start)
            if not final:
                x = x[: max(0, len(x) - SR)]  # l'ultimo secondo può essere un frame FLAC incompleto
            if len(x) < 2 * SR:
                return 0
            both = _speech(x[:, 0]) | _speech(x[:, 1])
            cut = len(both)
            if not final:  # taglia in una pausa negli ultimi 10 s
                tail = both[-int(10 / FS):]
                quiet = np.flatnonzero(~tail)
                if len(quiet):
                    cut = len(both) - len(tail) + int(quiet[-1])
            if self.asr is None:
                log("carico il modello di trascrizione …")
                self.load()
            return len(self._commit(x[: cut * FRAME], start))

    def step(self) -> tuple[list[Line], str, float]:
        """Un passo dello streaming: (righe appena fissate, bozza corrente, secondi di silenzio finale)."""
        audio = self.mdir / "audio.flac"
        lock = self._locked() if audio.exists() and not self.driver_alive() else None
        self.driving = bool(lock)
        if not lock:
            return [], self.shared_draft(), 0.0
        with lock:
            start = self.cursor
            x = _decode(audio, start)
            x = x[: max(0, len(x) - int(0.3 * SR))]
            if len(x) < SR // 2:
                return [], self._draft, 0.0
            both = _speech(x[:, 0]) | _speech(x[:, 1])
            idx = np.flatnonzero(both)
            if len(idx) and self.asr is None:
                self.load()
            if not len(idx):
                if len(x) > 5 * SR:  # solo silenzio: avanza senza trascrivere
                    self.cursor_path.write_text(f"{start + len(x) / SR - 1:.3f}")
                self._set_draft("", 0)
                return [], "", len(x) / SR
            trailing = (len(both) - 1 - idx[-1]) * FS
            pending = len(x) / SR
            cut, pause = None, False
            if trailing >= COMMIT_PAUSE:
                cut, pause = (idx[-1] + int(0.3 / FS)) * FRAME, True
            elif pending > FORCE_COMMIT:
                lo = int(FORCE_COMMIT * 0.6 / FS)  # taglia nel punto più "quieto" verso la fine
                win = both[lo: int(FORCE_COMMIT / FS)]
                quiet = np.flatnonzero(~win)
                cut = (lo + int(quiet[-1]) if len(quiet) else int(FORCE_COMMIT / FS)) * FRAME
            if cut:
                return self._commit(x[:cut], start, pause), "", trailing
            if len(x) - self._draft_len >= int(0.8 * SR):  # bozza: ritrascrivi solo se c'è audio nuovo
                mix = x[:, 0] * 0.5 + x[:, 1] * 0.5
                secs = max(_speech(x[:, 0]).sum(), _speech(x[:, 1]).sum()) * FS
                self._set_draft(_plausible(self.asr.transcribe(mix), secs), len(x))
            return [], self._draft, trailing


CLEAN_PROMPT = """Correggi il transcript automatico (Whisper) di una riunione in corso, in italiano salvo diversa lingua.
Per ogni riga NUOVA: correggi parole trascritte male (usa il contesto e il vocabolario), punteggiatura,
maiuscole e ripetizioni accidentali. NON riassumere, NON aggiungere contenuti, NON fondere o spezzare righe.
Se una riga è chiaramente un'allucinazione su rumore/silenzio (es. "Grazie." isolato, "Sottotitoli a cura di…")
o è un pezzo senza senso, restituiscila vuota.
Vocabolario (nomi e termini ricorrenti): {vocab}

Rispondi SOLO con le righe nuove corrette, una per riga, nel formato `N|testo` (stesso N dell'input).
Non usare strumenti.

CONTESTO GIÀ CORRETTO (non modificarlo):
{ctx}

RIGHE NUOVE:
{new}
"""


class Cleaner:
    """Correzione con Haiku delle righe fissate ma non ancora corrette."""

    def __init__(self, lt: LiveTranscript):
        self.lt = lt

    def pending(self) -> list[int]:
        done = self.lt.cleaned()
        return [i for i in range(len(self.lt.lines())) if i not in done]

    def run(self) -> list[int]:
        """Corregge le righe in attesa; restituisce gli indici corretti. Se Claude fallisce, tiene il grezzo."""
        from .summarize import run_claude
        lines = self.lt.lines()
        todo = self.pending()[:40]
        if not todo:
            return []
        done = self.lt.cleaned()
        ctx_idx = [i for i in range(todo[0]) if i in done][-8:]
        ctx = "\n".join(f"{lines[i].speaker}: {done[i]}" for i in ctx_idx if done[i]) or "(inizio)"
        new = "\n".join(f"{i}|{lines[i].speaker}: {lines[i].text}" for i in todo)
        fixed = {i: lines[i].text for i in todo}
        try:
            out = run_claude(self.lt.s, CLEAN_PROMPT.format(vocab=self.lt.s.vocabulary or "-", ctx=ctx, new=new),
                             timeout=90)
            for row in out.splitlines():
                m = re.match(r"^\s*(\d+)\s*\|\s*(.*)$", row)
                if m and int(m.group(1)) in fixed:
                    t = re.sub(r"^(Io|Call)\s*:\s*", "", m.group(2)).strip()
                    fixed[int(m.group(1))] = t
        except Exception:  # noqa: BLE001
            pass
        cur = self.lt.cleaned()
        cur.update(fixed)
        self.lt.clean_path.write_text(json.dumps(cur, ensure_ascii=False))
        return todo
