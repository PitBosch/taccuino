# Taccuino — guida per chi modifica il codice

Registratore di riunioni sempre attivo (Windows e Linux): daemon + live worker + console/TUI + pipeline. Trascrive con
Whisper (OpenVINO GenAI, GPU/NPU/CPU Intel); note, titoli, scelta della cartella e correzioni le fa Haiku via
`claude -p`. Per l'installazione vedi `claude_install.md`; per l'uso `README.md` e `docs/`.

## Regola d'oro: il codice che dipende dal sistema operativo sta solo in `taccuino/platforms/`

- `platforms/linux/` e `platforms/windows/` espongono **la stessa interfaccia** (elencata in `platforms/__init__.py`):
  cattura audio (`Recorder`), `mic_users`, servizio, `try_lock`, percorsi, RAM.
- Il resto del pacchetto importa solo da `taccuino.platforms` (o dalle facciate `compat.py`/`detect.py`). Niente
  `sys.platform`, `msvcrt`, `fcntl`, `pw-dump`, `schtasks` fuori da lì. `tests/test_platforms.py` controlla che le due
  cartelle abbiano la stessa interfaccia.
- Gli import di `config` dentro `platforms/*` vanno **dentro le funzioni** (config importa platforms: ciclo).
- Nuovo sistema operativo = nuova cartella con la stessa interfaccia.

## Mappa

| modulo | cosa fa |
|---|---|
| `daemon.py` | ciclo principale: rileva il mic, avvia/ferma la registrazione, coda di elaborazione, comandi (`control`) |
| `live.py`, `live_worker.py` | trascrizione in streaming (`LiveTranscript`), correzione di Haiku (`Cleaner`, anche i nomi delle voci), appunti live (`LiveNotes`) |
| `speakers.py` | voci "Call A/B…" nel live: embedding wespeaker leggero + clustering online (`live.speakers.json`) |
| `pipeline.py` | `process_meeting`: nota finale; `resummarize` |
| `summarize.py` | `run_claude`, prompt di nota, `choose_folder`, `existing_folders` |
| `split.py` | divisione di registrazioni con più riunioni (Haiku, manuale, dal vivo) |
| `engines.py` | `ASR` (retry su device perso), `Diarizer` |
| `vocab.py` | vocabolario dal vault + `Vocabolario.md` di progetto (`prompt_text`) |
| `setup_wizard.py` | `taccuino doctor` e `taccuino setup` |
| `config.py` | `Settings`, profili e catalogo modelli (`ASR_PROFILES`, `ASR_MODELS`) |
| `console.py`, `tui.py`, `captions.py` | console semplice, TUI (prompt_toolkit), sottotitoli |
| `secretary.py`, `jira.py` | appunti interpretati e task Jira |

Dati utente (non nel repo): `<dati>/config.json`, `state.json`, `meetings/<data>/…` (vedi `docs/reference.md`).

## Sviluppo

```bash
python -m unittest discover -s tests          # nessuna dipendenza oltre al venv del progetto; deve restare verde
python -m taccuino.cli doctor                 # diagnosi della macchina
```

- Sulla macchina di sviluppo il daemon può essere in esecuzione: **non toccare `<dati>/state.json` né `config.json` nei
  test**; usa `TACCUINO_HOME=<cartella temporanea>` (dati e config finti) o i `mock.patch` dei percorsi come nei test.
- Windows: avvia Python con `PYTHONUTF8=1`. In heredoc/shell evita `\n` dentro stringhe Python passate da riga di
  comando (si rompono): scrivi un file.
- Test sul browser: solo a fine sessione e solo se necessari.

## Convenzioni

- **Lingua**: testi per l'utente, log e commenti in italiano, con gli accenti giusti; identificatori in inglese/italiano
  come nel resto del codice. File sempre in UTF-8.
- **Privacy**: nessun dato di cliente, nome di persona o percorso personale nel repo, nei test o negli esempi. I
  `Vocabolario.md` vivono nella KB dell'utente, **mai** nel repo. Al cloud va solo testo, tramite Claude Code.
- **Niente suoni**: Taccuino registra soltanto, non riproduce mai audio.
- **Un solo modello Whisper in RAM**. Su errore del device (`DEVICE_LOST`) si ricarica sullo stesso device e si riprova
  (max 2); **nessun ripiego automatico su un altro device** (scelta voluta). La GPU è il profilo di default; l'NPU non
  si sceglie mai da sola.
- Le correzioni di bug nascono da un caso reale con un test che lo riproduce (vedi `tests/`).
- Non dichiarare "funziona" senza averlo eseguito; distingui fatti, ipotesi e cose non verificate nei messaggi e nei docs.

## Insidie già incontrate

- Windows: `os.kill(pid, 0)` **termina** il processo, mai usarlo per controllare se vive. `os.replace` può fallire se un
  altro processo ha il file aperto. `schtasks /SC ONLOGON` richiede amministratore (ripiego `Register-ScheduledTask`).
- `state.json` viene riscritto dal daemon: leggerlo sempre con tolleranza (`cli._state`).
- Il loopback WASAPI non manda nulla quando non suona niente: la cattura riempie di silenzio per restare allineata.
- `find_vault`: una cartella come `Desktop` può avere un `.obsidian` e dentro `.venv`: si prende il vault più interno e si
  ignorano `.venv`/`site-packages` (test di regressione in `tests/test_vocab.py`).
- Tra due call di fila spesso **non c'è silenzio**: non usare le pause per separare le riunioni (vedi `split.py`).
- Whisper con `initial_prompt` può ripetere il prompt su silenzio: `ASR._transcribe` scarta quei casi.

## Git

- Lavora su un branch; **non pubblicare su `master`** senza che l'utente lo chieda. Commit piccoli, messaggio in
  italiano che dice il perché. Non committare `config.json`, vocabolari di progetto, audio, o file con credenziali.
- Messaggi di commit e PR terminano con le righe di attribuzione richieste dall'ambiente.
