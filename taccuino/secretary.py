"""Il "segretario": Haiku interpreta gli appunti che scrivi (`n testo`) e, se serve, prepara task Jira.

- l'appunto viene riscritto chiaro e completo usando il contesto della call (nomi, di cosa si parlava)
  e finisce nella nota della riunione;
- se chiedi esplicitamente un task ("crea task…", "ticket…", "jira: …") lo crea subito sul tuo Jira;
- se nell'appunto c'è un'azione che varrebbe la pena tracciare ma non l'hai chiesto, lo PROPONE soltanto:
  `jira` mostra le proposte, `jira ok N` le crea, `jira no N` le scarta.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

from .config import Settings
from .pipeline import fmt_ts

EXPLICIT = re.compile(r"\b(task|ticket|jira|issue|crea(re)? (un )?(task|ticket)|apri(re)? (un )?(task|ticket))\b", re.I)

PROMPT = """Sei il segretario dell'utente durante una riunione. L'utente ha appena scritto un appunto veloce
(abbreviato, senza contesto). Interpretalo usando il transcript recente della call.

Oggi è {today}. {when}
Appunto dell'utente: «{note}»

Transcript recente (automatico, può avere errori):
{ctx}

Rispondi SOLO con un oggetto JSON, senza testo prima o dopo:
{{"appunto": "<l'appunto riscritto chiaro e completo, 1-2 frasi, fedele a ciò che intende l'utente; niente invenzioni>",
  "esplicito": <true se l'utente chiede ESPLICITAMENTE di creare un task/ticket Jira, altrimenti false>,
  "jira": [{{"titolo": "<titolo del task, imperativo, max 12 parole>",
             "descrizione": "<cosa fare e perché, con il contesto utile della riunione>",
             "scadenza": "<YYYY-MM-DD se detta o deducibile (es. 'venerdì'), altrimenti null>"}}],
  "risposta": "<conferma brevissima per l'utente, max 15 parole>"}}
"jira" contiene task solo se l'appunto descrive un'azione concreta da fare; altrimenti []."""


def _ctx_lines(mdir: Path | None, s: Settings, minutes: float = 5) -> str:
    if not mdir:
        return "(nessuna riunione in corso)"
    from .live import LiveTranscript
    lines = LiveTranscript(mdir, s).clean_lines()
    if not lines:
        return "(ancora niente)"
    end = lines[-1].end
    return "\n".join(f"[{fmt_ts(l.start)}] {l.speaker}: {l.text}" for l in lines if l.end >= end - minutes * 60)


def _parse(out: str) -> dict:
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise ValueError("risposta non in JSON")
    return json.loads(m.group(0))


def proposals_path(mdir: Path) -> Path:
    return mdir / "jira_proposals.json"


def load_proposals(mdir: Path | None) -> list[dict]:
    try:
        return json.loads(proposals_path(mdir).read_text()) if mdir else []
    except (FileNotFoundError, ValueError):
        return []


def save_proposals(mdir: Path, items: list[dict]):
    proposals_path(mdir).write_text(json.dumps(items, ensure_ascii=False, indent=1))


def handle_note(s: Settings, note: str, mdir: Path | None, elapsed: float | None) -> list[str]:
    """Interpreta l'appunto; restituisce le righe da mostrare all'utente."""
    from . import jira
    from .summarize import run_claude
    when = f"Siamo al minuto {fmt_ts(elapsed)} della riunione." if elapsed is not None else ""
    msgs: list[str] = []
    try:
        res = _parse(run_claude(s, PROMPT.format(today=datetime.now().strftime("%A %d/%m/%Y"), when=when,
                                                 note=note, ctx=_ctx_lines(mdir, s)), timeout=90))
    except Exception as e:  # noqa: BLE001
        return [f"appunto salvato così com'è (Haiku non disponibile: {e})"]
    text = (res.get("appunto") or note).strip()
    if mdir:
        ts = f"[{fmt_ts(elapsed)}] " if elapsed is not None else ""
        with open(mdir / "secretary.md", "a") as f:
            f.write(f"- {ts}{text}\n")
    msgs.append(f"annotato: {text}")
    tasks = [t for t in res.get("jira") or [] if t.get("titolo")]
    explicit = bool(res.get("esplicito")) and bool(EXPLICIT.search(note))  # doppio controllo: Haiku + parole chiave
    for t in tasks:
        due = t.get("scadenza") if re.match(r"^\d{4}-\d{2}-\d{2}$", str(t.get("scadenza") or "")) else None
        if explicit:
            if not jira.configured(s):
                msgs.append("Jira non collegato: aggiungi il connettore Atlassian a Claude Code "
                                "(claude.ai → Impostazioni → Connettori). Tengo il task tra le proposte.")
                explicit = False
            else:
                try:
                    key, url = jira.create_issue(s, t["titolo"], t.get("descrizione", ""), due)
                    msgs.append(f"creato {key}: {t['titolo']}{' (scadenza ' + due + ')' if due else ''} — {url}")
                    continue
                except jira.JiraError as e:
                    msgs.append(f"Jira: {e}. Tengo il task tra le proposte.")
        if mdir:
            props = load_proposals(mdir)
            props.append({"titolo": t["titolo"], "descrizione": t.get("descrizione", ""), "scadenza": due,
                          "stato": "proposta", "creato": time.time()})
            save_proposals(mdir, props)
            n = len([p for p in props if p["stato"] == "proposta"])
            msgs.append(f"proposta Jira {len(props)}: {t['titolo']} — `jira ok {len(props)}` per crearla"
                        + (f" ({n} in attesa)" if n > 1 else ""))
    return msgs


def jira_command(s: Settings, mdir: Path | None, arg: str) -> list[str]:
    """`jira` = elenco proposte · `jira ok N|tutte` = crea · `jira no N` = scarta."""
    from . import jira
    props = load_proposals(mdir)
    verb, _, rest = arg.strip().partition(" ")
    if not verb:
        rows = [f"{i}. [{p['stato']}] {p['titolo']}" + (f" (scadenza {p['scadenza']})" if p.get("scadenza") else "")
                + (f" → {p['key']}" if p.get("key") else "") for i, p in enumerate(props, 1)]
        return rows or ["nessuna proposta Jira in questa riunione"]
    idx = [i for i, p in enumerate(props) if p["stato"] == "proposta"] if rest.strip() in ("tutte", "all", "") \
        else [int(x) - 1 for x in re.findall(r"\d+", rest)]
    msgs = []
    for i in idx:
        if not 0 <= i < len(props):
            msgs.append(f"proposta {i + 1} inesistente")
            continue
        p = props[i]
        if verb in ("ok", "si", "sì", "crea"):
            if p["stato"] != "proposta":
                msgs.append(f"{i + 1}: già {p['stato']}")
                continue
            try:
                key, url = jira.create_issue(s, p["titolo"], p.get("descrizione", ""), p.get("scadenza"))
                p.update(stato="creata", key=key, url=url)
                msgs.append(f"creato {key}: {p['titolo']} — {url}")
            except jira.JiraError as e:
                msgs.append(f"{i + 1}: {e}")
        elif verb in ("no", "scarta"):
            p["stato"] = "scartata"
            msgs.append(f"{i + 1}: scartata")
    if mdir:
        save_proposals(mdir, props)
    return msgs or ["uso: jira · jira ok N · jira ok tutte · jira no N"]
