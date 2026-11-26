"""Vocabolario dal vault Obsidian: nomi, progetti, aziende e termini che probabilmente sentirai nelle call.

  taccuino vocabolario [--vault PERCORSO]

1. In locale si estraggono solo i *candidati*: titoli delle note, [[link]] e alias, #tag, alias nel frontmatter,
   nomi propri (parole maiuscole a metà frase), sigle e CamelCase, con il numero di note in cui compaiono.
   Il testo delle note NON lascia il computer.
2. Haiku sceglie dall'elenco i termini utili (persone, progetti, aziende, prodotti, gergo tecnico), nella
   grafia corretta, e scarta le parole comuni.
3. Il risultato va in `vocabulary.txt` (nella cartella dati): lo usano Whisper (i più importanti),
   la correzione di Haiku, il segretario e le note. Il daemon lo rigenera da solo ogni `vocab_refresh_days`.
Funziona uguale su Linux e Windows (vault cercato anche in Documenti e OneDrive).
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, defaultdict
from itertools import zip_longest
from pathlib import Path

from .config import DATA_DIR, Settings

VOCAB_PATH = DATA_DIR / "vocabulary.txt"
META_PATH = DATA_DIR / "vocabulary.json"

_SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", "templates", "Templates"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}[ _-]*")
_WIKI = re.compile(r"\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|([^\]]+))?\]\]")
_TAG = re.compile(r"(?<![\w/#])#([A-Za-zÀ-ÿ][\w/-]{1,40})")
_PROPER = re.compile(r"(?<![.!?:]\s)(?<!^)\b([A-ZÀ-Ý][a-zà-ÿ]+(?:[ -][A-ZÀ-Ý][a-zà-ÿ]+){0,2})\b")
_SPECIAL = re.compile(r"\b([A-Z]{2,6}\d*|[A-Z][a-z]+[A-Z]\w+|[A-Za-z]+-?\d+[A-Za-z]*)\b")
_FRONT = re.compile(r"^---\n(.*?)\n---", re.S)


def find_vault(s: Settings) -> Path | None:
    """Il vault configurato, altrimenti quello con più note tra home, Documenti e OneDrive."""
    if s.vault_path:
        p = Path(s.vault_path).expanduser()
        return p if p.is_dir() else None
    home = Path.home()
    roots = [home, home / "Documents", home / "Documenti", home / "OneDrive", home / "OneDrive" / "Documents",
             home / "OneDrive" / "Documenti", home / "Dropbox", home / "Sync"]
    if os.environ.get("OneDrive"):
        roots.append(Path(os.environ["OneDrive"]))
    found = set()
    for r in roots:
        if not r.is_dir():
            continue
        for depth in ("*/.obsidian", "*/*/.obsidian", ".obsidian"):
            for o in r.glob(depth):
                if o.is_dir() and not any(x in o.parts for x in (".cache", "node_modules", "AppData")):
                    found.add(o.parent)
    if not found:
        return None
    return max(found, key=lambda v: sum(1 for _ in v.rglob("*.md")))


def _notes(vault: Path):
    for p in vault.rglob("*.md"):
        if not any(part in _SKIP_DIRS for part in p.relative_to(vault).parts):
            yield p


def candidates(vault: Path, limit: int = 1200) -> list[tuple[str, int]]:
    """(termine, in quante note compare), ordinati per importanza. Solo estrazione locale."""
    docs: dict[str, set[int]] = defaultdict(set)
    strong: Counter = Counter()  # titoli, link, alias e tag: segnali forti
    for i, p in enumerate(_notes(vault)):
        title = _DATE.sub("", p.stem).strip(" _-")
        if 2 < len(title) <= 50:
            strong[title] += 3
            docs[title].add(i)
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")[:200_000]
        except OSError:
            continue
        if (fm := _FRONT.match(text)):
            for line in fm.group(1).splitlines():
                if re.match(r"^\s*(aliases|alias|tags)\s*:", line) or line.strip().startswith("- "):
                    for v in re.split(r"[,\[\]]", line.split(":", 1)[-1].lstrip("- ")):
                        v = v.strip().strip("'\"#")
                        if 2 < len(v) <= 40:
                            strong[v] += 2
                            docs[v].add(i)
        for target, alias in _WIKI.findall(text):
            for v in (target, alias):
                v = _DATE.sub("", v.strip().split("/")[-1])
                if 2 < len(v) <= 50:
                    strong[v] += 2
                    docs[v].add(i)
        for t in _TAG.findall(text):
            docs[t.split("/")[-1]].add(i)
        for rx in (_PROPER, _SPECIAL):
            for m in rx.findall(text):
                if 2 < len(m) <= 40:
                    docs[m].add(i)
    score = {t: len(d) + strong[t] for t, d in docs.items() if len(d) >= 2 or strong[t]}
    best = sorted(score, key=score.get, reverse=True)[:limit]
    return [(t, len(docs[t])) for t in best]


PROMPT = """Qui sotto ci sono termini estratti automaticamente dalle note Obsidian dell'utente (tra parentesi:
in quante note compaiono). Serve un VOCABOLARIO per migliorare la trascrizione automatica delle sue riunioni.

Tieni solo termini che potrebbero essere PRONUNCIATI in una riunione e che un riconoscitore vocale sbaglierebbe:
nomi di persone, aziende, clienti, progetti, prodotti, luoghi poco comuni, sigle, gergo tecnico, nomi di strumenti.
Scarta parole comuni italiane/inglesi, titoli di note generici, date, frasi, nomi di file, tag organizzativi.
Usa la grafia corretta e canonica (es. "Kubernetes", "PostgreSQL", "Marco Rossi"); unisci i doppioni.
Ordina dal più importante (più frequente/specifico) al meno. Massimo {n} termini.

Rispondi SOLO con i termini, uno per riga, senza numeri né commenti. Non usare strumenti.

TERMINI:
{terms}
"""


def build(s: Settings, vault: Path | None = None, log=print, max_terms: int = 300) -> list[str]:
    from .summarize import run_claude
    vault = vault or find_vault(s)
    if not vault:
        raise SystemExit("Nessun vault Obsidian trovato: `taccuino config vault_path PERCORSO`")
    t = time.time()
    cands = candidates(vault)
    log(f"vault {vault}: {len(cands)} termini candidati ({time.time() - t:.1f}s), li passo a Haiku …")
    terms: list[str] = []
    for i in range(0, len(cands), 600):  # a blocchi, per non mandare elenchi enormi
        chunk = "\n".join(f"{c} ({n})" for c, n in cands[i:i + 600])
        out = run_claude(s, PROMPT.format(n=max_terms if i == 0 else max_terms // 2, terms=chunk), timeout=300)
        for line in out.splitlines():
            line = re.sub(r"^[\s\-*•\d.)]+", "", line).strip()
            if 1 < len(line) <= 50 and line.lower() not in {x.lower() for x in terms}:
                terms.append(line)
    terms = terms[:max_terms]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    VOCAB_PATH.write_text("\n".join(terms) + "\n", encoding="utf-8")
    META_PATH.write_text(json.dumps({"vault": str(vault), "built": time.time(), "terms": len(terms)}))
    log(f"vocabolario: {len(terms)} termini → {VOCAB_PATH}")
    return terms


def terms(s: Settings) -> list[str]:
    """Vocabolario effettivo: quello scritto a mano in config (prima) + quello dal vault."""
    manual = [t.strip() for t in re.split(r"[,\n]", s.vocabulary or "") if t.strip()]
    auto = VOCAB_PATH.read_text(encoding="utf-8").splitlines() if VOCAB_PATH.exists() else []
    seen, out = set(), []
    for t in manual + auto:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


PROJECT_FILE = "Vocabolario.md"  # vocabolario di progetto nella KB: `- Termine — nota`, una voce per riga
WHISPER_CHARS = 450  # il prompt di Whisper è corto (~224 token)


def project_entries(s: Settings, folder: str | None = None) -> list[str]:
    """Voci "Termine — nota" dei `Vocabolario.md` nella cartella scelta e nei suoi genitori; cartella ancora
    ignota (None) → di tutti i progetti della KB. Una voce a testa per file, così nessun progetto resta fuori."""
    root = Path(s.output_dir)
    if folder:
        files = [d / PROJECT_FILE for d in [root / folder, *(root / folder).parents] if d == root or root in d.parents]
    elif folder is None and root.is_dir():
        files = [f for f in root.rglob(PROJECT_FILE) if not any(p in s.exclude_folders for p in f.relative_to(root).parts)]
    else:
        files = [root / PROJECT_FILE]
    per_file = []
    for f in files:
        try:
            per_file.append([l[2:].strip() for l in f.read_text(encoding="utf-8").splitlines() if l.startswith("- ")])
        except OSError:
            pass
    return [e for group in zip_longest(*per_file) for e in group if e]


def prompt_text(s: Settings, limit: int | None = None, folder: str | None = None) -> str:
    """Per i prompt. `folder`: cartella scelta per la riunione (None = ancora ignota, vale tutta la KB).
    limit → Whisper: solo i termini (progetto prima, poi vault), i primi `limit` e al massimo WHISPER_CHARS caratteri.
    Senza limit → Haiku: voci di progetto complete di nota (incluso "trascritto anche come …") e poi i termini."""
    proj = project_entries(s, folder)
    if limit:
        names = [e.split(" — ")[0].replace("*", "").replace("`", "").strip() for e in proj]
        out, size = [], 0
        for t in dict.fromkeys(names + terms(s)):
            if len(out) >= limit or size + len(t) + 2 > WHISPER_CHARS:
                break
            out.append(t)
            size += len(t) + 2
        return ", ".join(out)
    return "\n".join([*(f"- {e}" for e in proj), ", ".join(terms(s))]).strip()


def stale(s: Settings) -> bool:
    if s.vocab_refresh_days <= 0:
        return False
    try:
        built = json.loads(META_PATH.read_text())["built"]
    except (FileNotFoundError, ValueError, KeyError):
        return find_vault(s) is not None
    return time.time() - built > s.vocab_refresh_days * 86400
