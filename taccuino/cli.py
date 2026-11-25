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
  taccuino note "testo"           aggiunge un appunto con timestamp alla riunione in corso
  taccuino process FILE|DIR       elabora un file audio/video o una cartella riunione
  taccuino summarize DIR          rigenera solo le note di Claude
  taccuino list                   ultime riunioni elaborate
  taccuino devices                device OpenVINO e sorgenti audio
  taccuino download               scarica i modelli (Whisper + diarization)
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

from .compat import IS_WIN
from .config import CONTROL_PATH, DATA_DIR, MEETINGS_DIR, STATE_PATH, load_settings, save_settings

SERVICE = """[Unit]
Description=Taccuino - note di riunione locali
After=pipewire.service pipewire-pulse.service
Wants=pipewire-pulse.service

[Service]
ExecStart={exe} daemon
Restart=on-failure
RestartSec=5
Environment=PATH={path}
Nice=5

[Install]
WantedBy=default.target
"""


WIN_TASK = "Taccuino"


def _win_install_service():
    """Windows: attività dell'Utilità di pianificazione che avvia il daemon (senza finestra) all'accesso."""
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    pyw = pyw if pyw.exists() else exe
    tr = f'"{pyw}" -X utf8 -m taccuino.cli daemon'
    r = subprocess.run(["schtasks", "/Create", "/SC", "ONLOGON", "/TN", WIN_TASK, "/TR", tr, "/RL", "LIMITED", "/F"],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit("schtasks: " + (r.stderr or r.stdout).strip())
    subprocess.run(["schtasks", "/Run", "/TN", WIN_TASK], capture_output=True)
    print(f"Attività \"{WIN_TASK}\" creata: parte a ogni accesso.\nLog: {DATA_DIR / 'daemon.log'}")


def _state():
    if not STATE_PATH.exists():
        return None
    st = json.loads(STATE_PATH.read_text())
    return st if time.time() - st.get("updated", 0) < 30 else None


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
    ap.add_argument("--profile", help="xe | npu | cpu")
    sub = ap.add_subparsers(dest="cmd")
    for c in ("daemon", "install-service", "uninstall-service", "quit", "status", "stop", "list", "devices",
              "download"):
        sub.add_parser(c)
    sub.add_parser("console").add_argument("--plain", action="store_true", help="console semplice, senza TUI")
    sub.add_parser("riassumi").add_argument("minutes", nargs="?", default="5")
    sub.add_parser("chiedi").add_argument("question", nargs="+")
    sub.add_parser("sottotitoli")
    sub.add_parser("live-worker").add_argument("dir")
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

    elif a.cmd == "install-service" and IS_WIN:
        _win_install_service()

    elif a.cmd == "uninstall-service":
        if _state():
            _send("quit")
        if IS_WIN:
            subprocess.run(["schtasks", "/End", "/TN", WIN_TASK], capture_output=True)
            r = subprocess.run(["schtasks", "/Delete", "/TN", WIN_TASK, "/F"], capture_output=True, text=True)
            print("Attività rimossa" if r.returncode == 0 else (r.stderr or r.stdout).strip())
        else:
            subprocess.run(["systemctl", "--user", "disable", "--now", "taccuino.service"])
            (Path.home() / ".config/systemd/user/taccuino.service").unlink(missing_ok=True)
            subprocess.run(["systemctl", "--user", "daemon-reload"])
            print("Servizio rimosso")

    elif a.cmd == "quit":
        _send("quit"); print("daemon fermato")

    elif a.cmd == "install-service":
        exe = shutil.which("taccuino") or str(Path(sys.argv[0]).resolve())
        unit = Path.home() / ".config/systemd/user/taccuino.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        path = ":".join(dict.fromkeys([str(Path(exe).parent), str(Path(s.claude_bin).parent),
                                       str(Path.home() / ".local/bin"), "/usr/local/bin", "/usr/bin", "/bin"]))
        unit.write_text(SERVICE.format(exe=exe, path=path))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "taccuino.service"], check=True)
        print(f"Installato {unit}\nLog: journalctl --user -u taccuino -f")

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
        print(f"profilo: {s.profile} (Whisper @ {s.asr[1]}), output: {s.output_dir}")

    elif a.cmd == "start":
        _send(f"start {a.title}".strip()); print("● registrazione avviata")
    elif a.cmd == "stop":
        _send("stop"); print("■ registrazione fermata, elaborazione in coda")

    elif a.cmd == "note":
        st = _state()
        if not st or not st["recording"]:
            sys.exit("Nessuna riunione in corso.")
        el = int(time.time() - st["since"])
        with open(Path(st["meeting_dir"]) / "notes.txt", "a") as f:
            f.write(f"- [{el // 60:02d}:{el % 60:02d}] {' '.join(a.text)}\n")
        print("appunto salvato")

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
        if IS_WIN:
            from .winaudio import list_devices
            print("\n" + list_devices())
        else:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-sources", "pulse"], capture_output=True, text=True)
            print("\nSorgenti audio:\n" + (out.stdout or out.stderr))
        if not shutil.which("ffmpeg"):
            print("\n⚠ ffmpeg non trovato nel PATH" + (" (winget install Gyan.FFmpeg)" if IS_WIN else ""))

    elif a.cmd == "download":
        from .engines import ensure_diarization_models, ensure_whisper
        print(ensure_whisper(s.asr[0])); print(*ensure_diarization_models(), sep="\n")

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
