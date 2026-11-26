# Installazione di Taccuino con Claude Code

Questo file è una procedura per **Claude Code**. L'utente ti ha chiesto di installare Taccuino su questa macchina:
segui i passi nell'ordine, **verifica ogni passo prima di passare al successivo**, e non dichiarare "fatto" finché
l'ultimo controllo non è passato. Se un passo fallisce due volte, fermati e spiega all'utente cosa hai provato.

## Regole

- Parla all'utente in italiano. Lavora nella cartella di questo repo (quella con `pyproject.toml`).
- **Non scaricare ciò che non serve**: Whisper sì; i modelli di diarization solo se l'utente li vuole
  (`final_transcribe`). Il modello Whisper pesa ~830 MB: dillo prima di scaricare se la connessione è lenta.
- **Chiedi all'utente** (una volta, tutte insieme) prima di cominciare: dove vuole le note (`output_dir`, può essere un
  vault Obsidian) e se vuole il servizio all'accesso. Il resto lo decidi tu.
- Non modificare impostazioni che l'utente ha già in `config.json`: `taccuino setup` non lo fa, non farlo a mano.
- Non fare `git push` né modificare il repo: stai installando, non sviluppando.
- Non lanciare test nel browser. Non registrare audio senza dirlo all'utente (la prova finale lo richiede: chiedi).

## 1. Capisci la macchina

```bash
python --version        # serve ≥ 3.10 (consigliato 3.12)
```

- **Windows** → PowerShell. Prerequisiti: `winget install Python.Python.3.12`, `winget install Gyan.FFmpeg`,
  `setx PYTHONUTF8 1` (poi serve una nuova shell: nelle chiamate successive usa `$env:PYTHONUTF8=1`).
- **Linux** → bash. Prerequisiti: PipeWire con pipewire-pulse, `ffmpeg`, `python3-venv`.
- In ogni caso serve **Claude Code autenticato**: `claude -p "ciao"` deve rispondere. Se non risponde, chiedi
  all'utente di fare il login (`! claude` nel prompt) e riprova.

Controlla: `ffmpeg -version` e `ffprobe -version` funzionano. Altrimenti installali e riapri la shell.

## 2. Ambiente e pacchetto

```bash
# Windows                                  # Linux
py -3.12 -m venv .venv                     python3 -m venv .venv
.venv\Scripts\pip install -e .             .venv/bin/pip install -e .
```

Controllo: `.venv/Scripts/taccuino --help` (Windows) o `.venv/bin/taccuino --help` (Linux) stampa i comandi.
L'installazione scarica OpenVINO e sherpa-onnx: può richiedere qualche minuto.

## 3. Diagnosi

```bash
taccuino doctor
```

(usa il percorso completo dell'eseguibile nel venv se `taccuino` non è nel PATH). Leggi ogni riga:

| riga | se ✗ |
|---|---|
| `OpenVINO` | deve elencare almeno `CPU`; `GPU` è quella che serve. Senza GPU Intel si userà la CPU con un modello piccolo. Su Linux: `sudo usermod -aG render $USER` e nuova sessione. |
| `ffmpeg` / `ffprobe` | installali (passo 1) |
| `Claude Code` | login (passo 1) |
| `modello …` e `daemon` | normali ora: si sistemano al passo 4 |

`·` sono informazioni, non problemi. Non passare al passo 4 con altre ✗.

## 4. Configurazione, modello e servizio

```bash
taccuino setup --yes --download --service --output-dir "<cartella scelta dall'utente>"
```

Ometti `--output-dir` se l'utente accetta il default (`Documenti/Taccuino`) e `--service` se non lo vuole.
`setup` sceglie profilo e modello in base a RAM e dispositivi e **te lo scrive**: riportalo all'utente. Regole:

- GPU Intel + ≥ 12 GB di RAM → `xe` + `turbo-int8`; 8–12 GB → `turbo-int4`; meno → `small-int8`; senza GPU → `cpu`.
- **L'NPU non viene mai scelta da sola** (instabile in call lunghe). Se l'utente la vuole: `--profile npu`.
- Le soglie di RAM sono euristiche, non misurate. Se la macchina è stretta, suggerisci `scripts/bench_asr.py`.

Controllo: `taccuino doctor` ora deve mostrare ✓ su modello e su `daemon: attivo` (se hai installato il servizio).

## 5. Prova (chiedi il permesso: registra audio)

1. `taccuino status` → "in ascolto".
2. `taccuino start "prova"`; chiedi all'utente di parlare ~30 secondi (anche con un video che suona, per il canale di
   sistema); poi `taccuino stop`. Con registrazioni < 60 s e non manuali il daemon scarta: questa è manuale, non si scarta.
3. Attendi la nota (1–3 minuti: Haiku): deve comparire un `.md` in `output_dir`. Se non compare, leggi il log del daemon
   (Windows: `%LOCALAPPDATA%\taccuino\daemon.log`; Linux: `journalctl --user -u taccuino`).
4. Facoltativo: apri una call (Teams/Meet) e verifica che `taccuino status` dica "registrazione in corso".

## 6. Riepilogo all'utente

Scrivi: sistema e hardware rilevati, profilo/modello scelti e perché, dove stanno note e dati, come aprire la console
(`taccuino`), e **ciò che non hai verificato** (ad esempio la call vera, l'NPU, il servizio dopo un riavvio).

## Problemi noti

- **Windows**: `taccuino` non riconosciuto in PowerShell → usa il percorso completo di `.venv\Scripts\taccuino.exe` o
  aggiungi la funzione al profilo (vedi `docs/windows/README.md`; PowerShell 5.1 e 7 hanno profili diversi).
  `schtasks` negato senza amministratore → c'è già il ripiego automatico. Accenti rovinati → `PYTHONUTF8`.
- **Audio di sistema assente** (solo il microfono nella nota): Teams/Meet usa un dispositivo diverso dal predefinito;
  vedi `taccuino devices` e `taccuino config system_source`.
- **RAM alta**: un solo Whisper alla volta è in memoria; ~1–2 GB con `turbo-int8`. Prova `--model turbo-int4` (da verificare).
- **Linux**: l'integrazione audio/systemd non è stata riprovata su una macchina Linux reale dopo la ristrutturazione
  (`docs/linux/README.md`): se un passo fallisce, riporta l'errore esatto all'utente.
