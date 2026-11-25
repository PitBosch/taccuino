"""Processo lanciato dal daemon per la durata di una call: `taccuino live-worker DIR`.

- trascrive in streaming (bozza + frasi fissate) e fa correggere a Haiku nelle pause, così console e
  sottotitoli si limitano a leggere i file e si aprono all'istante;
- ogni `live_notes_minutes` Haiku legge cosa si è detto e aggiunge APPUNTI mirati alla nota della riunione,
  già nella sottocartella giusta di ~/Documents/Taccuino (la sceglie Claude alla prima passata).
A fine call il daemon lo ferma e la pipeline completa (diarization + note finali) riscrive la stessa nota.
"""
from __future__ import annotations

import json
import re
import signal
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import load_settings
from .live import Cleaner, LiveTranscript
from .pipeline import fmt_ts

NOTES_PROMPT = """Stai seguendo in diretta una riunione dell'utente ("Io"; "Call" = partecipanti remoti).
Ogni pochi minuti ricevi il transcript nuovo (automatico, già ripulito, può avere errori) e prendi appunti.

Scrivi SOLO appunti nuovi, mirati e precisi, che all'utente servirà ritrovare: decisioni, impegni e cose da fare
(chi/cosa/entro quando), numeri, date, nomi, problemi emersi, domande rimaste aperte, cambi di argomento importanti.
Niente frasi generiche ("si è discusso di…"), niente ripetizioni di appunti già presi, niente invenzioni.
Non commentare la qualità della trascrizione e non scrivere che qualcosa "non è chiaro": se un punto è troppo
confuso per essere utile, semplicemente non annotarlo. Nelle azioni ometti "quando" se non è stato detto.
Se nel transcript nuovo non c'è nulla che meriti un appunto, rispondi solo: NIENTE

{folder_rule}
Rispondi in questo formato (lingua della riunione):
TITOLO: <titolo provvisorio, max 8 parole>
CARTELLA: <sottocartella>
- <appunto>
- [ ] <azione: chi — cosa[ — entro quando]>

Non usare strumenti.

APPUNTI GIÀ PRESI:
{prev}

APPUNTI DELL'UTENTE:
{user}

TRANSCRIPT NUOVO ({span}):
{new}
"""

FOLDER_FIRST = """Scegli anche CARTELLA, la sottocartella dove archiviare la nota: una di quelle esistenti se la
riunione ci rientra (stesso progetto, cliente, team o argomento), altrimenti una nuova, breve e abbastanza generica
da raccogliere riunioni future simili (es. "Lavoro/Progetto Alfa", "Clienti/Rossi"). Max 2 livelli.
Cartelle esistenti: {folders}"""
FOLDER_KEEP = 'Cartella attuale: "{folder}". Ripetila uguale, a meno che sia chiaramente sbagliata.'


class LiveNotes:
    def __init__(self, lt: LiveTranscript):
        self.lt, self.s = lt, lt.s
        self.mdir = lt.mdir
        self.path = self.mdir / "live_notes.md"
        self.upto = 0.0  # fin dove (secondi) il transcript è già stato letto
        self.last = time.time()

    def _meta(self) -> dict:
        try:
            return json.loads((self.mdir / "meta.json").read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def _update_meta(self, **kw):
        m = self._meta()
        m.update(kw)
        (self.mdir / "meta.json").write_text(json.dumps(m, indent=1))

    def due(self) -> bool:
        return self.s.live_notes_minutes > 0 and time.time() - self.last >= self.s.live_notes_minutes * 60

    def run(self, final: bool = False):
        from .summarize import clean_folder, existing_folders, run_claude
        self.last = time.time()
        lines = [l for l in self.lt.clean_lines() if l.end > self.upto]
        words = sum(len(l.text.split()) for l in lines)
        if words < (5 if final else 25):  # troppo poco per valere un appunto
            return
        meta = self._meta()
        folder = meta.get("folder", "")
        root = Path(self.s.output_dir)
        rule = FOLDER_KEEP.format(folder=folder) if folder else FOLDER_FIRST.format(
            folders=", ".join(f'"{f}"' for f in existing_folders(root)) or "(nessuna)")
        prev = self.path.read_text().strip() if self.path.exists() else "(nessuno)"
        user = (self.mdir / "notes.txt").read_text().strip() if (self.mdir / "notes.txt").exists() else "(nessuno)"
        span = f"{fmt_ts(lines[0].start)}–{fmt_ts(lines[-1].end)}"
        new = "\n".join(f"[{fmt_ts(l.start)}] {l.speaker}: {l.text}" for l in lines)
        try:
            out = run_claude(self.s, NOTES_PROMPT.format(folder_rule=rule, prev=prev[-4000:], user=user,
                                                         span=span, new=new), timeout=120)
        except Exception as e:  # noqa: BLE001
            print(f"appunti live: Claude non disponibile ({e})", flush=True)
            return
        self.upto = lines[-1].end
        title, bullets = meta.get("live_title", ""), []
        for row in out.splitlines():
            r = row.strip()
            if m := re.match(r"^\W*TITOLO\W*:\s*(.+)$", r, re.I):
                title = m.group(1).strip().strip('"*# ')
            elif m := re.match(r"^\W*CARTELLA\W*:\s*(.+)$", r, re.I):
                folder = folder or clean_folder(m.group(1).strip().strip('"*`'))
            elif r.startswith(("- ", "* ", "• ")):
                bullets.append("- " + r[2:].strip())
        if bullets:
            with open(self.path, "a") as f:
                f.write(f"\n**{span}**\n" + "\n".join(bullets) + "\n")
        self._update_meta(folder=folder, live_title=title)
        self._write_note(title, folder)
        print(f"appunti live: +{len(bullets)} ({span}) → {folder or '.'}", flush=True)

    def _write_note(self, title: str, folder: str):
        """Nota provvisoria nella cartella scelta; a fine call la pipeline la riscrive completa."""
        from .pipeline import _slug
        meta = self._meta()
        started = datetime.fromtimestamp(meta.get("started", time.time()))
        old = meta.get("output")
        out = Path(old) if old else Path(self.s.output_dir) / folder / \
            f"{started:%Y-%m-%d %H.%M} - {_slug(title or 'Riunione')}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        notes = self.path.read_text().strip() if self.path.exists() else "_(ancora nessun appunto)_"
        out.write_text(f"# {title or 'Riunione'}\n\n*{started:%d/%m/%Y %H:%M} · in corso…*\n\n"
                       f"## Appunti di Claude durante la riunione\n{notes}\n")
        if not old:
            self._update_meta(output=str(out))


def run(mdir: str):
    s = load_settings()
    lt = LiveTranscript(Path(mdir), s)
    notes = LiveNotes(lt)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    cleaning = threading.Event()
    noting = threading.Event()
    last_speech = time.time()

    def bg(ev: threading.Event, fn):
        ev.set()

        def go():
            try:
                fn()
            finally:
                ev.clear()
        threading.Thread(target=go, daemon=True).start()

    lt.beat()
    lt.load()
    print(f"live: trascrizione in streaming di {Path(mdir).name}", flush=True)
    while not stop.is_set():
        lt.beat()
        try:
            _, _, trailing = lt.step()
        except Exception as e:  # noqa: BLE001
            print(f"live: {e}", flush=True)
            stop.wait(3)
            continue
        if trailing < 0.3:
            last_speech = time.time()
        todo = Cleaner(lt).pending()
        if todo and not cleaning.is_set() and (time.time() - last_speech > 1.2 or len(todo) >= 6):
            bg(cleaning, Cleaner(lt).run)
        if notes.due() and not noting.is_set() and not cleaning.is_set():
            bg(noting, notes.run)
        stop.wait(0.2)
    # fine call: ultima correzione e ultimo giro di appunti, poi esci (la pipeline finale fa il resto)
    for ev in (cleaning, noting):
        while ev.is_set():
            time.sleep(0.2)
    try:
        Cleaner(lt).run()
        notes.run(final=True)
    except Exception as e:  # noqa: BLE001
        print(f"live: chiusura: {e}", flush=True)
    (lt.mdir / "live.driver").unlink(missing_ok=True)
