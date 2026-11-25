"""Wrapper OpenVINO GenAI per Whisper (ASR) e LLM (note/riassunti)."""
from __future__ import annotations

import re
import threading
import time
from typing import Callable, Iterable

import numpy as np

from .config import Profile, model_path


def ensure_model(repo_id: str) -> str:
    p = model_path(repo_id)
    marker = p / ".taccuino-complete"
    if not marker.exists():
        from huggingface_hub import snapshot_download
        p.parent.mkdir(parents=True, exist_ok=True)
        print(f"[taccuino] scarico {repo_id} …", flush=True)
        snapshot_download(repo_id, local_dir=str(p))
        marker.touch()
    return str(p)


class ASR:
    def __init__(self, profile: Profile, language: str | None = None):
        import openvino_genai as ov_genai
        t = time.time()
        self.pipe = ov_genai.WhisperPipeline(ensure_model(profile.asr_model), profile.asr_device)
        self.device = profile.asr_device
        self.language = language
        self.lock = threading.Lock()
        print(f"[taccuino] Whisper su {self.device} pronto in {time.time() - t:.1f}s", flush=True)

    def transcribe(self, audio: np.ndarray, language: str | None = None) -> tuple[str, str | None]:
        lang = language or self.language
        kw = {"task": "transcribe"}
        if lang:
            kw["language"] = f"<|{lang}|>"
        with self.lock:
            res = self.pipe.generate(audio.astype(np.float32).tolist(), **kw)
        text = res.texts[0].strip() if res.texts else ""
        return _clean_hallucinations(text), lang


# Frasi tipiche allucinate da Whisper sul silenzio
_HALLU = re.compile(r"^(sottotitoli.*|grazie (a tutti )?per (la visione|l'attenzione).*|thank you\.?|"
                    r"thanks for watching.*|\.+|♪+|you)$", re.I)


def _clean_hallucinations(t: str) -> str:
    return "" if _HALLU.match(t.strip()) else t


class LLM:
    def __init__(self, profile: Profile):
        import openvino_genai as ov_genai
        self.ov_genai = ov_genai
        t = time.time()
        self.pipe = ov_genai.LLMPipeline(ensure_model(profile.llm_model), profile.llm_device, **profile.llm_props)
        self.profile = profile
        self.lock = threading.Lock()
        print(f"[taccuino] LLM {profile.llm_model} su {profile.llm_device} pronto in {time.time() - t:.1f}s", flush=True)

    def chat(self, system: str, user: str, on_token: Callable[[str], None] | None = None,
             max_new_tokens=1200) -> str:
        cfg = self.ov_genai.GenerationConfig()
        cfg.max_new_tokens = max_new_tokens
        cfg.do_sample = False
        cfg.repetition_penalty = 1.05
        history = self.ov_genai.ChatHistory([
            {"role": "system", "content": system},
            # "/no_think" disattiva il ragionamento dei modelli Qwen3 (ignorato dagli altri)
            {"role": "user", "content": user + ("\n/no_think" if "qwen3" in self.profile.llm_model.lower() else "")},
        ])
        filt = _ThinkFilter(on_token)

        def streamer(sub: str):
            filt.push(sub)
            return self.ov_genai.StreamingStatus.RUNNING

        with self.lock:
            res = self.pipe.generate(history, cfg, streamer)
        out = res.texts[0] if hasattr(res, "texts") else str(res)
        return re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()


class _ThinkFilter:
    """Nasconde i blocchi <think>…</think> durante lo streaming."""

    def __init__(self, cb):
        self.cb, self.buf, self.inside = cb, "", False

    def push(self, s: str):
        if not self.cb:
            return
        self.buf += s
        while True:
            if self.inside:
                i = self.buf.find("</think>")
                if i < 0:
                    return
                self.buf, self.inside = self.buf[i + 8:].lstrip(), False
            else:
                i = self.buf.find("<think>")
                if i < 0:
                    # tieni in buffer un possibile inizio di tag
                    keep = 7 if "<" in self.buf[-7:] else 0
                    emit, self.buf = (self.buf[:-keep], self.buf[-keep:]) if keep else (self.buf, "")
                    if emit:
                        self.cb(emit)
                    return
                if i:
                    self.cb(self.buf[:i])
                self.buf, self.inside = self.buf[i + 7:], True


def chunk_text(lines: Iterable[str], max_chars: int) -> list[str]:
    chunks, cur, n = [], [], 0
    for ln in lines:
        if n + len(ln) > max_chars and cur:
            chunks.append("\n".join(cur))
            cur, n = [], 0
        cur.append(ln)
        n += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks
