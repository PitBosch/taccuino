"""Trascrizione live della riunione in corso, leggendo il FLAC che ffmpeg sta ancora scrivendo.

Il daemon non trascrive nulla durante la call (costo zero). Mentre la console è aperta:
  1. `step()` ogni ~1 s: trascrive l'audio dopo l'ultimo punto fermo → **bozza** che si aggiorna mentre parli;
     alla prima pausa (o dopo 20 s) la frase viene **fissata** in `live.jsonl`.
  2. `Cleaner`: nelle pause Claude Haiku corregge le frasi fissate (parole sbagliate, punteggiatura,
     allucinazioni tipo "Grazie." sul rumore) → `live.clean.json`. Riassunti e domande usano il testo corretto.
Niente diarization completa qui: le righe sono "Io" (microfono) e "Call" (audio di sistema); con `live_speakers`
le righe "Call" diventano "Call A", "Call B", … con un embedding vocale leggero (vedi `speakers.py`) e Haiku, nella
stessa chiamata di correzione, dà loro un nome quando il testo lo rende chiaro (`live.names.json`).
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
from .speakers import is_call
from .vocab import prompt_text as vocab_prompt

FRAME = 480          # 30 ms
FS = FRAME / SR
THRESH = 0.006       # RMS sopra cui un frame è sicuramente "parlato"
MIN_THRESH = 0.0015  # soglia minima con audio molto basso (si adatta al rumore di fondo)
MAX_SEG = 25.0       # Whisper lavora su finestre da 30 s
COMMIT_PAUSE = 0.7   # pausa che chiude una frase
FORCE_COMMIT = 12.0  # oltre questa durata la frase viene fissata comunque (bozza sempre veloce)
LABELS = ("Io", "Call")
# Filtro deterministico delle allucinazioni di Whisper ("Grazie.", "Sì.", "Grazie a tutti." su rumore e respiri).
# Non si butta una parola in quanto tale: si guarda se il testo è plausibile per il suono che c'era.
_HALLU_SHORT = re.compile(r"^\W*(grazie( mille| a tutti| per l'attenzione| per la visione)?|ok|okay|ciao|sì|si|no|"
                          r"sottotitoli.*|amara\.org.*|buonasera|buongiorno)\W*$", re.I)
MIN_RATE = 0.4  # parole al secondo di "parlato" rilevato: sotto, è rumore che Whisper ha riempito


def _norm(t: str) -> str:
    return re.sub(r"\W+", " ", t.lower()).strip()


def _plausible(text: str, speech_secs: float, recent: list[str] = ()) -> str:
    """Restituisce il testo se è plausibile, altrimenti "".
    1. densità: 1-2 parole da 3-12 s di suono sono un riempitivo, non parlato (un "grazie" vero dura ~0,5 s);
    2. frasi tipiche delle allucinazioni ammesse solo se brevi come quando le dici davvero (≤ 2,5 s);
    3. ripetizione: la stessa frase breve appena fissata (Whisper le ripete in serie)."""
    t = (text or "").strip()
    words = len(re.findall(r"\w+", t))
    if not words:
        return ""
    if speech_secs >= 2.0 and words / speech_secs < MIN_RATE:
        return ""
    if _HALLU_SHORT.match(t) and speech_secs > 2.5:
        return ""
    if words <= 3 and any(_norm(t) == _norm(r) for r in recent):
        return ""
    return text


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
        self.names_path = self.mdir / "live.names.json"
        self.asr = None
        self.voices = None  # CallSpeakers, creato alla prima riga "Call" (solo nel processo che trascrive)
        self.log = lambda m: print(m, flush=True)
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
            # 30 s: un passo del worker (trascrizione di un blocco, o il caricamento del modello) può durare più
            # di 5 s senza heartbeat; con soglia bassa console e sottotitoli caricavano un secondo Whisper (~2 GB)
            return int(pid) != os.getpid() and time.time() - float(ts) < 30
        except (FileNotFoundError, ValueError):
            return False

    # Console e sottotitoli segnalano che stanno guardando: solo allora il worker calcola la bozza.
    def watch(self):
        (self.mdir / "live.viewer").write_text(f"{time.time():.1f}")

    def viewer_active(self) -> bool:
        try:
            return time.time() - float((self.mdir / "live.viewer").read_text()) < 5
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

    def names(self) -> dict[str, str]:
        """Nomi dati da Haiku alle voci della call ("Call A" → nome); vuoto per le riunioni vecchie."""
        try:
            data = json.loads(self.names_path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in data.items() if v} if isinstance(data, dict) else {}
        except (FileNotFoundError, ValueError):
            return {}

    def who(self, speaker: str, names: dict[str, str] | None = None) -> str:
        """Come mostrare uno speaker: il nome se Haiku l'ha capito, altrimenti l'etichetta ("Io", "Call A", …)."""
        return (self.names() if names is None else names).get(speaker, speaker)

    def clean_lines(self) -> list[Line]:
        """Righe con la correzione di Haiku dove c'è già (le righe svuotate da Haiku spariscono) e i nomi delle
        voci della call al posto delle etichette, se noti."""
        fixed, names = self.cleaned(), self.names()
        out = []
        for i, l in enumerate(self.lines()):
            t = fixed.get(i, l.text)
            if t:
                out.append(Line(l.start, l.end, names.get(l.speaker, l.speaker), t))
        return out

    @property
    def cursor(self) -> float:
        try:
            return float(self.cursor_path.read_text())
        except (FileNotFoundError, ValueError):
            return 0.0

    def refresh_vocab(self) -> str:
        """Prompt di Whisper aggiornato con la cartella scelta per questa riunione (all'inizio vale tutta la KB);
        restituisce il testo completo per Haiku."""
        try:
            folder = json.loads((self.mdir / "meta.json").read_text()).get("folder") or None
        except (OSError, ValueError):
            folder = None
        if self.asr is not None:
            self.asr.vocabulary = vocab_prompt(self.s, 40, folder)
        return vocab_prompt(self.s, None, folder)

    def load(self, log=lambda _: None):
        if self.asr is None:
            from .engines import make_asr
            self.asr = make_asr(*self.s.asr, language=self.s.language, quiet=True)
            self.refresh_vocab()

    # ---------- trascrizione ----------
    def _commit(self, x: np.ndarray, start: float, pause: bool = False) -> list[Line]:
        """Trascrive x (fissato), lo aggiunge a live.jsonl e sposta il cursore.
        pause=True: il blocco finisce in una pausa, quindi la bozza (se lo copre) è già il testo giusto."""
        if len(x) / SR <= MAX_SEG:
            return self._write(self._short(x, start, pause), start + len(x) / SR, x, start)
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
                           start + len(x) / SR, x, start)

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
                if (text := _plausible(self.asr.transcribe(x[:, c]), secs[c], self._recent(start))):
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
        text = _plausible(text, max(secs), self._recent(start))
        return [Line(round(start + idx[0] * FS, 1), round(start + (idx[-1] + 1) * FS, 1), LABELS[c], text)] \
            if text else []

    def _recent(self, now: float, window: float = 45.0) -> list[str]:
        """Testi delle ultime righe fissate (per riconoscere le ripetizioni di Whisper)."""
        return [l.text for l in self.lines()[-4:] if now - l.end < window]

    def _label_calls(self, new: list[Line], x: np.ndarray, start: float) -> list[Line]:
        """Righe "Call" → "Call A", "Call B", … dall'embedding della voce (solo il canale di sistema della riga)."""
        if not self.s.live_speakers or not any(l.speaker == "Call" for l in new):
            return new
        if self.voices is None:
            from .speakers import CallSpeakers
            self.voices = CallSpeakers(self.mdir, self.s.speaker_threshold, log=self.log)
        out = []
        for l in new:
            if l.speaker == "Call":
                ch = x[int(max(0.0, l.start - start) * SR): int(max(0.0, l.end - start) * SR), 1]
                try:
                    l = Line(l.start, l.end, self.voices.label(ch, _speech(ch), FRAME, SR), l.text)
                except Exception as e:  # noqa: BLE001  # mai perdere una riga per colpa delle voci
                    self.log(f"voci della call: {e}")
            out.append(l)
        return out

    def _write(self, new: list[Line], cursor: float, x: np.ndarray | None = None, start: float = 0.0) -> list[Line]:
        if x is not None:
            new = self._label_calls(new, x, start)
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

    def step(self, want_draft: bool = True) -> tuple[list[Line], str, float]:
        """Un passo dello streaming: (righe appena fissate, bozza corrente, secondi di silenzio finale).
        want_draft=False: niente bozza (nessuno la guarda), solo frasi fissate alle pause → molto più leggero."""
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
            new_speech = both[int(self._draft_len / FRAME):].any() if self._draft_len < len(x) else False
            if want_draft and new_speech and len(x) - self._draft_len >= int(0.8 * SR):  # solo se c'è parlato nuovo
                mix = x[:, 0] * 0.5 + x[:, 1] * 0.5
                secs = max(_speech(x[:, 0]).sum(), _speech(x[:, 1]).sum()) * FS
                self._set_draft(_plausible(self.asr.transcribe(mix), secs), len(x))
            return [], self._draft, trailing


CLEAN_PROMPT = """Correggi il transcript automatico (Whisper) di una riunione in corso, in italiano salvo diversa lingua.
Per ogni riga NUOVA: correggi parole trascritte male (usa il contesto e il vocabolario), punteggiatura,
maiuscole e ripetizioni accidentali. NON riassumere, NON aggiungere contenuti, NON fondere o spezzare righe.
Se una riga è chiaramente un'allucinazione su rumore/silenzio (es. "Grazie." isolato, "Sottotitoli a cura di…")
o è un pezzo senza senso, restituiscila vuota.
Vocabolario (nomi e termini ricorrenti; "trascritto anche come" elenca gli errori tipici di Whisper): {vocab}

Rispondi SOLO con le righe nuove corrette, una per riga, nel formato `N|testo` (stesso N dell'input).
{names_rule}Non usare strumenti.

CONTESTO GIÀ CORRETTO (non modificarlo):
{ctx}

RIGHE NUOVE:
{new}
"""

# Aggiunta al prompt solo se tra le righe ci sono voci della call ("Call A", "Call B", …): niente chiamate in più.
NAMES_RULE = """"Io" è l'utente; "Call A", "Call B", … sono voci diverse dei partecipanti remoti (riconosciute
dal suono, possono sbagliare). Se il testo rende CHIARO il nome di una di queste voci (si presenta, oppure qualcuno
la chiama per nome e lei risponde), aggiungi in fondo una riga `NOME|Call X|Nome`. Solo con un'evidenza esplicita nel
testo: mai indovinare; nel dubbio non scrivere nulla. Nomi già noti: {known}.
"""


class Cleaner:
    """Correzione con Haiku delle righe fissate ma non ancora corrette, a blocchi (non a ogni frase)."""

    _last: dict[str, float] = {}  # ultima correzione per riunione (per processo)

    def __init__(self, lt: LiveTranscript):
        self.lt = lt

    def due(self, last_speech: float) -> bool:
        """Correggere adesso? Ogni `live_clean_seconds` (default 15 min), in una pausa. 0 = solo a fine call."""
        every = self.lt.s.live_clean_seconds
        if every <= 0:
            return False
        todo = self.pending()
        if not todo:
            return False
        now = time.time()
        last = Cleaner._last.setdefault(str(self.lt.mdir), now)
        paused = now - last_speech > 1.2
        return now - last >= every and paused

    def mark(self):
        Cleaner._last[str(self.lt.mdir)] = time.time()

    def pending(self) -> list[int]:
        done = self.lt.cleaned()
        return [i for i in range(len(self.lt.lines())) if i not in done]

    def run(self) -> list[int]:
        """Corregge TUTTE le righe in attesa, a blocchi da 60; restituisce gli indici corretti."""
        self.mark()
        done: list[int] = []
        while (chunk := self._run_chunk()):
            done += chunk
        return done

    def _run_chunk(self) -> list[int]:
        """Corregge fino a 60 righe in attesa. Se Claude fallisce, tiene il grezzo."""
        from .summarize import run_claude
        lines = self.lt.lines()
        todo = self.pending()[:60]
        if not todo:
            return []
        done = self.lt.cleaned()
        ctx_idx = [i for i in range(todo[0]) if i in done][-8:]
        ctx = "\n".join(f"{lines[i].speaker}: {done[i]}" for i in ctx_idx if done[i]) or "(inizio)"
        new = "\n".join(f"{i}|{lines[i].speaker}: {lines[i].text}" for i in todo)
        fixed = {i: lines[i].text for i in todo}
        voices = {l.speaker for l in lines if is_call(l.speaker) and l.speaker != "Call"}
        names = self.lt.names()
        rule = NAMES_RULE.format(known=", ".join(f"{k} = {v}" for k, v in sorted(names.items())) or "nessuno") \
            if voices else ""
        try:
            out = run_claude(self.lt.s, CLEAN_PROMPT.format(vocab=self.lt.refresh_vocab() or "-", ctx=ctx, new=new,
                                                            names_rule=rule), timeout=90)
            for row in out.splitlines():
                m = re.match(r"^\s*(\d+)\s*\|\s*(.*)$", row)
                if m and int(m.group(1)) in fixed:
                    t = re.sub(r"^(Io|Call(?: [A-Z])?)\s*:\s*", "", m.group(2)).strip()
                    fixed[int(m.group(1))] = t
            found = parse_names(out, voices)
            if found and {**names, **found} != names:
                names.update(found)
                self.lt.names_path.write_text(json.dumps(names, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        cur = self.lt.cleaned()
        cur.update(fixed)
        self.lt.clean_path.write_text(json.dumps(cur, ensure_ascii=False))
        return todo


_NO_NAME = re.compile(r"^(\?+|-+|n/?a|nessuno|sconosciut[oa]|ignoto|non (so|noto|chiaro).*|call( [a-z])?|io)$", re.I)


def parse_names(out: str, voices: set[str]) -> dict[str, str]:
    """Righe `NOME|Call X|Nome` della risposta di Haiku → {"Call X": "Nome"}, solo per voci esistenti e nomi
    plausibili (niente "?", "sconosciuto", frasi)."""
    found: dict[str, str] = {}
    for row in out.splitlines():
        m = re.match(r"^\W*NOME\W*\|\s*(Call [A-Z])\s*\|\s*(.+?)\s*$", row, re.I)
        if not m:
            continue
        label = "Call " + m.group(1)[-1].upper()
        name = m.group(2).strip().strip('"*`.,;:')
        if label in voices and name and not _NO_NAME.match(name) and len(name) <= 40 and len(name.split()) <= 4 \
                and not re.search(r"[|:\d]", name):
            found[label] = name
    return found
