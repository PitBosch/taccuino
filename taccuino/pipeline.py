"""Registrazione → diarization → trascrizione → Markdown → note di Claude Code."""
from __future__ import annotations

import difflib
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from .audio import SR, load_channels, speech_ratio
from .config import Settings
from .engines import ASR, Diarizer, Turn
from .summarize import summarize

MAX_TURN = 25.0  # Whisper lavora su finestre da 30 s


@dataclass
class Line:
    start: float
    end: float
    speaker: str
    text: str


def _merge(turns: list[Turn], gap=1.0) -> list[Turn]:
    out: list[Turn] = []
    for t in turns:
        if out and out[-1].speaker == t.speaker and t.start - out[-1].end <= gap and t.end - out[-1].start <= MAX_TURN:
            out[-1].end = t.end
        else:
            out.append(Turn(t.start, t.end, t.speaker))
    # spezza i turni troppo lunghi
    res = []
    for t in out:
        s = t.start
        while t.end - s > MAX_TURN + 3:
            res.append(Turn(s, s + MAX_TURN, t.speaker))
            s += MAX_TURN
        res.append(Turn(s, t.end, t.speaker))
    return res


def _label_map(turns: list[Turn], channel: str) -> dict[int, str]:
    """Microfono: lo speaker con più parlato sei tu ("Io"), gli altri sono in stanza.
    Sistema: "Speaker 1, 2, …" in ordine di prima apparizione."""
    if channel == "mic":
        tot: dict[int, float] = {}
        for t in turns:
            tot[t.speaker] = tot.get(t.speaker, 0) + t.end - t.start
        order = sorted(tot, key=tot.get, reverse=True)
        return {spk: ("Io" if i == 0 else f"In sala {i}") for i, spk in enumerate(order)}
    seen: dict[int, str] = {}
    for t in turns:
        seen.setdefault(t.speaker, f"Speaker {len(seen) + 1}")
    return seen


def _dedupe_echo(mic: list[Line], sys: list[Line]) -> list[Line]:
    """Con gli altoparlanti il microfono riprende anche la call: scarta righe "doppione"."""
    keep = []
    for m in mic:
        dup = any(s.start < m.end + 1 and m.start < s.end + 1 and
                  difflib.SequenceMatcher(None, m.text.lower(), s.text.lower()).ratio() > 0.55
                  for s in sys)
        if not dup:
            keep.append(m)
    return keep


def transcribe_recording(audio_path: Path, s: Settings, log=print) -> tuple[list[Line], float]:
    x = load_channels(audio_path)
    duration = len(x) / SR
    channels = {"mic": x[:, 0]} if x.shape[1] == 1 else {"mic": x[:, 0], "sys": x[:, 1]}
    diar = Diarizer(s.num_speakers, s.cluster_threshold)
    plans: dict[str, list[tuple[Turn, str]]] = {}
    for name, ch in channels.items():
        if speech_ratio(ch, 0.006) < 0.01:
            log(f"canale {name}: silenzio, salto")
            continue
        t = time.time()
        turns = _merge(diar(ch))
        labels = _label_map(turns, name)
        log(f"canale {name}: {len(set(labels.values()))} speaker, {len(turns)} turni ({time.time() - t:.0f}s)")
        plans[name] = [(tr, labels[tr.speaker]) for tr in turns]
    del diar
    if not plans:
        return [], duration

    asr = ASR(*s.asr, language=s.language, vocabulary=s.vocabulary)
    lines: dict[str, list[Line]] = {}
    t = time.time()
    for name, plan in plans.items():
        ch = channels[name]
        lines[name] = []
        for tr, label in plan:
            if tr.end - tr.start < 0.6:
                continue
            seg = ch[int(max(0, tr.start - 0.2) * SR): int((tr.end + 0.2) * SR)]
            text = asr.transcribe(seg)
            if text:
                lines[name].append(Line(round(tr.start, 1), round(tr.end, 1), label, text))
    log(f"trascrizione: {time.time() - t:.0f}s per {duration:.0f}s di audio")
    del asr
    mic = lines.get("mic", [])
    if "sys" in lines:
        mic = _dedupe_echo(mic, lines["sys"])
    out = sorted(mic + lines.get("sys", []), key=lambda l: l.start)
    return out, duration


def fmt_ts(sec: float) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def transcript_text(lines: list[Line]) -> str:
    out, prev = [], None
    for l in lines:
        if l.speaker == prev and out:
            out[-1] += " " + l.text
        else:
            out.append(f"[{fmt_ts(l.start)}] **{l.speaker}:** {l.text}")
        prev = l.speaker
    return "\n\n".join(out)


def _slug(t: str) -> str:
    return re.sub(r"[^\w\- àèéìòù]+", "", t).strip()[:60]


def process_meeting(mdir: Path, s: Settings, log=print) -> Path:
    """Elabora una cartella riunione (audio.flac, meta.json, notes.txt) e scrive il Markdown."""
    meta = json.loads((mdir / "meta.json").read_text()) if (mdir / "meta.json").exists() else {}
    audio = mdir / "audio.flac"
    lines, duration = transcribe_recording(audio, s, log)
    (mdir / "transcript.json").write_text(json.dumps([asdict(l) for l in lines], ensure_ascii=False, indent=1))
    started = datetime.fromtimestamp(meta.get("started", audio.stat().st_mtime))
    user_notes = (mdir / "notes.txt").read_text().strip() if (mdir / "notes.txt").exists() else ""
    transcript = transcript_text(lines)

    title, notes, folder = meta.get("title") or "", "", meta.get("folder", "")
    if s.summarize and lines:
        log("chiedo le note a Claude Code (Haiku) …")
        try:
            t_title, t_folder, notes = summarize(s, transcript, user_notes, started, duration, meta.get("apps", []))
            title = title or t_title
            folder = folder or t_folder
        except Exception as e:  # noqa: BLE001
            log(f"Claude Code non disponibile: {e}")
            notes = f"> Note non generate: {e}\n> Rigenera con `taccuino summarize \"{mdir}\"`."
    title = title or "Riunione"

    md = [f"# {title}", "",
          f"*{started:%d/%m/%Y %H:%M} · {fmt_ts(duration)} · "
          f"{', '.join(sorted({l.speaker for l in lines})) or 'nessun parlato'}*", ""]
    if user_notes:
        md += ["## I miei appunti", "", user_notes, ""]
    if notes:
        md += [notes.strip(), ""]
    live_notes = (mdir / "live_notes.md").read_text().strip() if (mdir / "live_notes.md").exists() else ""
    if live_notes:
        md += ["## Appunti di Claude durante la riunione", "", live_notes, ""]
    md += ["---", "", "## Transcript", "", transcript or "_(nessun parlato rilevato)_", ""]

    outdir = Path(s.output_dir) / folder  # sottocartella scelta da Claude (vuota = radice)
    outdir.mkdir(parents=True, exist_ok=True)
    old = meta.get("output")
    name = f"{started:%Y-%m-%d %H.%M} - {_slug(title)}.md"
    # se il live worker ha già creato la nota provvisoria, resta nella sua cartella ma prende il titolo finale
    out = (Path(old).parent / name) if old and Path(old).exists() else outdir / name
    out.write_text("\n".join(md))
    if old and Path(old).exists() and Path(old) != out and "in corso…" in Path(old).read_text()[:300]:
        Path(old).unlink()
    meta.update(title=title, folder=folder, output=str(out), processed=time.time(), duration=duration)
    (mdir / "meta.json").write_text(json.dumps(meta, indent=1))
    if s.keep_audio_days == 0:
        audio.unlink(missing_ok=True)
    log(f"scritto {out}")
    return out


def resummarize(mdir: Path, s: Settings, log=print) -> Path:
    """Rigenera solo le note di Claude partendo da transcript.json (senza ritrascrivere)."""
    meta = json.loads((mdir / "meta.json").read_text())
    lines = [Line(**d) for d in json.loads((mdir / "transcript.json").read_text())]
    transcript = transcript_text(lines)
    user_notes = (mdir / "notes.txt").read_text().strip() if (mdir / "notes.txt").exists() else ""
    started = datetime.fromtimestamp(meta["started"])
    _, _, notes = summarize(s, transcript, user_notes, started, meta.get("duration", 0), meta.get("apps", []))
    out = Path(meta["output"])
    body = out.read_text()
    head, _, tail = body.partition("\n---\n")
    head_lines = head.split("\n")
    keep = head_lines[:3]
    if user_notes:
        keep += ["", "## I miei appunti", "", user_notes]
    out.write_text("\n".join(keep) + "\n\n" + notes.strip() + "\n\n---\n" + tail)
    log(f"aggiornato {out}")
    return out


def cleanup_audio(meetings_dir: Path, days: int):
    if days <= 0:
        return
    limit = time.time() - days * 86400
    for f in meetings_dir.glob("*/audio.flac"):
        if f.stat().st_mtime < limit and (f.parent / "transcript.json").exists():
            f.unlink()
