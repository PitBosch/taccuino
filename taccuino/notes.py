"""Generazione delle note "alla Granola": le tue note grezze + transcript → note complete."""
from __future__ import annotations

from typing import Callable

from .engines import LLM, chunk_text

LANG_NAME = {"it": "italiano", "en": "English"}

SYSTEM = (
    "Sei un assistente che scrive note di riunione chiare e fedeli. "
    "Usa SOLO informazioni presenti nel transcript o nelle note dell'utente; non inventare nomi, numeri o decisioni. "
    "You are bilingual Italian/English: write in the requested output language."
)

ENHANCE = """Lingua di output / Output language: {lang}

TITOLO RIUNIONE: {title}

NOTE GREZZE DELL'UTENTE (sono la priorità: mantieni i suoi punti e la sua struttura, arricchiscili con i dettagli del transcript):
<<<
{notes}
>>>

TRANSCRIPT ("Io" = l'utente, "Altri" = gli altri partecipanti):
<<<
{transcript}
>>>

Scrivi le note finali in Markdown con questa struttura:
## Sintesi
(2-4 frasi)
## Punti discussi
(elenco puntato, raggruppato per argomento con ### sottotitoli se utile)
## Decisioni
## Azioni
(- [ ] chi — cosa — entro quando, se detto)
## Domande aperte
Ometti le sezioni vuote. Se l'output è in inglese traduci anche i titoli delle sezioni."""

PARTIAL = """Lingua di output / Output language: {lang}
Questa è la parte {i} di {n} del transcript di una riunione ("Io" = utente, "Altri" = altri partecipanti).
Estrai in elenco puntato, in modo fedele e conciso: argomenti, fatti e numeri citati, decisioni, azioni (chi/cosa/quando), domande aperte.
<<<
{chunk}
>>>"""


def detect_language(segments: list[dict], forced: str | None) -> str:
    if forced:
        return forced
    words = " ".join(s["text"] for s in segments).lower().split()
    it = sum(w in {"che", "non", "di", "il", "è", "la", "per", "un", "sono", "abbiamo", "questo"} for w in words)
    en = sum(w in {"the", "and", "is", "to", "of", "that", "we", "it", "you", "this", "are"} for w in words)
    return "en" if en > it else "it"


def format_transcript(segments: list[dict]) -> list[str]:
    out = []
    for s in segments:
        m, sec = divmod(int(s["t0"]), 60)
        who = "Io" if s["speaker"] == "me" else "Altri"
        out.append(f"[{m:02d}:{sec:02d}] {who}: {s['text']}")
    return out


def enhance(llm: LLM, title: str, notes: str, segments: list[dict], language: str | None,
            on_token: Callable[[str], None] | None = None,
            on_status: Callable[[str], None] | None = None) -> str:
    lang_code = detect_language(segments, language)
    lang = LANG_NAME.get(lang_code, lang_code)
    lines = format_transcript(segments)
    chunks = chunk_text(lines, llm.profile.llm_chunk_chars)
    if len(chunks) > 1:
        # map-reduce: riassumi ogni parte, poi unisci
        partials = []
        for i, ch in enumerate(chunks, 1):
            if on_status:
                on_status(f"Analizzo parte {i}/{len(chunks)}…")
            partials.append(llm.chat(SYSTEM, PARTIAL.format(lang=lang, i=i, n=len(chunks), chunk=ch), max_new_tokens=600))
        transcript = "\n\n".join(f"(Estratto parte {i})\n{p}" for i, p in enumerate(partials, 1))
    else:
        transcript = chunks[0] if chunks else "(nessun transcript)"
    if on_status:
        on_status("Scrivo le note…")
    return llm.chat(SYSTEM, ENHANCE.format(lang=lang, title=title or "(senza titolo)",
                                           notes=notes.strip() or "(nessuna)", transcript=transcript),
                    on_token=on_token)


ASK = """Rispondi alla domanda usando solo il transcript e le note. Lingua: la stessa della domanda.
NOTE:
{notes}
TRANSCRIPT:
{transcript}
DOMANDA: {q}"""


def ask(llm: LLM, q: str, notes: str, segments: list[dict], on_token=None) -> str:
    lines = format_transcript(segments)
    transcript = "\n".join(lines)[-llm.profile.llm_chunk_chars:]
    return llm.chat(SYSTEM, ASK.format(notes=notes or "-", transcript=transcript, q=q), on_token=on_token, max_new_tokens=600)
