"""taccuino — registra, diarizza e trascrive le riunioni in locale (Intel GPU/NPU); le note le scrive Claude Code.

  taccuino                        console interattiva (riassumi ultimi 5 min, domande, note…)
  taccuino riassumi [MIN]         riassume gli ultimi MIN minuti (default 5) della riunione
  taccuino chiedi "domanda"       domanda a Claude sulla riunione in corso (o l'ultima)
  taccuino sottotitoli            finestrina sempre in primo piano con la trascrizione live
  taccuino daemon                 avvia il servizio sempre attivo (di solito via systemd / Utilità di pianificazione)
  taccuino install-service        installa e avvia il servizio (systemd utente su Linux, attività all'accesso su Windows)
  taccuino uninstall-service      ferma e rimuove il servizio
  taccuino quit                   ferma il daemon (chiude bene la registrazione in corso)
  taccuino status                 stato del daemon e app che usano il microfono
  taccuino start ["titolo"]       avvia una registrazione manuale (es. riunione dal vivo)
  taccuino stop                   ferma la registrazione in corso
  taccuino split [DIR [MM:SS]]    senza argomenti: chiude la riunione e ne apre un'altra (call di fila);
                                  con DIR: divide una registrazione (all'istante MM:SS, o dove decide Haiku)
  taccuino note "testo"           appunto per il segretario (Haiku): nota + eventuale task Jira
  taccuino jira [ok N|no N]       proposte Jira del segretario
  taccuino vocabolario [--show]   Haiku compila il vocabolario (nomi, progetti, termini) dal vault Obsidian
  taccuino process FILE|DIR       elabora un file audio/video o una cartella riunione
  taccuino summarize DIR          rigenera solo le note di Claude
  taccuino list                   ultime riunioni elaborate
  taccuino devices                device OpenVINO e sorgenti audio
  taccuino doctor                 controlla requisiti e configurazione (sola lettura)
  taccuino setup [--yes …]        configura profilo e modello in base alla macchina (vedi --help)
  taccuino download               scarica i modelli (Whisper + diarization)
  taccuino parakeet-build         compila la libreria di Parakeet (whisper.cpp, serve git + cmake + compilatore)
  taccuino config [KEY [VALUE]]   mostra/modifica le impostazioni
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import platforms
from .platforms import IS_WIN
from .split import parse_ts
from .config import CONTROL_PATH, DATA_DIR, MEETINGS_DIR, STATE_PATH, load_settings, save_settings

def _state():
    for _ in range(5):  # il daemon riscrive il file: per un istante può essere vuoto o incompleto
        try:
            st = json.loads(STATE_PATH.read_text())
            return st if time.time() - st.get("updated", 0) < 30 else None
        except FileNotFoundError:
            return None
        except (ValueError, OSError):
            time.sleep(0.05)
    return None


def _send(cmd: str):
    if not _state():
        sys.exit("Il daemon non è in esecuzione (taccuino install-service, oppure taccuino daemon).")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONTROL_PATH.write_text(cmd)
    for _ in range(10):
        time.sleep(0.5)
        if not CONTROL_PATH.exists():
            return
    sys.exit("Il daemon non ha risposto.")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="taccuino", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", help="parakeet | xe | npu | cpu")
    sub = ap.add_subparsers(dest="cmd")
    for c in ("daemon", "install-service", "uninstall-service", "quit", "status", "stop", "list", "devices",
              "download", "parakeet-build"):
        sub.add_parser(c)
    sub.add_parser("doctor")
    st = sub.add_parser("setup")
    st.add_argument("--yes", action="store_true", help="applica senza chiedere (necessario senza terminale)")
    st.add_argument("--output-dir", default="", help="cartella dove scrivere le note")
    st.add_argument("--model", default="", help="parakeet-q4 | turbo-int8 | turbo-int4 | small-int8 | small-int4 | base-int8")
    st.add_argument("--download", action="store_true", help="scarica il modello Whisper")
    st.add_argument("--with-diarization", action="store_true", help="con --download: scarica anche la diarization")
    st.add_argument("--service", action="store_true", help="installa il servizio all'accesso")
    sub.add_parser("console").add_argument("--plain", action="store_true", help="console semplice, senza TUI")
    sub.add_parser("riassumi").add_argument("minutes", nargs="?", default="5")
    sub.add_parser("chiedi").add_argument("question", nargs="+")
    sub.add_parser("sottotitoli")
    sub.add_parser("live-worker").add_argument("dir")
    sub.add_parser("jira").add_argument("args", nargs="*")
    v = sub.add_parser("vocabolario"); v.add_argument("--vault", default=""); v.add_argument("--show", action="store_true")
    sp = sub.add_parser("split"); sp.add_argument("dir", nargs="?"); sp.add_argument("at", nargs="?")
    sub.add_parser("start").add_argument("title", nargs="?", default="")
    sub.add_parser("note").add_argument("text", nargs="+")
    p = sub.add_parser("process"); p.add_argument("path"); p.add_argument("--title", default="")
    p.add_argument("--no-summary", action="store_true")
    sub.add_parser("summarize").add_argument("dir")
    c = sub.add_parser("config"); c.add_argument("key", nargs="?"); c.add_argument("value", nargs="?")
    a = ap.parse_args(argv)
    if IS_WIN and not sys.flags.utf8_mode and a.cmd not in ("daemon", "process"):
        print("⚠ Python non è in modalità UTF-8: esegui una volta `setx PYTHONUTF8 1` e riapri il terminale "
              "(altrimenti accenti e note possono rovinarsi).", file=sys.stderr)
    if a.profile:
        os.environ["TACCUINO_PROFILE"] = a.profile
    s = load_settings()

    if a.cmd in (None, "console"):
        plain = getattr(a, "plain", False) or not (sys.stdin.isatty() and sys.stdout.isatty())
        try:
            if plain:
                raise ImportError
            from .tui import run
        except ImportError:  # niente terminale (o prompt_toolkit mancante): console semplice
            from .console import run
        run()

    elif a.cmd == "doctor":
        from .setup_wizard import doctor
        sys.exit(1 if doctor() else 0)

    elif a.cmd == "setup":
        from .setup_wizard import setup
        setup(a.yes, a.output_dir, a.model, a.profile or "", a.download, a.service, a.with_diarization)

    elif a.cmd == "vocabolario":
        from .vocab import VOCAB_PATH, build, terms
        if a.show:
            t = terms(s)
            print(f"{len(t)} termini ({VOCAB_PATH}):\n" + ", ".join(t))
        else:
            build(s, Path(a.vault).expanduser() if a.vault else None)

    elif a.cmd == "jira":
        from .console import Console
        Console().handle("jira " + " ".join(a.args))

    elif a.cmd == "live-worker":
        from .live_worker import run
        run(a.dir)

    elif a.cmd == "sottotitoli":
        from .captions import run
        run()

    elif a.cmd in ("riassumi", "chiedi"):
        from .console import Console
        c = Console()
        c.handle(f"r {a.minutes}") if a.cmd == "riassumi" else c.question(" ".join(a.question))

    elif a.cmd == "daemon":
        from .daemon import run
        run()

    elif a.cmd == "install-service":
        platforms.install_service()

    elif a.cmd == "uninstall-service":
        if _state():
            _send("quit")
        platforms.uninstall_service()

    elif a.cmd == "quit":
        _send("quit"); print("daemon fermato")

    elif a.cmd == "status":
        st = _state()
        if not st:
            print("daemon: non attivo")
        else:
            if st["recording"]:
                mins = (time.time() - st["since"]) / 60
                print(f"● registrazione in corso da {mins:.1f} min ({'manuale' if st['manual'] else ', '.join(st['apps'])})")
            else:
                print("○ in ascolto, nessuna riunione")
            print(f"microfono usato da: {', '.join(st['mic_users']) or 'nessuno'}")
            if st["processing"]:
                print(f"elaborazione: {st['processing']} (+{st['queue']} in coda)")
        print(f"profilo: {s.profile} ({s.asr[0].split("/")[-1]} @ {s.asr[1]}), output: {s.output_dir}")

    elif a.cmd == "start":
        _send(f"start {a.title}".strip()); print("● registrazione avviata")
    elif a.cmd == "split" and not a.dir:
        _send("split"); print("✂ registrazione divisa: la nuova riunione comincia adesso")

    elif a.cmd == "split":
        from .pipeline import process_meeting
        from .split import find_splits, split_meeting
        from .live import LiveTranscript
        mdir = Path(a.dir).expanduser()
        if not mdir.is_absolute() and not mdir.exists():
            mdir = MEETINGS_DIR / a.dir
        if a.at:
            cuts = [(parse_ts(a.at), "taglio manuale")]
        else:
            print("Claude cerca dove comincia la riunione successiva …")
            cuts = find_splits(s, LiveTranscript(mdir, s).clean_lines())
            if not cuts:
                sys.exit("Haiku non vede più riunioni in questa registrazione. Indica tu il punto: taccuino split DIR MM:SS")
        parts = []
        for at, why in reversed(cuts):
            parts.insert(0, split_meeting(mdir, at, why))
        for d in (mdir, *parts):
            print(process_meeting(d, s))

    elif a.cmd == "stop":
        _send("stop"); print("■ registrazione fermata, elaborazione in coda")

    elif a.cmd == "note":
        from .console import Console
        Console().handle("n " + " ".join(a.text))
        time.sleep(0.2)
        import threading
        for t in threading.enumerate():  # aspetta il segretario prima di uscire
            if t is not threading.main_thread():
                t.join(timeout=120)

    elif a.cmd == "list":
        rows = []
        for m in sorted(MEETINGS_DIR.glob("*/meta.json"), reverse=True)[:20]:
            meta = json.loads(m.read_text())
            state = "✓" if meta.get("output") else "…"
            rows.append(f"{state} {datetime.fromtimestamp(meta['started']):%d/%m %H:%M}  "
                        f"{meta.get('title') or '-':40.40}  {meta.get('output', m.parent)}")
        print("\n".join(rows) or "nessuna riunione")

    elif a.cmd == "devices":
        import openvino as ov
        core = ov.Core()
        for d in core.available_devices:
            print(f"{d:6} {core.get_property(d, 'FULL_DEVICE_NAME')}")
        if "NPU" not in core.available_devices:
            print("NPU    non trovata (serve Core Ultra + driver NPU Intel"
                  f"{'' if IS_WIN else ': intel/linux-npu-driver'})")
        print("\n" + platforms.list_audio_devices())
        if not shutil.which("ffmpeg"):
            print("\n⚠ ffmpeg non trovato nel PATH" + (f" ({platforms.ffmpeg_hint})" if platforms.ffmpeg_hint else ""))

    elif a.cmd == "download":
        from .engines import ensure_diarization_models, ensure_whisper
        print(ensure_whisper(s.asr[0])); print(*ensure_diarization_models(), sep="\n")
        from .config import is_parakeet
        from .native import lib_path
        if is_parakeet(s.asr[0]) and not lib_path().exists():
            print("Per Parakeet serve anche la libreria: taccuino parakeet-build")

    elif a.cmd == "parakeet-build":
        from .native import build
        build()

    elif a.cmd == "process":
        from .pipeline import process_meeting
        src = Path(a.path).expanduser().resolve()
        if src.is_dir():
            mdir = src
        else:
            MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
            started = src.stat().st_mtime
            mdir = MEETINGS_DIR / f"{datetime.fromtimestamp(started):%Y-%m-%d_%H%M%S}-import"
            mdir.mkdir(exist_ok=True)
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(src),
                            "-ac", "1", "-ar", "16000", "-c:a", "flac", str(mdir / "audio.flac")], check=True)
            (mdir / "meta.json").write_text(json.dumps({"started": started, "manual": True,
                                                        "title": a.title, "apps": [], "source": str(src)}))
        if a.no_summary:
            s.summarize = False
        print(process_meeting(mdir, s))

    elif a.cmd == "summarize":
        from .pipeline import resummarize
        resummarize(Path(a.dir).expanduser(), s)

    elif a.cmd == "config":
        from dataclasses import asdict
        if not a.key:
            print(json.dumps(asdict(s), indent=2, ensure_ascii=False)); return
        cur = getattr(s, a.key)
        val = a.value or ""
        if isinstance(cur, bool):
            val = val.lower() in ("1", "true", "yes", "si", "sì", "on")
        elif isinstance(cur, (int, float)) and not isinstance(cur, bool):
            val = type(cur)(val)
        elif isinstance(cur, (list, dict)):
            val = json.loads(val)
        elif val.lower() in ("none", "auto", ""):
            val = None
        setattr(s, a.key, val)
        save_settings(s)
        if _state():
            CONTROL_PATH.write_text("reload")
        print(f"{a.key} = {val!r}")


if __name__ == "__main__":
    main()
