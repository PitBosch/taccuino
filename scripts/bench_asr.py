"""Confronta modelli ASR OpenVINO sullo stesso audio (velocità + testo).

  .venv/bin/python scripts/bench_asr.py audio.wav --device GPU --seconds 120 \
      OpenVINO/whisper-large-v3-turbo-int8-ov paloik/Qwen3-ASR-1.7B-int8-ov
Nessuna riproduzione audio: il file viene solo decodificato.
"""
import argparse
import gc
import time

from taccuino.audio import SR, load_channels
from taccuino.engines import ASR

ap = argparse.ArgumentParser()
ap.add_argument("file")
ap.add_argument("models", nargs="+")
ap.add_argument("--device", default="GPU")
ap.add_argument("--language", default="it")
ap.add_argument("--seconds", type=float, default=120)
ap.add_argument("--window", type=float, default=20)
a = ap.parse_args()

x = load_channels(a.file)[:, 0][: int(a.seconds * SR)]
win = int(a.window * SR)
chunks = [x[i:i + win] for i in range(0, len(x), win) if len(x[i:i + win]) > SR]
for m in a.models:
    t = time.time()
    asr = ASR(m, a.device, a.language)
    load = time.time() - t
    asr.transcribe(chunks[0])  # warm-up
    t = time.time()
    texts = [asr.transcribe(c) for c in chunks]
    el = time.time() - t
    print(f"\n=== {m} @ {a.device}: load {load:.1f}s, {len(x) / SR:.0f}s audio in {el:.1f}s "
          f"(RTF {el / (len(x) / SR):.3f})")
    for i, tx in enumerate(texts):
        print(f"  [{i * a.window:5.0f}s] {tx}")
    del asr
    gc.collect()
