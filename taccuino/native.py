"""Parakeet su whisper.cpp: compilazione della libreria nativa (`taccuino parakeet-build`) e caricamento con ctypes.

La libreria si costruisce dai sorgenti ufficiali di whisper.cpp, al commit fissato in `native/whisper.cpp.commit`,
con la patch `native/whisper.cpp-parakeet-stream.patch`, che aggiunge:
  - lo streaming dei pesi dell'encoder: le grandi matrici di ogni strato si leggono dal file mentre si calcola lo
    strato precedente (2 slot da ~14 MB in RAM invece di ~330 MB);
  - `taccuino-asr`, una libreria con tre funzioni C (tq_open, tq_transcribe, tq_close) chiamate da Python.
Serve git, cmake e un compilatore C++ (g++ su Linux, Visual Studio Build Tools su Windows). Su CPU con AVX2/AVX512.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import platforms
from .config import MODELS_DIR

WHISPER_CPP_URL = "https://github.com/ggml-org/whisper.cpp"
NATIVE_SRC = Path(__file__).resolve().parent.parent / "native"
BUILD_ROOT = MODELS_DIR.parent / "native"


def lib_path() -> Path:
    if env := os.environ.get("TACCUINO_ASR_LIB"):
        return Path(env)
    return BUILD_ROOT / platforms.native_lib_name


def _run(*cmd: str, cwd: Path | None = None) -> None:
    print("[taccuino] $", " ".join(cmd), flush=True)
    subprocess.run(list(cmd), cwd=cwd, check=True)


def build(jobs: int = 0) -> Path:
    """Scarica whisper.cpp al commit fissato, applica la patch e compila la libreria. Restituisce il percorso."""
    for exe in ("git", "cmake"):
        if not shutil.which(exe):
            sys.exit(f"Manca {exe}. Installa: {platforms.build_hint}")
    commit = (NATIVE_SRC / "whisper.cpp.commit").read_text().strip()
    patch = NATIVE_SRC / "whisper.cpp-parakeet-stream.patch"
    src = BUILD_ROOT / "whisper.cpp"
    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    if not (src / ".git").exists():
        _run("git", "init", "-q", str(src))
        _run("git", "remote", "add", "origin", WHISPER_CPP_URL, cwd=src)
    _run("git", "fetch", "-q", "--depth", "1", "origin", commit, cwd=src)
    _run("git", "checkout", "-q", "-f", commit, cwd=src)
    _run("git", "clean", "-q", "-fdx", "-e", "build", cwd=src)
    _run("git", "apply", "--whitespace=nowarn", str(patch), cwd=src)
    bdir = src / "build"
    # libreria unica: ggml e parakeet statici dentro taccuino-asr; niente GPU (si usa la CPU)
    _run("cmake", "-S", str(src), "-B", str(bdir), "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=OFF",
         "-DGGML_VULKAN=OFF", "-DGGML_CUDA=OFF", "-DGGML_CCACHE=OFF", "-DWHISPER_BUILD_TESTS=OFF",
         "-DWHISPER_BUILD_SERVER=OFF", "-DCMAKE_POSITION_INDEPENDENT_CODE=ON")
    _run("cmake", "--build", str(bdir), "--config", "Release", "--target", "taccuino-asr",
         "-j", str(jobs or os.cpu_count() or 4))
    found = sorted(bdir.rglob(platforms.native_lib_name), key=lambda p: p.stat().st_mtime)
    if not found:
        sys.exit(f"Compilazione finita ma {platforms.native_lib_name} non trovata in {bdir}")
    out = BUILD_ROOT / platforms.native_lib_name
    shutil.copy2(found[-1], out)
    print(f"[taccuino] libreria Parakeet pronta: {out}", flush=True)
    return out


def load(path: Path | None = None) -> ctypes.CDLL:
    p = path or lib_path()
    if not p.exists():
        raise RuntimeError(f"libreria Parakeet non trovata ({p}): esegui `taccuino parakeet-build`")
    lib = ctypes.CDLL(str(p))
    lib.tq_open.restype = ctypes.c_void_p
    lib.tq_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    lib.tq_transcribe.restype = ctypes.c_char_p
    lib.tq_transcribe.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_int]
    lib.tq_close.restype = None
    lib.tq_close.argtypes = [ctypes.c_void_p]
    return lib
