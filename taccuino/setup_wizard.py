"""Diagnosi e configurazione guidata, uguali su Linux e Windows.

  taccuino doctor                    controlla tutto in sola lettura (OpenVINO, RAM, ffmpeg, Claude, modelli, audio…)
  taccuino setup [opzioni]           propone profilo e modello in base alla macchina e scrive la configurazione

`setup` non cambia nulla che hai già impostato, salvo quello che chiedi con le opzioni. Senza terminale
(es. lanciato da Claude Code) serve `--yes`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import platforms
from .config import ASR_MODELS, ASR_PROFILES, CONFIG_PATH, DATA_DIR, load_settings, model_path, save_settings

# Soglie euristiche, NON misurate: il solo dato misurato è turbo-int8 ≈ 1-2 GB di RAM residenti con Whisper caricato.
# Verifica con `python scripts/bench_asr.py` prima di fidarti di un modello più piccolo.
RAM_FOR_TURBO_INT8 = 12.0
RAM_FOR_TURBO_INT4 = 8.0


def ov_devices() -> list[str]:
    try:
        import openvino as ov
        return list(ov.Core().available_devices)
    except Exception:  # noqa: BLE001
        return []


def recommend(ram_gb: float, devices: list[str]) -> tuple[str, str, str]:
    """(profilo, chiave del modello, motivo). L'NPU non è mai scelta da sola: è instabile nelle call lunghe
    (ZE_RESULT_ERROR_DEVICE_LOST visto 2 volte su Core Ultra 7 155H, causa non confermata); si sceglie con --profile npu."""
    if not any(d.startswith("GPU") for d in devices):
        return "cpu", "small-int8", "nessuna GPU Intel trovata: si usa la CPU con un modello piccolo"
    if ram_gb >= RAM_FOR_TURBO_INT8:
        return "xe", "turbo-int8", f"GPU trovata e {ram_gb:.0f} GB di RAM: modello più preciso"
    if ram_gb >= RAM_FOR_TURBO_INT4:
        return "xe", "turbo-int4", f"GPU trovata e solo {ram_gb:.0f} GB di RAM: versione int4 (da verificare con il benchmark)"
    return "xe", "small-int8", f"GPU trovata e solo {ram_gb:.0f} GB di RAM: modello piccolo"


def _claude_ok(claude_bin: str) -> str:
    exe = shutil.which(claude_bin) or claude_bin
    try:
        r = subprocess.run(platforms.command(exe, "--version"), capture_output=True, text=True, timeout=20,
                           **platforms.NO_WINDOW)
        return r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else ""
    except Exception:  # noqa: BLE001
        return ""


def doctor() -> int:
    """Stampa lo stato di ogni requisito; restituisce il numero di problemi bloccanti."""
    from .vocab import find_vault
    s = load_settings()
    bad = 0

    def line(ok: bool | None, what: str, detail: str = ""):
        nonlocal bad
        bad += ok is False
        print(f"{'✓' if ok else ('✗' if ok is False else '·')} {what}" + (f": {detail}" if detail else ""))

    line(sys.version_info >= (3, 10), "Python ≥ 3.10", sys.version.split()[0])
    line(True, "sistema", "Windows" if platforms.IS_WIN else "Linux")
    devs = ov_devices()
    line(bool(devs), "OpenVINO", ", ".join(devs) or "non importabile (pip install -e .)")
    ram = platforms.total_ram_gb()
    line(ram > 0 or None, "RAM", f"{ram:.1f} GB")
    for exe in ("ffmpeg", "ffprobe"):
        line(bool(shutil.which(exe)), exe, shutil.which(exe) or platforms.ffmpeg_hint or "da installare")
    ver = _claude_ok(s.claude_bin)
    line(bool(ver), "Claude Code (note, titoli, cartelle)", ver or f"non trovato ({s.claude_bin})")
    model, device = s.asr
    have = (model_path(model) / ".taccuino-complete").exists()
    line(have, f"modello {model.split('/')[-1]} su {device}", "scaricato" if have else "da scaricare: taccuino download")
    if device != "CPU" and devs:
        line(any(d.startswith(device) for d in devs), f"dispositivo {device} presente")
    out = Path(s.output_dir).expanduser()
    line(out.is_dir() or None, "cartella delle note", f"{out}{'' if out.is_dir() else ' (verrà creata)'}")
    vault = find_vault(s)
    line(None, "vault Obsidian per il vocabolario", str(vault) if vault else "non trovato (facoltativo)")
    if platforms.IS_WIN and not sys.flags.utf8_mode:
        line(None, "PYTHONUTF8", "non attivo in questa shell: `setx PYTHONUTF8 1` e riapri il terminale")
    from .cli import _state
    st = _state()
    line(None, "daemon", "attivo" if st else "non attivo (taccuino install-service)")
    print()
    print(platforms.list_audio_devices().strip()[:1500])
    return bad


def setup(yes: bool = False, output_dir: str = "", model: str = "", profile: str = "", download: bool = False,
          service: bool = False, with_diarization: bool = False) -> None:
    from .vocab import find_vault
    s = load_settings()
    raw = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    devs, ram = ov_devices(), platforms.total_ram_gb()
    rec_profile, rec_model, why = recommend(ram, devs)
    new_profile = profile or (None if "profile" in raw else rec_profile)
    new_model_key = model or (None if "asr_model" in raw or "profile" in raw else rec_model)
    if model and model not in ASR_MODELS:
        sys.exit(f"Modello sconosciuto {model!r}. Disponibili: {', '.join(ASR_MODELS)}")
    if new_profile and new_profile not in ASR_PROFILES:
        sys.exit(f"Profilo sconosciuto {new_profile!r}. Disponibili: {', '.join(ASR_PROFILES)}")

    changes: dict = {}
    if new_profile:
        changes["profile"] = new_profile
    if new_model_key:
        repo = ASR_MODELS[new_model_key]
        default_for_profile = ASR_PROFILES[new_profile or s.profile][0]
        changes["asr_model"] = None if repo == default_for_profile else repo
    if output_dir:
        changes["output_dir"] = output_dir
    vault = find_vault(s)
    if vault and "vault_path" not in raw:
        changes["vault_path"] = str(vault)  # solo per il vocabolario; le note vanno dove dici tu (--output-dir)

    print(f"Macchina: {'Windows' if platforms.IS_WIN else 'Linux'}, {ram:.1f} GB di RAM, dispositivi OpenVINO: "
          f"{', '.join(devs) or 'nessuno'}")
    print(f"Scelta consigliata: profilo {rec_profile}, modello {rec_model} — {why}")
    print("Modifiche alla configurazione:" if changes else "Nessuna modifica alla configurazione.")
    for k, v in changes.items():
        print(f"  {k} = {v!r}")
    if changes and not yes:
        try:
            answer = input("Applico? [S/n] ") if sys.stdin.isatty() else None
        except EOFError:  # su Windows NUL risulta "tty" ma non si può leggere
            answer = None
        if answer is None:
            sys.exit("\nSenza terminale serve --yes per applicare.")
        if answer.strip().lower() in ("n", "no"):
            sys.exit("Annullato.")
    if changes:
        for k, v in changes.items():
            setattr(s, k, v)
        save_settings(s)
        print(f"Configurazione salvata in {CONFIG_PATH}")

    s = load_settings()
    if download:
        from .engines import ensure_diarization_models, ensure_whisper
        print(ensure_whisper(s.asr[0]))
        if with_diarization:
            print(*ensure_diarization_models(), sep="\n")
    if service:
        platforms.install_service()
    print(f"\nDati in {DATA_DIR}. Controllo finale: taccuino doctor")
