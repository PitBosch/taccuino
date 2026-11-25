"""taccuino — registra, diarizza e trascrive le riunioni in locale (Intel GPU/NPU); le note le scrive Claude Code.

  taccuino daemon                 avvia il servizio sempre attivo (di solito via systemd)
  taccuino install-service        installa e avvia il servizio systemd utente
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
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("daemon", "install-service", "status", "stop", "list", "devices", "download"):
        sub.add_parser(c)
    sub.add_parser("start").add_argument("title", nargs="?", default="")
    sub.add_parser("note").add_argument("text", nargs="+")
    p = sub.add_parser("process"); p.add_argument("path"); p.add_argument("--title", default="")
    p.add_argument("--no-summary", action="store_true")
    sub.add_parser("summarize").add_argument("dir")
    c = sub.add_parser("config"); c.add_argument("key", nargs="?"); c.add_argument("value", nargs="?")
    a = ap.parse_args(argv)
    if a.profile:
        os.environ["TACCUINO_PROFILE"] = a.profile
    s = load_settings()

    if a.cmd == "daemon":
        from .daemon import run
        run()

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
            print("NPU    non trovata (serve Core Ultra + driver intel/linux-npu-driver)")
        out = subprocess.run(["ffmpeg", "-hide_banner", "-sources", "pulse"], capture_output=True, text=True)
        print("\nSorgenti audio:\n" + (out.stdout or out.stderr))

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
