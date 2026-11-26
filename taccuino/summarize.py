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


def run_claude(s: Settings, prompt: str, timeout: int = 900, allow_tools: list[str] | None = None,
               read_dir: Path | None = None) -> str:
    """`claude -p` con Haiku. allow_tools: strumenti (es. di un connettore MCP) pre-approvati per questa chiamata.
    read_dir: Claude può solo leggere (Read/Glob/Grep) dentro questa cartella, che diventa la sua cartella di lavoro."""
    cmd = command(s.claude_bin, "-p", "--output-format", "text", "--no-session-persistence",
                  "--disallowedTools", "Bash", "Edit", "Write", "WebFetch", "WebSearch", "NotebookEdit")
    if read_dir:
        cmd += ["--tools", "Read", "Glob", "Grep", "--allowedTools", "Read", "Glob", "Grep"]
    elif allow_tools:
        cmd += ["--allowedTools", *allow_tools]
    cmd += ["--model", CLAUDE_MODEL]
    res = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                         timeout=timeout, cwd=str(read_dir) if read_dir else tempfile.gettempdir(), **NO_WINDOW)
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError((res.stderr or res.stdout or f"exit {res.returncode}").strip()[-400:])
    return res.stdout.strip()


def ask(s: Settings, transcript: str, question: str, scope: str) -> str:
    extra = (s.claude_extra_prompt + "\n") if s.claude_extra_prompt else ""
    return run_claude(s, ASK_PROMPT.format(scope=scope, question=question, extra=extra) + transcript, timeout=300)


def existing_folders(root: Path, exclude=()) -> list[str]:
    """Sottocartelle (fino a 2 livelli) dell'archivio note, senza quelle nascoste o in `exclude` (sottoalbero incluso)."""
    if not root.is_dir():
        return []
    ex = {e.lower() for e in exclude}
    out = []
    for d in sorted(root.glob("*")) + sorted(root.glob("*/*")):
        parts = d.relative_to(root).parts
        if d.is_dir() and not any(p.startswith(".") for p in parts) and not ex & {p.lower() for p in parts}:
            out.append("/".join(parts))
    return out


def clean_folder(folder: str) -> str:
    """Rende sicuro il percorso proposto da Claude: niente "..", assoluti o caratteri strani, max 2 livelli."""
    parts = []
    for p in folder.replace("\\", "/").split("/"):
        p = re.sub(r"[^\w\- àèéìòùÀÈÉÌÒÙ&]+", "", p).strip(" .-")[:40]
        if p:
            parts.append(p)
    return "/".join(parts[:2])


FOLDER_PROMPT = """Devi scegliere la sottocartella dell'archivio (la tua cartella di lavoro) dove archiviare la nota
di una riunione. Hai solo strumenti di lettura (Glob, Grep, Read): guarda cosa contengono le cartelle candidate
(nomi dei file, un indice o README, le note più recenti) e apri al massimo 3-4 documenti, solo se servono a capire a
quale progetto, cliente o tema appartiene la riunione (nomi, sigle e argomenti emersi nel transcript). Non leggere
altro e non scrivere nulla.
Scegli una cartella esistente se la riunione ci rientra; solo se nessuna va bene, proponine una nuova, breve e
generica. Al massimo 2 livelli, separati da "/". Cartelle candidate: {folders}

Rispondi SOLO con queste due righe:
CARTELLA: <percorso>
MOTIVO: <1-2 frasi: cosa hai trovato (cartelle o documenti consultati) e perché la riunione ci rientra>

APPUNTI DELL'UTENTE:
{user}

TRANSCRIPT (automatico, può avere errori):
{transcript}
"""


def choose_folder(s: Settings, transcript: str, user_notes: str = "") -> tuple[str, str]:
    """Haiku guarda cosa c'è nell'archivio (sola lettura) e sceglie la sottocartella. Restituisce (cartella, motivo)."""
    root = Path(s.output_dir)
    root.mkdir(parents=True, exist_ok=True)
    folders = ", ".join(f'"{f}"' for f in existing_folders(root, s.exclude_folders)) or "(nessuna, è la prima)"
    out = run_claude(s, FOLDER_PROMPT.format(folders=folders, user=user_notes or "(nessuno)",
                                             transcript=transcript[-6000:]), timeout=180, read_dir=root)
    m = re.search(r"^\W*CARTELLA\W*:\s*(.+)$", out, re.I | re.M)
    if not m:
        raise RuntimeError("risposta senza CARTELLA: " + out[:120])
    why = re.search(r"^\W*MOTIVO\W*:\s*(.+(?:\n(?!\W*CARTELLA).+)*)", out, re.I | re.M)
    return clean_folder(m.group(1).strip().strip('"*`')), (why.group(1).strip().replace("\n", " ") if why else "")


def summarize(s: Settings, transcript: str, user_notes: str, started: datetime, duration: float,
              apps: list[str], folder: str = "") -> tuple[str, str, str]:
    """Restituisce (titolo, cartella, note). `folder`: cartella già scelta, da cui prendere il vocabolario."""
    from . import vocab
    folders = existing_folders(Path(s.output_dir), s.exclude_folders)
    glossary = vocab.load(s, folder)[0]
    extra = "\n".join(x for x in (s.claude_extra_prompt,
                                  f"Vocabolario del progetto (scrivi nomi e termini esattamente così, "
                                  f"anche nei partecipanti):\n{glossary}" if glossary else "") if x)
    prompt = PROMPT.format(
        date=started.strftime("%d/%m/%Y %H:%M"), duration=round(duration / 60),
        apps=", ".join(apps) or "-", notes=user_notes or "(nessuno)",
        folders=", ".join(f'"{f}"' for f in folders) or "(nessuna, è la prima)",
        extra=extra + "\n" if extra else "")
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
