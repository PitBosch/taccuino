"""Jira tramite il connettore Atlassian (MCP) già collegato a Claude Code: niente token qui dentro.

Taccuino chiede a Claude Code (sempre Haiku, `claude -p`) di creare il task usando SOLO gli strumenti
del connettore Atlassian. Il connettore viene trovato da solo (`claude mcp list`); si può forzare con
`taccuino config jira_mcp "claude.ai Atlassian"`. Progetto: `taccuino config jira_project CHIAVE`
(vuoto = Claude usa il tuo progetto personale, se ne hai uno solo).
"""
from __future__ import annotations

import re
import subprocess
import tempfile

from .compat import NO_WINDOW
from .config import Settings

_cache: dict[str, str] = {}


class JiraError(RuntimeError):
    pass


def _server(s: Settings) -> str:
    """Nome del server MCP Atlassian come lo vede Claude Code (es. "claude.ai Atlassian")."""
    if s.jira_mcp:
        return s.jira_mcp
    if "server" in _cache:
        return _cache["server"]
    from .summarize import command
    try:
        out = subprocess.run(command(s.claude_bin) + ["mcp", "list"], capture_output=True, text=True,
                             timeout=90, cwd=tempfile.gettempdir(), encoding="utf-8", **NO_WINDOW).stdout
    except Exception as e:  # noqa: BLE001
        raise JiraError(f"non riesco a leggere i connettori di Claude Code: {e}") from None
    for line in out.splitlines():
        name = line.split(":", 1)[0].strip()
        if re.search(r"atlassian|jira", line, re.I) and "Connected" in line:
            _cache["server"] = name
            return name
    raise JiraError("nessun connettore Atlassian/Jira collegato a Claude Code "
                    "(claude.ai → Impostazioni → Connettori → Atlassian)")


def _tool_prefix(server: str) -> str:
    # "claude.ai Atlassian" → "mcp__claude_ai_Atlassian" (come Claude Code nomina gli strumenti MCP)
    return "mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", server)


def configured(s: Settings) -> bool:
    try:
        _server(s)
        return True
    except JiraError:
        return False


PROMPT = """Crea UN issue nel Jira personale dell'utente usando gli strumenti Atlassian disponibili.
- Sito: il primo sito Atlassian accessibile (o "{site}" se indicato).
- Progetto: {project}
- Tipo: {itype} (se non esiste nel progetto, usa il tipo più simile, es. "Attività")
- Titolo: {summary}
- Descrizione: {description}
- Scadenza (duedate): {due}
- Etichetta: taccuino (se il campo labels è disponibile)
Non fare nient'altro. Alla fine rispondi con UNA sola riga:
CREATO <CHIAVE> <URL dell'issue>
oppure, se non è possibile:
ERRORE <motivo breve>"""


def create_issue(s: Settings, summary: str, description: str = "", due: str | None = None) -> tuple[str, str]:
    """Crea il task tramite Claude Code + connettore Atlassian. Restituisce (chiave, url)."""
    from .summarize import run_claude
    server = _server(s)
    project = (f'chiave "{s.jira_project}"' if s.jira_project else
               "il progetto personale dell'utente (se ce n'è più di uno e nessuno è chiaramente personale, "
               "rispondi ERRORE elencando le chiavi)")
    prompt = PROMPT.format(site=s.jira_url or "-", project=project, itype=s.jira_issue_type, summary=summary,
                           description=description or "-", due=due or "nessuna")
    try:
        out = run_claude(s, prompt, timeout=180, allow_tools=[_tool_prefix(server)])
    except Exception as e:  # noqa: BLE001
        raise JiraError(str(e)) from None
    m = re.search(r"CREATO\s+([A-Z][A-Z0-9_]+-\d+)\s+(\S+)", out)
    if not m:
        err = re.search(r"ERRORE\s+(.+)", out)
        raise JiraError(err.group(1).strip() if err else out.strip()[-300:])
    return m.group(1), m.group(2)
