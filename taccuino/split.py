"""Divide una registrazione che contiene più riunioni di seguito (due call consecutive, microfono mai rilasciato).

Il silenzio non basta: tra due call di fila spesso non c'è nessuna pausa (nella registrazione reale da cui è nato
questo modulo la pausa più lunga era di 25 s, dentro la stessa call). Quindi:

  - a fine registrazione (`auto_split`) Haiku legge il transcript e, solo se è sicuro, indica dove comincia ogni
    riunione successiva; la nota riporta il motivo del taglio;
  - a mano: `taccuino split DIR MM:SS` taglia dove dici tu; `taccuino split DIR` fa decidere a Haiku;
  - durante la call: `/split` chiude la registrazione e ne apre subito un'altra.

Il taglio crea una nuova cartella riunione (audio, righe live e correzioni dal punto di taglio, tempi ricalcolati)
e accorcia l'originale. L'audio originale non va perso: il file `audio.flac` è solo diviso in due.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .compat import NO_WINDOW
from .config import MEETINGS_DIR, Settings

DETECT_PROMPT = """Qui sotto c'è il transcript automatico, a blocchi di 1 minuto, di UNA registrazione continua.
A volte contiene più riunioni diverse di seguito (es. due call consecutive: finisce una con saluti e chiusura,
ne comincia un'altra con persone, cliente, progetto o argomento diversi).

Indica una nuova riunione SOLO se sei sicuro. Un cambio di argomento dentro la stessa riunione NON è una nuova riunione.
Rispondi con una riga per ogni riunione successiva alla prima, in questo formato:
INIZIO: <numero del blocco in cui comincia> | <motivo breve, con le prove dal testo>
Se è una sola riunione rispondi solo: NESSUNA
Non usare strumenti.

TRANSCRIPT:
{blocks}"""

REFINE_PROMPT = """In questo estratto una riunione finisce e ne comincia un'altra (o sta per cominciare).
Indica il numero della PRIMA riga che appartiene alla nuova riunione. Saluti, ringraziamenti e battute di
chiusura ("ciao ciao", "grazie, buona giornata", "tutto bene?") appartengono alla riunione che finisce, non
a quella nuova: la nuova comincia con la prima riga del suo contenuto o della sua apertura.
Rispondi solo: RIGA: <numero>
Non usare strumenti.

{lines}"""


def parse_ts(text: str) -> float:
    """"1:02:03", "62:03" o "3723" → secondi."""
    parts = [float(p) for p in text.strip().split(":")]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def _ts(sec: float) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _blocks(lines, size: int = 60) -> list[tuple[int, str]]:
    """(numero blocco, testo) a blocchi di `size` secondi; di ogni blocco inizio e fine, dove stanno saluti e aperture."""
    by: dict[int, list[str]] = {}
    for l in lines:
        by.setdefault(int(l.start // size), []).append(f"{l.speaker}: {l.text}")
    out = []
    for b in sorted(by):
        text = " ".join(by[b])
        out.append((b, text if len(text) <= 330 else f"{text[:160]} … {text[-160:]}"))
    return out


def find_splits(s: Settings, lines) -> list[tuple[float, str]]:
    """[(secondo di inizio, motivo)] delle riunioni successive alla prima; vuoto se è una sola riunione."""
    from .summarize import run_claude
    if not lines:
        return []
    blocks = _blocks(lines)
    out = run_claude(s, DETECT_PROMPT.format(blocks="\n".join(f"[{b}] {t}" for b, t in blocks)), timeout=300)
    found = []
    for m in re.finditer(r"^\W*INIZIO\W*:\s*(\d+)\s*\|?\s*(.*)$", out, re.M | re.I):
        block, why = int(m.group(1)), m.group(2).strip()
        near = [l for l in lines if (block - 1) * 60 <= l.start < (block + 2) * 60]
        if not near:
            continue
        try:  # secondo passaggio: la riga esatta in cui comincia la nuova riunione
            r = run_claude(s, REFINE_PROMPT.format(lines="\n".join(
                f"{i}| [{_ts(l.start)}] {l.speaker}: {l.text[:200]}" for i, l in enumerate(near))), timeout=120)
            k = re.search(r"RIGA\W*:\s*(\d+)", r, re.I)
            start = near[int(k.group(1))].start if k and int(k.group(1)) < len(near) else block * 60.0
        except Exception:  # noqa: BLE001
            start = block * 60.0
        found.append((start, why))
    return sorted(found)


def _read_lines(mdir: Path) -> list[dict]:
    p = mdir / "live.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def split_meeting(mdir: Path, at: float, reason: str = "") -> Path:
    """Taglia la cartella riunione al secondo `at` (portato all'inizio della riga più vicina): le righe da lì in poi
    passano in una nuova cartella; restituisce la nuova cartella."""
    mdir = Path(mdir)
    meta = json.loads((mdir / "meta.json").read_text(encoding="utf-8"))
    rows = _read_lines(mdir)
    if not rows:
        raise ValueError("nessuna trascrizione live da dividere")
    idx = next((i for i, r in enumerate(rows) if r["start"] >= at - 1.0), None)
    if idx is None or idx == 0:
        raise ValueError(f"punto di taglio {_ts(at)} fuori dalla registrazione (0 – {_ts(rows[-1]['end'])})")
    cut = rows[idx]["start"]
    clean_p = mdir / "live.clean.json"
    clean = {int(k): v for k, v in json.loads(clean_p.read_text(encoding="utf-8")).items()} if clean_p.exists() else {}

    started = datetime.fromtimestamp(meta["started"] + cut)
    new = MEETINGS_DIR / f"{started:%Y-%m-%d_%H%M%S}"
    n = 1
    while new.exists():
        new, n = MEETINGS_DIR / f"{started:%Y-%m-%d_%H%M%S}-{n}", n + 1
    new.mkdir(parents=True)

    shift = lambda r: {**r, "start": round(r["start"] - cut, 1), "end": round(r["end"] - cut, 1)}  # noqa: E731
    (new / "live.jsonl").write_text("".join(json.dumps(shift(r), ensure_ascii=False) + "\n" for r in rows[idx:]),
                                    encoding="utf-8")
    (new / "live.clean.json").write_text(json.dumps({str(i - idx): clean[i] for i in clean if i >= idx},
                                                    ensure_ascii=False), encoding="utf-8")
    (mdir / "live.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows[:idx]),
                                     encoding="utf-8")
    clean_p.write_text(json.dumps({str(i): clean[i] for i in clean if i < idx}, ensure_ascii=False), encoding="utf-8")

    audio = mdir / "audio.flac"
    if audio.exists():
        tmp = mdir / "audio.part1.flac"
        ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(audio)]
        subprocess.run([*ff, "-t", f"{cut:.2f}", "-c:a", "flac", str(tmp)], check=True, **NO_WINDOW)
        subprocess.run([*ff, "-ss", f"{cut:.2f}", "-c:a", "flac", str(new / "audio.flac")], check=True, **NO_WINDOW)
        os.replace(tmp, audio)

    # gli appunti live coprivano l'intera registrazione: le note finali si rigenerano da zero per ciascuna parte
    for d in (mdir, new):
        for f in ("live_notes.md", "live.draft", "live.cursor", "transcript.json"):
            (d / f).unlink(missing_ok=True)
    meta.update(split_checked=True, split_at=cut, split_reason=reason)
    (mdir / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    meta2 = {k: v for k, v in meta.items() if k not in ("output", "folder", "folder_reason", "live_title", "processed",
                                                         "duration", "split_at")}
    meta2.update(started=meta["started"] + cut, title="", split_from=mdir.name, split_reason=reason,
                 split_checked=True)
    (new / "meta.json").write_text(json.dumps(meta2, indent=1), encoding="utf-8")
    return new


def maybe_split(mdir: Path, s: Settings, log=print) -> list[Path]:
    """A fine registrazione: se è lunga e Haiku vede più riunioni, le divide. Restituisce le nuove cartelle."""
    from .live import LiveTranscript
    meta_p = mdir / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
    if not s.auto_split or meta.get("split_checked") or not (mdir / "live.jsonl").exists():
        return []
    lines = LiveTranscript(mdir, s).clean_lines()
    if not lines or lines[-1].end < s.split_min_minutes * 60:
        return []
    meta["split_checked"] = True  # una sola verifica per registrazione, anche se Haiku non risponde
    meta_p.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    log("Claude controlla se la registrazione contiene più riunioni …")
    try:
        cuts = find_splits(s, lines)
    except Exception as e:  # noqa: BLE001
        log(f"controllo divisione: {e}")
        return []
    made: list[Path] = []
    for at, why in reversed(cuts):  # dal taglio più avanzato al primo, così i tempi dei precedenti non cambiano
        try:
            made.insert(0, split_meeting(mdir, at, why))
            log(f"divisa a {_ts(at)}: {why}")
        except ValueError as e:
            log(f"taglio a {_ts(at)} ignorato: {e}")
    return made
