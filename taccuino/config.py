"""Impostazioni e percorsi.

Profili ASR (Whisper via OpenVINO):
  - "xe"  : Intel Iris Xe / Arc iGPU (default)
  - "npu" : Intel Core Ultra con NPU: la trascrizione non occupa la GPU
  - "cpu" : fallback
La diarization gira sempre su CPU (sherpa-onnx, leggera).
I riassunti li scrive Claude Code (`claude -p`), sempre con Haiku.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

def _default_data_dir() -> Path:
    if sys.platform == "win32":  # %LOCALAPPDATA%\taccuino
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "taccuino"
    return Path.home() / ".local/share/taccuino"


DATA_DIR = Path(os.environ.get("TACCUINO_HOME", _default_data_dir()))
MODELS_DIR = DATA_DIR / "models"
MEETINGS_DIR = DATA_DIR / "meetings"
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
CONTROL_PATH = DATA_DIR / "control"

ASR_PROFILES = {
    "xe": ("OpenVINO/whisper-large-v3-turbo-int8-ov", "GPU"),
    "npu": ("OpenVINO/whisper-large-v3-turbo-int8-ov", "NPU"),
    "cpu": ("OpenVINO/whisper-small-int8-ov", "CPU"),
}

SEGMENTATION_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
                    "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
EMBEDDING_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
                 "wespeaker_en_voxceleb_resnet34_LM.onnx")


def _default_output_dir() -> str:
    homes = [Path.home()]
    if sys.platform == "win32" and os.environ.get("OneDrive"):  # Documenti spesso spostati su OneDrive
        homes.insert(0, Path(os.environ["OneDrive"]))
    for home in homes:
        for name in ("Documenti", "Documents"):
            if (home / name).is_dir():
                return str(home / name / "Taccuino")
    return str(Path.home() / "Taccuino")


@dataclass
class Settings:
    profile: str = "xe"
    # None = rilevamento automatico; "it" / "en" per forzare la lingua
    language: str | None = "it"
    mic_source: str = "default"
    system_source: str = "@DEFAULT_MONITOR@"
    output_dir: str = field(default_factory=_default_output_dir)
    # sottocartelle dell'archivio dove Claude non deve mai archiviare note (es. cartelle di servizio di un vault)
    exclude_folders: list = field(default_factory=list)

    # --- rilevamento automatico delle riunioni ---
    auto_detect: bool = True
    # app che tengono il microfono aperto ma non sono riunioni (nome binario o application.name)
    ignore_apps: list = field(default_factory=lambda: ["ffmpeg", "pw-record", "parecord", "arecord",
                                                       "easyeffects", "obs", "speech-dispatcher"])
    start_delay: float = 5.0    # secondi di microfono in uso prima di iniziare
    stop_delay: float = 20.0    # secondi di microfono libero prima di chiudere
    min_meeting_seconds: float = 60.0  # registrazioni più corte vengono scartate

    # --- diarization ---
    num_speakers: int = -1        # -1 = automatico
    cluster_threshold: float = 0.6  # più alto = meno speaker

    # --- Claude Code ---
    summarize: bool = True
    claude_bin: str = field(default_factory=lambda: shutil.which("claude") or str(
        Path.home() / ".local/bin" / ("claude.exe" if sys.platform == "win32" else "claude")))
    claude_extra_prompt: str = ""    # istruzioni aggiuntive (es. "usa sempre l'inglese")

    # --- durante la call ---
    live_transcribe: bool = True     # trascrizione in streaming durante la call (processo separato)
    live_clean_seconds: float = 900.0  # ogni quanto Haiku corregge le frasi trascritte (0 = solo a fine call)
    live_notes_minutes: float = 15.0  # ogni quanti minuti Claude aggiunge appunti alla nota (0 = mai)
    # a fine call: False = nota dalla trascrizione live (veloce, niente GPU); True = ritrascrive tutto con
    # diarization (speaker separati, più preciso, qualche minuto di GPU)
    final_transcribe: bool = False

    # --- Jira (tramite il connettore Atlassian di Claude Code) ---
    jira_project: str = ""          # chiave del progetto (vuoto = il tuo progetto personale)
    jira_issue_type: str = "Task"
    jira_mcp: str = ""              # nome del connettore in `claude mcp list` (vuoto = trovato da solo)
    jira_url: str = ""              # sito Atlassian, solo se ne hai più di uno

    keep_audio_days: int = 14  # 0 = cancella l'audio dopo l'elaborazione

    # --- trascrizione ---
    asr_model: str | None = None  # override del modello del profilo (repo Hugging Face OpenVINO)
    vocabulary: str = ""  # nomi/termini ricorrenti, es. "Paolino, Floripa, Kubernetes"

    @property
    def asr(self) -> tuple[str, str]:
        model, device = ASR_PROFILES[self.profile]
        return self.asr_model or model, device


def load_settings() -> Settings:
    s = Settings()
    if CONFIG_PATH.exists():
        for k, v in json.loads(CONFIG_PATH.read_text()).items():
            if hasattr(s, k):
                setattr(s, k, v)
    if env := os.environ.get("TACCUINO_PROFILE"):
        s.profile = env
    if s.profile not in ASR_PROFILES:
        raise SystemExit(f"Profilo sconosciuto {s.profile!r}. Disponibili: {', '.join(ASR_PROFILES)}")
    return s


def save_settings(s: Settings) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(asdict(s), indent=2))


def model_path(repo_id: str) -> Path:
    return MODELS_DIR / repo_id.split("/")[-1]
