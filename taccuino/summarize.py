"""Note della riunione scritte da Claude Code in modalità headless (`claude -p`)."""
from __future__ import annotations

import subprocess
from datetime import datetime

from .config import Settings

PROMPT = """Sei l'assistente che scrive le note di una riunione a partire dal transcript qui sotto.
Il transcript è stato generato automaticamente (Whisper + diarization), quindi può contenere errori
di trascrizione o speaker scambiati: correggi mentalmente gli errori evidenti ma NON inventare nulla.

Etichette: "Io" è l'utente; "In sala N" sono persone nella stessa stanza dell'utente;
"Speaker N" sono partecipanti remoti della call. Se dal contesto si capisce il nome reale di uno speaker
(es. qualcuno lo chiama per nome), usalo nella forma "Nome (Speaker N)".

Data: {date} · Durata: {duration} min · App: {apps}
{extra}
APPUNTI PRESI DALL'UTENTE DURANTE LA RIUNIONE (priorità alta, rispetta i suoi punti):
{notes}

Rispondi SOLO con questo formato, nella lingua prevalente della riunione:

TITOLO: <titolo breve, max 8 parole>

## Sintesi
<3-5 frasi>

## Partecipanti
- <etichetta / nome> — <ruolo o cosa ha portato, se deducibile>

## Punti discussi
- …

## Decisioni
- …

## Azioni
- [ ] <chi> — <cosa> — <entro quando, se detto>

## Domande aperte
- …

Ometti le sezioni vuote. Non usare strumenti, non leggere file: hai già tutto il necessario.

TRANSCRIPT:
"""


def summarize(s: Settings, transcript: str, user_notes: str, started: datetime, duration: float,
              apps: list[str]) -> tuple[str, str]:
    prompt = PROMPT.format(
        date=started.strftime("%d/%m/%Y %H:%M"), duration=round(duration / 60),
        apps=", ".join(apps) or "-", notes=user_notes or "(nessuno)",
        extra=(s.claude_extra_prompt + "\n") if s.claude_extra_prompt else "")
    cmd = [s.claude_bin, "-p", "--output-format", "text", "--no-session-persistence",
           "--disallowedTools", "Bash", "Edit", "Write", "WebFetch", "WebSearch", "NotebookEdit"]
    if s.claude_model:
        cmd += ["--model", s.claude_model]
    res = subprocess.run(cmd, input=prompt + transcript, capture_output=True, text=True, timeout=900,
                         cwd="/tmp")
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError((res.stderr or res.stdout or f"exit {res.returncode}").strip()[-400:])
    out = res.stdout.strip()
    title = ""
    first, _, rest = out.partition("\n")
    if first.upper().startswith("TITOLO:") or first.upper().startswith("TITLE:"):
        title = first.split(":", 1)[1].strip().strip('"*# ')
        out = rest.strip()
    return title, out
