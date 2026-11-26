# Taccuino

Registratore di riunioni **sempre attivo**, ispirato a Granola, **senza bot** che entrano nella call.
Funziona su **Windows** e **Linux**. Trascrive in locale (Whisper su GPU/NPU Intel con OpenVINO); le note le scrive
Claude Code (`claude -p --model haiku`).

1. Un servizio resta in ascolto. Quando un'app (Teams, Meet, Zoom, Slack…) apre il microfono, registra
   **microfono + audio di sistema** su due canali. Quando lo rilascia, si ferma.
2. **Durante la call** trascrive in streaming e, ogni 15 minuti, Haiku corregge il testo e aggiunge appunti.
   Le persone remote vengono distinte dalla voce ("Call A", "Call B", …) e Haiku dà loro un nome quando si capisce.
3. **A fine call** Haiku scrive la nota finale (sintesi, decisioni, azioni, partecipanti), sceglie titolo e
   **sottocartella** guardando cosa c'è nell'archivio (e scrive perché), e la salva come file Markdown.

> Taccuino **non emette mai suoni**: registra soltanto. L'audio resta sul PC; al cloud va solo il testo,
> inviato a Claude tramite Claude Code.

```
 microfono ─┐                         ┌─ L: tu + chi è in stanza ─┐
            ├─ audio.flac (stereo) ───┤                           ├─ Whisper (GPU) ─▶ live.jsonl ─▶ Haiku ─▶ nota .md
 sistema  ──┘    (durante la call)    └─ R: partecipanti remoti  ─┘
```

## Da dove cominciare

| voglio… | vai a |
|---|---|
| installarlo **su Windows** | [docs/windows/README.md](docs/windows/README.md) |
| installarlo **su Linux** | [docs/linux/README.md](docs/linux/README.md) |
| farlo installare **a Claude Code** | apri Claude Code nella cartella del repo e scrivi: *"segui claude_install.md"* |
| capire comandi, impostazioni, vocabolari, modelli | [docs/reference.md](docs/reference.md) |
| lavorare sul codice | [CLAUDE.md](CLAUDE.md) |

## In breve

```bash
pip install -e .          # in un venv, dalla cartella del repo
taccuino doctor           # controlla requisiti: OpenVINO, RAM, ffmpeg, Claude Code, modello, audio
taccuino setup --download --service   # sceglie profilo e modello per la tua macchina, scarica Whisper, installa il servizio
taccuino                  # console sulla riunione in corso (o l'ultima)
```

Uso quotidiano:

```bash
taccuino status                     # in ascolto / in registrazione / in elaborazione
taccuino start "1:1 con Marco"      # registrazione manuale (riunione dal vivo)
taccuino split                      # call di fila: chiude questa riunione e ne apre un'altra
taccuino note "budget da rivedere"  # appunto, interpretato dal segretario (Haiku)
taccuino stop
```

Le note finiscono in `output_dir` (di default `Documenti/Taccuino`; `taccuino setup --output-dir …` per cambiarla).

## Com'è fatto il repo

```
taccuino/                 pacchetto Python (logica comune, uguale su ogni sistema)
  platforms/linux/        tutto ciò che dipende da Linux: cattura audio (ffmpeg+PipeWire), rilevamento mic, systemd
  platforms/windows/      tutto ciò che dipende da Windows: cattura WASAPI, registro "privacy microfono", Utilità di pianificazione
docs/
  windows/README.md       installazione, requisiti e limiti su Windows
  linux/README.md         installazione, requisiti e limiti su Linux
  reference.md            comandi, impostazioni, vocabolari, scelta dei modelli, prestazioni
tests/                    python -m unittest discover -s tests
scripts/bench_asr.py      benchmark dei modelli di trascrizione
claude_install.md         procedura di installazione per Claude Code
CLAUDE.md                 guida al codice per chi (o cosa) lo modifica
```

Per aggiungere un sistema operativo basta una nuova cartella in `taccuino/platforms/` con la stessa interfaccia
(descritta in `taccuino/platforms/__init__.py`).
