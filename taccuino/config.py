"""Profili hardware e percorsi.

Un profilo dice quale modello usare e su quale device OpenVINO farlo girare.
  - "xe"  : Intel Iris Xe / Arc iGPU (questo laptop, Tiger Lake)
  - "npu" : Intel Core Ultra con NPU (Meteor Lake / Lunar Lake / Arrow Lake)
  - "cpu" : fallback universale
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

DATA_DIR = Path(os.environ.get("TACCUINO_HOME", Path.home() / ".local/share/taccuino"))
MODELS_DIR = DATA_DIR / "models"
DB_PATH = DATA_DIR / "taccuino.db"
CONFIG_PATH = DATA_DIR / "config.json"


@dataclass
class Profile:
    asr_model: str
    asr_device: str
    llm_model: str
    llm_device: str
    # Proprietà extra passate a LLMPipeline (es. limiti di prompt per la NPU)
    llm_props: dict = field(default_factory=dict)
    # Numero massimo di caratteri di transcript per chiamata LLM (map-reduce oltre)
    llm_chunk_chars: int = 12000


PROFILES: dict[str, Profile] = {
    # Iris Xe: Whisper turbo int8 + Qwen3-4B int4, tutto su GPU.
    "xe": Profile(
        asr_model="OpenVINO/whisper-large-v3-turbo-int8-ov",
        asr_device="GPU",
        llm_model="OpenVINO/Qwen3-4B-int4-ov",
        llm_device="GPU",
        llm_chunk_chars=12000,
    ),
    # NPU (Core Ultra): Whisper su NPU (libera la GPU), LLM su NPU con modello
    # int4 simmetrico channel-wise ("-cw-"), l'unico formato consigliato per NPU.
    # La NPU compila il modello con lunghezza di prompt fissa: MAX_PROMPT_LEN.
    "npu": Profile(
        asr_model="OpenVINO/whisper-large-v3-turbo-int8-ov",
        asr_device="NPU",
        llm_model="OpenVINO/Qwen3-8B-int4-cw-ov",
        llm_device="NPU",
        llm_props={"MAX_PROMPT_LEN": 4096, "MIN_RESPONSE_LEN": 1024, "CACHE_DIR": str(DATA_DIR / "npu-cache")},
        llm_chunk_chars=9000,
    ),
    # NPU per l'ASR, GPU per l'LLM: spesso il miglior compromesso su Lunar Lake.
    "npu+gpu": Profile(
        asr_model="OpenVINO/whisper-large-v3-turbo-int8-ov",
        asr_device="NPU",
        llm_model="OpenVINO/Qwen3-8B-int4-ov",
        llm_device="GPU",
        llm_chunk_chars=16000,
    ),
    "cpu": Profile(
        asr_model="OpenVINO/whisper-small-int8-ov",
        asr_device="CPU",
        llm_model="OpenVINO/Qwen3-1.7B-int4-ov",
        llm_device="CPU",
        llm_chunk_chars=8000,
    ),
}


@dataclass
class Settings:
    profile: str = "xe"
    # None = rilevamento automatico; "it" / "en" per forzare la lingua
    language: str | None = None
    # Sorgenti PulseAudio/PipeWire (vedi `taccuino devices`)
    mic_source: str = "default"
    system_source: str = "@DEFAULT_MONITOR@"
    capture_system_audio: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    # Override puntuali del profilo (es. {"llm_model": "OpenVINO/gemma-4-E4B-it-int4-ov"})
    overrides: dict = field(default_factory=dict)

    def resolved_profile(self) -> Profile:
        p = Profile(**asdict(PROFILES[self.profile]))
        for k, v in self.overrides.items():
            setattr(p, k, v)
        return p


def load_settings() -> Settings:
    s = Settings()
    if CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text())
        for k, v in data.items():
            if hasattr(s, k):
                setattr(s, k, v)
    if env := os.environ.get("TACCUINO_PROFILE"):
        s.profile = env
    if s.profile not in PROFILES:
        raise SystemExit(f"Profilo sconosciuto {s.profile!r}. Disponibili: {', '.join(PROFILES)}")
    return s


def save_settings(s: Settings) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(asdict(s), indent=2))


def model_path(repo_id: str) -> Path:
    return MODELS_DIR / repo_id.split("/")[-1]
