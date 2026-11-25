"""Note della riunione scritte da Claude Code in modalità headless (`claude -p`)."""
from __future__ import annotations

import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from .compat import NO_WINDOW, command
from .config import Settings

# Taccuino usa sempre e solo Claude Haiku: veloce, economico, basta per note e riassunti. Non configurabile.
CLAUDE_MODEL = "haiku"

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
CARTELLA: <sottocartella dove archiviare la nota>

Per CARTELLA: scegli una delle cartelle esistenti qui sotto se la riunione ci rientra (stesso progetto,
cliente, team o argomento); altrimenti inventane una nuova, breve e generica abbastanza da raccogliere
riunioni future simili (es. "Lavoro/Progetto Alfa", "Clienti/Rossi", "Personale"). Al massimo 2 livelli,
separati da "/". Cartelle esistenti: {folders}

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


ASK_PROMPT = """Stai aiutando l'utente durante (o subito dopo) una riunione. Qui sotto c'è il transcript
automatico {scope}: può contenere errori di trascrizione, correggi mentalmente quelli evidenti ma NON inventare.
"Io" è l'utente, "Call" sono i partecipanti remoti (le altre etichette sono speaker distinti).
{extra}
Rispondi in modo breve e diretto, nella lingua della riunione, in testo semplice adatto a un terminale
(elenchi con "-", niente tabelle). Non usare strumenti, non leggere file: hai già tutto il necessario.

RICHIESTA: {question}

TRANSCRIPT:
"""

RECAP = ("Riassumi in 3-6 punti cosa si è detto; poi, solo se ci sono, decisioni prese e cose da fare "
         "(chi/cosa). Se il transcript è quasi vuoto o incomprensibile, dillo in una riga.")


def run_claude(s: Settings, prompt: str, timeout: int = 900, allow_tools: list[str] | None = None) -> str:
    """`claude -p` con Haiku. allow_tools: strumenti (es. di un connettore MCP) pre-approvati per questa chiamata."""
    cmd = command(s.claude_bin, "-p", "--output-format", "text", "--no-session-persistence",
                  "--disallowedTools", "Bash", "Edit", "Write", "WebFetch", "WebSearch", "NotebookEdit")
    if allow_tools:
        cmd += ["--allowedTools", *allow_tools]
    cmd += ["--model", CLAUDE_MODEL]
    res = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                         timeout=timeout, cwd=tempfile.gettempdir(), **NO_WINDOW)
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError((res.stderr or res.stdout or f"exit {res.returncode}").strip()[-400:])
    return res.stdout.strip()


def _extra(s: Settings) -> str:
    from .vocab import prompt_text
    out = (s.claude_extra_prompt + "\n") if s.claude_extra_prompt else ""
    if (v := prompt_text(s)):
        out += f"Vocabolario dell'utente (grafia corretta di nomi e termini ricorrenti): {v}\n"
    return out


def ask(s: Settings, transcript: str, question: str, scope: str) -> str:
    extra = _extra(s)
    return run_claude(s, ASK_PROMPT.format(scope=scope, question=question, extra=extra) + transcript, timeout=300)


def existing_folders(root: Path) -> list[str]:
    """Sottocartelle (fino a 2 livelli) dell'archivio note."""
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.glob("*")) + sorted(root.glob("*/*")):
        if d.is_dir() and not any(part.startswith(".") for part in d.relative_to(root).parts):
            out.append(d.relative_to(root).as_posix())
    return out


def clean_folder(folder: str) -> str:
    """Rende sicuro il percorso proposto da Claude: niente "..", assoluti o caratteri strani, max 2 livelli."""
    parts = []
    for p in folder.replace("\\", "/").split("/"):
        p = re.sub(r"[^\w\- àèéìòùÀÈÉÌÒÙ&]+", "", p).strip(" .-")[:40]
        if p:
            parts.append(p)
    return "/".join(parts[:2])


def summarize(s: Settings, transcript: str, user_notes: str, started: datetime, duration: float,
              apps: list[str]) -> tuple[str, str, str]:
    """Restituisce (titolo, cartella, note)."""
    folders = existing_folders(Path(s.output_dir))
    prompt = PROMPT.format(
        date=started.strftime("%d/%m/%Y %H:%M"), duration=round(duration / 60),
        apps=", ".join(apps) or "-", notes=user_notes or "(nessuno)",
        folders=", ".join(f'"{f}"' for f in folders) or "(nessuna, è la prima)",
        extra=_extra(s))
    out = run_claude(s, prompt + transcript)
    title = folder = ""
    lines = out.split("\n")
    while lines and (not lines[0].strip() or re.match(r"^\W*(TITOLO|TITLE|CARTELLA|FOLDER)\W*:", lines[0], re.I)):
        head = lines.pop(0)
        if ":" in head:
            key, val = head.split(":", 1)
            val = val.strip().strip('"*#` ')
            if re.search(r"TITOLO|TITLE", key, re.I):
                title = val
            else:
                folder = clean_folder(val)
    return title, folder, "\n".join(lines).strip()
