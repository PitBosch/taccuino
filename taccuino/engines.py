"""Whisper (OpenVINO GenAI su GPU/NPU/CPU) e diarization (sherpa-onnx su CPU)."""
from __future__ import annotations

import re
import tarfile
import tempfile
import time
import urllib.request
from dataclasses import dataclass

import numpy as np

from .config import EMBEDDING_URL, MODELS_DIR, SEGMENTATION_URL, model_path


def ensure_whisper(repo_id: str) -> str:
    p = model_path(repo_id)
    marker = p / ".taccuino-complete"
    if not marker.exists():
        from huggingface_hub import snapshot_download
        p.parent.mkdir(parents=True, exist_ok=True)
        print(f"[taccuino] scarico {repo_id} …", flush=True)
        snapshot_download(repo_id, local_dir=str(p))
        marker.touch()
    return str(p)


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


_LANG_NAMES = {"it": "Italian", "en": "English", "fr": "French", "de": "German", "es": "Spanish"}


class ASR:
    """Whisper o Qwen3-ASR tramite openvino_genai.ASRPipeline (CPU/GPU/NPU)."""

    def __init__(self, repo_id: str, device: str, language: str | None = None, vocabulary: str = "",
                 quiet: bool = False):
        import openvino_genai as ov_genai
        t = time.time()
        path = ensure_whisper(repo_id)
        self.whisper = "whisper" in repo_id.lower()
        cls = ov_genai.WhisperPipeline if self.whisper else ov_genai.ASRPipeline
        cache = MODELS_DIR.parent / "ov_cache"  # kernel GPU compilati: il secondo avvio è molto più veloce
        cache.mkdir(parents=True, exist_ok=True)
        try:
            self.pipe = cls(path, device, CACHE_DIR=str(cache))
        except Exception:  # noqa: BLE001
            self.pipe = cls(path, device)
        self.language, self.vocabulary = language, vocabulary
        if not quiet:
            print(f"[taccuino] {repo_id.split('/')[-1]} su {device} pronto in {time.time() - t:.1f}s", flush=True)

    def transcribe(self, audio: np.ndarray) -> str:
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
        return "" if _HALLU.match(text) else text


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
