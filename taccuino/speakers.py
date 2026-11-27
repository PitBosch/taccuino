"""Voci distinte sul canale "Call" durante la trascrizione live, in modo leggero.

Per ogni riga "Call" fissata si calcola un embedding vocale (wespeaker via sherpa-onnx, 1 thread, al massimo i primi
~3 s di parlato: ~70-110 ms di CPU) e lo si confronta con i centroidi delle voci già sentite (similarità coseno):
  - similarità migliore ≥ `speaker_threshold` → stessa voce (il centroide si aggiorna, media mobile);
  - altrimenti, se la riga ha abbastanza parlato (≥ MIN_NEW_SECS), nasce una voce nuova: "Call A", "Call B", …;
  - righe troppo corte non creano voci: vanno alla più simile se sopra soglia, altrimenti restano "Call".
Oltre MAX_VOICES voci si assegna sempre alla più simile.

Lo stato (centroidi) sta in `live.speakers.json` nella cartella della riunione: se il worker riparte o la console
diventa lei a trascrivere, le etichette restano le stesse. Scrive solo il processo che trascrive (lock `live.lock`),
gli altri al massimo leggono. Il modello si carica solo alla prima riga "Call"; se sherpa_onnx o il modello mancano,
le righe restano "Call" (lo si scrive nel log una volta sola).
"""
from __future__ import annotations

import json
import os
import string
from pathlib import Path

import numpy as np

MAX_VOICES = 8
MAX_SECS = 3.0       # parlato massimo usato per l'embedding (tempo di CPU quasi lineare nella durata)
MIN_NEW_SECS = 1.0   # sotto, la riga non crea una voce nuova (embedding poco affidabile)
MIN_SECS = 0.5       # sotto, nemmeno si prova a riconoscerla
FILE = "live.speakers.json"


def voice_label(i: int) -> str:
    return f"Call {string.ascii_uppercase[i]}"


def is_call(speaker: str) -> bool:
    """True per "Call" e per le voci "Call A", "Call B", … (non per i nomi dati da Haiku)."""
    return speaker == "Call" or (speaker.startswith("Call ") and len(speaker) == 6 and speaker[5].isupper())


def _unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).ravel()
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class Voices:
    """Clustering online degli embedding: centroidi normalizzati, similarità coseno."""

    def __init__(self, path: Path | None, threshold: float, max_voices: int = MAX_VOICES):
        self.path, self.threshold, self.max_voices = path, threshold, max_voices
        self.sums: list[np.ndarray] = []  # somma degli embedding normalizzati (il centroide è la sua direzione)
        self.counts: list[int] = []
        self._mtime = None
        self.load()

    # ---------- stato su disco ----------
    def load(self):
        """Ricarica lo stato se il file è cambiato (un altro processo può aver trascritto nel frattempo)."""
        if self.path is None:
            return
        try:
            mtime = self.path.stat().st_mtime_ns
            if mtime == self._mtime:
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
            sums = [np.asarray(v, dtype=np.float32) for v in data["sums"]]
            counts = [int(c) for c in data["counts"]]
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            return
        if len(sums) == len(counts):
            self.sums, self.counts, self._mtime = sums, counts, mtime

    def save(self):
        if self.path is None:
            return
        data = {"threshold": self.threshold, "counts": self.counts,
                "sums": [[round(float(x), 5) for x in v] for v in self.sums]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        try:
            os.replace(tmp, self.path)
        except OSError:  # Windows: un lettore può avere il file aperto
            self.path.write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")
            tmp.unlink(missing_ok=True)
        try:
            self._mtime = self.path.stat().st_mtime_ns
        except OSError:
            self._mtime = None

    # ---------- assegnazione ----------
    def best(self, emb: np.ndarray) -> tuple[int, float]:
        """(indice, similarità) della voce più simile; (-1, -1) se non ce n'è ancora nessuna."""
        if not self.sums:
            return -1, -1.0
        e = _unit(emb)
        sims = [float(np.dot(_unit(s), e)) for s in self.sums]
        i = int(np.argmax(sims))
        return i, sims[i]

    def assign(self, emb: np.ndarray, speech_secs: float) -> str:
        """Etichetta per una riga "Call" con questo embedding e `speech_secs` secondi di parlato."""
        self.load()
        i, sim = self.best(emb)
        if i >= 0 and (sim >= self.threshold or len(self.sums) >= self.max_voices):
            if speech_secs >= MIN_NEW_SECS:  # le righe corte non spostano il centroide
                self.sums[i] = self.sums[i] + _unit(emb)
                self.counts[i] += 1
                self.save()
            return voice_label(i)
        if speech_secs < MIN_NEW_SECS:
            return "Call"
        self.sums.append(_unit(emb))
        self.counts.append(1)
        self.save()
        return voice_label(len(self.sums) - 1)


class Embedder:
    """Estrattore di embedding wespeaker, caricato alla prima richiesta; None se non disponibile."""

    def __init__(self, log=print):
        self.log, self.ex, self.failed = log, None, False

    def _load(self) -> bool:
        if self.ex is not None:
            return True
        if self.failed:
            return False
        try:
            import sherpa_onnx

            from .engines import embedding_model
            model = embedding_model()
            if model is None:
                raise FileNotFoundError("modello di embedding non scaricato (taccuino download)")
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=model, num_threads=1)
            self.ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
            return True
        except Exception as e:  # noqa: BLE001
            self.failed = True
            self.log(f"voci della call: non disponibili ({e}); le righe restano \"Call\"")
            return False

    def __call__(self, samples: np.ndarray, sr: int) -> np.ndarray | None:
        if len(samples) < int(MIN_SECS * sr) or not self._load():
            return None
        try:
            st = self.ex.create_stream()
            st.accept_waveform(sr, np.ascontiguousarray(samples, dtype=np.float32))
            st.input_finished()
            if not self.ex.is_ready(st):
                return None
            return np.asarray(self.ex.compute(st), dtype=np.float32)
        except Exception as e:  # noqa: BLE001
            self.failed, self.ex = True, None
            self.log(f"voci della call: errore dell'embedding ({e}); le righe restano \"Call\"")
            return None


class CallSpeakers:
    """Collega `LiveTranscript` a embedding + clustering: dà l'etichetta alle righe "Call" appena fissate."""

    def __init__(self, mdir: Path, threshold: float, log=print, embedder=None):
        self.voices = Voices(Path(mdir) / FILE, threshold)
        self.embed = embedder or Embedder(log)

    def label(self, call_audio: np.ndarray, speech: np.ndarray, frame: int, sr: int) -> str:
        """call_audio: canale Call della riga; speech: maschera dei frame con parlato (lunghezza = frame interi)."""
        idx = np.flatnonzero(speech)
        if not len(idx):
            return "Call"
        idx = idx[: int(MAX_SECS * sr / frame)]
        samples = call_audio[: len(speech) * frame].reshape(-1, frame)[idx].ravel()
        secs = len(samples) / sr
        if secs < MIN_SECS:
            return "Call"
        emb = self.embed(samples, sr)
        if emb is None:
            return "Call"
        return self.voices.assign(emb, secs)
