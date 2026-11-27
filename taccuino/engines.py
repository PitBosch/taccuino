"""Trascrizione (Whisper su OpenVINO GenAI, oppure Parakeet su whisper.cpp in CPU) e diarization (sherpa-onnx su CPU)."""
from __future__ import annotations

import gc
import re
import tarfile
import tempfile
import time
import urllib.request
from dataclasses import dataclass

import numpy as np

from .config import EMBEDDING_URL, MODELS_DIR, SEGMENTATION_URL, is_parakeet, model_path

PARAKEET_URL = "https://huggingface.co/ggml-org/parakeet-GGUF/resolve/main/ggml-{name}.bin"


def ensure_whisper(repo_id: str) -> str:
    if is_parakeet(repo_id):
        return ensure_parakeet(repo_id)
    p = model_path(repo_id)
    marker = p / ".taccuino-complete"
    if not marker.exists():
        from huggingface_hub import snapshot_download
        p.parent.mkdir(parents=True, exist_ok=True)
        print(f"[taccuino] scarico {repo_id} …", flush=True)
        snapshot_download(repo_id, local_dir=str(p))
        marker.touch()
    return str(p)


def ensure_parakeet(repo_id: str) -> str:
    """Modello Parakeet in formato ggml (un file solo); la cartella ha lo stesso marcatore dei modelli Whisper."""
    p = model_path(repo_id)
    f = p / "model.bin"
    marker = p / ".taccuino-complete"
    if not marker.exists():
        p.mkdir(parents=True, exist_ok=True)
        print(f"[taccuino] scarico {repo_id} …", flush=True)
        tmp = f.with_suffix(".part")
        urllib.request.urlretrieve(PARAKEET_URL.format(name=repo_id.split("/")[-1]), tmp)
        tmp.replace(f)
        marker.touch()
    return str(f)


def make_asr(repo_id: str, device: str, language: str | None = None, vocabulary: str = "", quiet: bool = False):
    """Il motore giusto per il modello configurato: Parakeet (whisper.cpp) oppure Whisper/Qwen3 (OpenVINO)."""
    if is_parakeet(repo_id):
        return ParakeetASR(repo_id, device, language, vocabulary, quiet)
    return ASR(repo_id, device, language, vocabulary, quiet)


def ensure_diarization_models() -> tuple[str, str]:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    seg = MODELS_DIR / "sherpa-onnx-pyannote-segmentation-3-0" / "model.int8.onnx"
    emb = MODELS_DIR / EMBEDDING_URL.rsplit("/", 1)[1]
    if not seg.exists():
        print("[taccuino] scarico modello di segmentazione …", flush=True)
        with tempfile.NamedTemporaryFile(suffix=".tar.bz2") as f:
            urllib.request.urlretrieve(SEGMENTATION_URL, f.name)
            with tarfile.open(f.name) as t:
                t.extractall(MODELS_DIR, filter="data")
    if not emb.exists():
        print("[taccuino] scarico modello di embedding speaker …", flush=True)
        urllib.request.urlretrieve(EMBEDDING_URL, emb)
    return str(seg), str(emb)


def embedding_model() -> str | None:
    """Percorso del modello di embedding speaker se è già scaricato (non scarica nulla: serve al live)."""
    p = MODELS_DIR / EMBEDDING_URL.rsplit("/", 1)[1]
    return str(p) if p.exists() else None


_LANG_NAMES = {"it": "Italian", "en": "English", "fr": "French", "de": "German", "es": "Spanish"}


class ASR:
    """Whisper o Qwen3-ASR tramite openvino_genai.ASRPipeline (CPU/GPU/NPU)."""

    def __init__(self, repo_id: str, device: str, language: str | None = None, vocabulary: str = "",
                 quiet: bool = False):
        import openvino_genai as ov_genai
        t = time.time()
        self.path = ensure_whisper(repo_id)
        self.whisper = "whisper" in repo_id.lower()
        self.cls = ov_genai.WhisperPipeline if self.whisper else ov_genai.ASRPipeline
        self.cache = MODELS_DIR.parent / "ov_cache"  # kernel GPU/NPU compilati: il secondo avvio è molto più veloce
        self.cache.mkdir(parents=True, exist_ok=True)
        self.device, self.pipe = device, None
        self._open(device)
        self.language, self.vocabulary = language, vocabulary
        if not quiet:
            print(f"[taccuino] {repo_id.split('/')[-1]} su {self.device} pronto in {time.time() - t:.1f}s", flush=True)

    def _open(self, device: str):
        self.pipe = None  # libera il modello precedente prima di ricaricarlo (un solo modello in RAM)
        gc.collect()
        try:
            props = {"CACHE_DIR": str(self.cache)}
            if device.startswith("GPU"):
                # senza questo il driver tiene un core al 100% in attesa attiva mentre la GPU lavora:
                # stessa velocità, ~8× meno CPU (2,65 s → 0,34 s per chiamata su Iris Xe)
                props.update(GPU_QUEUE_THROTTLE="LOW", GPU_HOST_TASK_PRIORITY="LOW")
            self.pipe = self.cls(self.path, device, **props)
        except Exception:  # noqa: BLE001
            self.pipe = self.cls(self.path, device)
        self.device = device

    def transcribe(self, audio: np.ndarray) -> str:
        try:
            return self._transcribe(audio)
        except Exception as e:  # noqa: BLE001
            # device caduto (es. DEVICE_LOST): ricarica lo STESSO modello sullo stesso device e riprova, max 2 volte
            for attempt in (1, 2):
                print(f"[taccuino] errore su {self.device} ({str(e)[:120]}): ricarico e riprovo ({attempt}/2)",
                      flush=True)
                time.sleep(2)
                try:
                    self._open(self.device)
                    return self._transcribe(audio)
                except Exception as e2:  # noqa: BLE001
                    e = e2
            raise e

    def _transcribe(self, audio: np.ndarray) -> str:
        kw: dict = {}
        if self.whisper:
            kw["task"] = "transcribe"
            if self.language:
                kw["language"] = f"<|{self.language}|>"
            if self.vocabulary:
                kw["initial_prompt"] = self.vocabulary
        else:
            if self.language:
                kw["language"] = _LANG_NAMES.get(self.language, self.language)
            if self.vocabulary:
                kw["context"] = self.vocabulary
        res = self.pipe.generate(audio.astype(np.float32).tolist(), **kw)
        text = res.texts[0].strip() if res.texts else ""
        text = re.sub(r"^language \w+<asr_text>", "", text).strip()  # prefisso Qwen3-ASR
        if self.vocabulary and len(text.split()) >= 3 and text.lower().strip(" .,") in self.vocabulary.lower():
            return ""  # su silenzio/rumore Whisper può ripetere il prompt (l'elenco di termini)
        return "" if _HALLU.match(text) else text


def split_quiet(x: np.ndarray, max_secs: float, sr: int = 16000) -> list[np.ndarray]:
    """Taglia l'audio in pezzi ≤ max_secs, ognuno nel punto più silenzioso dell'ultimo terzo (per non spezzare parole)."""
    out, n, hop = [], int(max_secs * sr), sr // 20
    while x.size > n:
        lo = int(n * 2 / 3)
        frames = x[lo:n][: (n - lo) // hop * hop].reshape(-1, hop)
        cut = lo + int(np.argmin((frames ** 2).mean(axis=1))) * hop + hop // 2
        out.append(x[:cut])
        x = x[cut:]
    out.append(x)
    return out


class ParakeetASR:
    """Parakeet TDT 0.6B v3 su whisper.cpp in CPU, con i pesi dell'encoder letti dal disco strato per strato.

    Lingua riconosciuta da solo (25 lingue europee) e nessun prompt: il vocabolario lo usa Haiku quando rivede il
    testo. Stessa interfaccia di `ASR` (`transcribe`, `vocabulary`, `device`)."""

    def __init__(self, repo_id: str, device: str = "CPU", language: str | None = None, vocabulary: str = "",
                 quiet: bool = False, threads: int = 4):
        from . import native
        t = time.time()
        self.path = ensure_parakeet(repo_id)
        self.device, self.language, self.vocabulary = "CPU", language, vocabulary
        if device != "CPU":
            raise RuntimeError(f"Parakeet gira solo su CPU (configurato: {device})")
        self.lib = native.load()
        self.h = self.lib.tq_open(self.path.encode("utf-8"), threads, 1)
        if not self.h:
            raise RuntimeError(f"Parakeet: impossibile caricare {self.path}")
        if not quiet:
            print(f"[taccuino] {repo_id.split('/')[-1]} su CPU pronto in {time.time() - t:.1f}s", flush=True)

    MAX_SECS = 30.0  # oltre si taglia: la RAM delle attivazioni cresce con la durata

    def transcribe(self, audio: np.ndarray) -> str:
        x = np.ascontiguousarray(audio, dtype=np.float32)
        parts = [self._one(c) for c in split_quiet(x, self.MAX_SECS)]
        return " ".join(p for p in parts if p)

    def _one(self, x: np.ndarray) -> str:
        import ctypes
        if x.size < 1600:  # meno di 0,1 s
            return ""
        r = self.lib.tq_transcribe(self.h, x.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), int(x.size))
        if r is None:
            raise RuntimeError("Parakeet: trascrizione fallita")
        text = r.decode("utf-8", "replace").strip()
        return "" if _HALLU.match(text) else text

    def close(self) -> None:
        if getattr(self, "h", None):
            self.lib.tq_close(self.h)
            self.h = None

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


# Frasi tipiche allucinate da Whisper su silenzio/rumore
_HALLU = re.compile(r"^(sottotitoli.*|sottotitolato.*|grazie (a tutti )?per (la visione|l'attenzione).*|"
                    r"thank you\.?|thanks for watching.*|[.…\s]*|♪+|you\.?)$", re.I)


@dataclass
class Turn:
    start: float
    end: float
    speaker: int


class Diarizer:
    def __init__(self, num_speakers=-1, threshold=0.6):
        import sherpa_onnx
        seg, emb = ensure_diarization_models()
        cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=seg), num_threads=4),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=emb, num_threads=4),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_speakers, threshold=threshold),
            min_duration_on=0.3, min_duration_off=0.5)
        if not cfg.validate():
            raise RuntimeError("configurazione diarization non valida")
        self.sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)

    def __call__(self, x: np.ndarray) -> list[Turn]:
        res = self.sd.process(np.ascontiguousarray(x, dtype=np.float32)).sort_by_start_time()
        return [Turn(r.start, r.end, r.speaker) for r in res]
