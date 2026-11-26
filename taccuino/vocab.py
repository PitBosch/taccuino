"""Vocabolari di progetto: file `Vocabolario.md` nella KB, usati da Whisper, dal correttore di Haiku e dalle note.

Formato: una voce per riga, `- Termine — nota` (la nota può dire come Whisper lo sbaglia). Le prime voci contano di più:
a Whisper arrivano solo i termini, tagliati a ~400 caratteri; Haiku legge il file intero."""
from __future__ import annotations

from itertools import zip_longest
from pathlib import Path

NAME = "Vocabolario.md"
WHISPER_CHARS = 400  # Whisper accetta ~224 token di prompt


def load(s, folder: str = "") -> tuple[str, str]:
    """(testo completo per Haiku, lista termini per Whisper).
    Cartella scelta → vocabolari in quella cartella e nei genitori; cartella ancora ignota → tutti quelli della KB."""
    root = Path(s.output_dir)
    if folder:
        files = [d / NAME for d in [root / folder, *(root / folder).parents] if d == root or root in d.parents]
    else:
        files = [f for f in root.rglob(NAME) if not any(p in s.exclude_folders for p in f.relative_to(root).parts)]
    texts = []
    for f in files:
        try:
            texts.append(f.read_text(encoding="utf-8"))
        except OSError:
            pass
    full = "\n".join(([s.vocabulary] if s.vocabulary else []) + texts).strip()
    per_file = [[line[2:].split(" — ")[0].replace("*", "").replace("`", "").strip()
                 for line in t.splitlines() if line.startswith("- ")] for t in texts]
    terms = [s.vocabulary] if s.vocabulary else []
    for group in zip_longest(*per_file):  # un termine a testa per file: con più progetti nessuno resta fuori dal prompt
        terms += [t for t in group if t and t not in terms]
    prompt = ""
    for t in terms:
        if len(prompt) + len(t) + 2 > WHISPER_CHARS:
            break
        prompt += (", " if prompt else "") + t
    return full[:8000], prompt


if __name__ == "__main__":  # controllo: python -m taccuino.vocab ["Gestione - Luxottica"]
    import sys

    from .config import load_settings
    full, prompt = load(load_settings(), sys.argv[1] if len(sys.argv) > 1 else "")
    print(f"{len(full)} caratteri per Haiku; Whisper ({len(prompt)}): {prompt}")
