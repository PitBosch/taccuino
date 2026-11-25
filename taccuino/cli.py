"""taccuino — note di riunione locali su Intel GPU/NPU.

  taccuino serve                     avvia la UI su http://127.0.0.1:8765
  taccuino download [--profile P]    scarica i modelli del profilo
  taccuino devices                   device OpenVINO e sorgenti audio
  taccuino transcribe FILE           trascrive un file audio/video (nessuna riproduzione)
  taccuino notes FILE                trascrive + genera le note
  taccuino bench [FILE]              misura velocità ASR e LLM sul profilo attivo
  taccuino config KEY VALUE          imposta un'opzione (profile, language, mic_source, …)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time


def main(argv=None):
    ap = argparse.ArgumentParser(prog="taccuino", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", help="xe | npu | npu+gpu | cpu")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("download")
    sub.add_parser("devices")
    t = sub.add_parser("transcribe"); t.add_argument("file"); t.add_argument("--language")
    n = sub.add_parser("notes"); n.add_argument("file"); n.add_argument("--language"); n.add_argument("--notes", default="")
    b = sub.add_parser("bench"); b.add_argument("file", nargs="?")
    c = sub.add_parser("config"); c.add_argument("key", nargs="?"); c.add_argument("value", nargs="?")
    a = ap.parse_args(argv)
    if a.profile:
        os.environ["TACCUINO_PROFILE"] = a.profile

    from .config import load_settings, save_settings
    s = load_settings()
    p = s.resolved_profile()

    if a.cmd == "serve":
        import uvicorn
        from .server import api
        print(f"Taccuino → http://{s.host}:{s.port}  (profilo {s.profile}: ASR {p.asr_device}, LLM {p.llm_device})")
        uvicorn.run(api, host=s.host, port=s.port, log_level="warning")

    elif a.cmd == "download":
        from .engines import ensure_model
        for m in {p.asr_model, p.llm_model}:
            print(ensure_model(m))

    elif a.cmd == "devices":
        import openvino as ov
        core = ov.Core()
        for d in core.available_devices:
            print(f"{d:6} {core.get_property(d, 'FULL_DEVICE_NAME')}")
        if "NPU" not in core.available_devices:
            print("NPU    non trovata (serve Core Ultra + driver intel/linux-npu-driver)")
        print("\nSorgenti audio (usa il nome in `taccuino config mic_source …`):")
        out = subprocess.run(["ffmpeg", "-hide_banner", "-sources", "pulse"], capture_output=True, text=True)
        print(out.stdout or out.stderr)

    elif a.cmd in ("transcribe", "notes"):
        from .audio import load_file, segment_array
        from .engines import ASR
        asr = ASR(p, a.language or s.language)
        x = load_file(a.file)
        segs = []
        t = time.time()
        for seg in segment_array(x):
            text, _ = asr.transcribe(seg.audio)
            if text:
                segs.append({"speaker": "them", "t0": seg.t0, "text": text})
                m, sec = divmod(int(seg.t0), 60)
                print(f"[{m:02d}:{sec:02d}] {text}", flush=True)
        el = time.time() - t
        print(f"\n# {len(x) / 16000:.0f}s di audio trascritti in {el:.0f}s (RTF {el / (len(x) / 16000):.2f})", file=sys.stderr)
        if a.cmd == "notes":
            from .engines import LLM
            from .notes import enhance
            print("\n" + "=" * 60)
            enhance(LLM(p), "", a.notes, segs, a.language or s.language,
                    on_token=lambda t: print(t, end="", flush=True),
                    on_status=lambda m: print(f"# {m}", file=sys.stderr))
            print()

    elif a.cmd == "bench":
        bench(p, a.file)

    elif a.cmd == "config":
        from dataclasses import asdict
        if not a.key:
            print(json.dumps(asdict(s), indent=2))
            return
        cur = getattr(s, a.key)
        val = a.value
        if isinstance(cur, bool):
            val = val.lower() in ("1", "true", "yes", "si", "sì")
        elif isinstance(cur, dict):
            val = json.loads(val)
        elif val in ("none", "auto", ""):
            val = None
        setattr(s, a.key, val)
        save_settings(s)
        print(f"{a.key} = {val!r}")


def bench(p, file=None):
    import numpy as np
    from .audio import load_file
    from .engines import ASR, LLM
    print(f"ASR {p.asr_model} @ {p.asr_device}")
    asr = ASR(p)
    x = load_file(file)[: 16000 * 60] if file else np.zeros(16000 * 20, np.float32)
    chunk = x[: 16000 * 20]
    asr.transcribe(chunk)  # warm-up
    t = time.time()
    txt, _ = asr.transcribe(chunk)
    el = time.time() - t
    print(f"  20s di audio → {el:.2f}s (RTF {el / 20:.3f})  «{txt[:80]}»")
    del asr
    print(f"LLM {p.llm_model} @ {p.llm_device}")
    llm = LLM(p)
    prompt = "Riassumi in tre punti: " + ("Il team ha discusso il budget del prossimo trimestre e le assunzioni. " * 60)
    toks = []
    t = time.time()
    first = [None]

    def cb(tk):
        if first[0] is None:
            first[0] = time.time() - t
        toks.append(tk)

    llm.chat("Sei un assistente.", prompt, on_token=cb, max_new_tokens=128)
    el = time.time() - t
    print(f"  prompt ~{len(prompt) // 4} token: primo token {first[0]:.2f}s, "
          f"~{len(toks) / max(el - (first[0] or 0), 1e-3):.1f} chunk/s, totale {el:.1f}s")


if __name__ == "__main__":
    main()
