# Taccuino

Registratore di riunioni **sempre attivo** per Linux, ispirato a Granola, senza bot che entrano nella call.

1. Un servizio systemd resta in ascolto. Quando un'app (Meet, Teams, Zoom, Slack…) apre il microfono,
   registra **microfono + audio di sistema** su due canali separati. Quando l'app lo rilascia, si ferma.
2. Alla fine della riunione, in locale:
   - **diarization** (chi parla quando) con sherpa-onnx (pyannote-segmentation-3.0 + WeSpeaker), su CPU;
   - **trascrizione** con Whisper large-v3-turbo via **OpenVINO** su **Intel Iris Xe / Arc** (o NPU).
3. Le note (sintesi, decisioni, azioni, partecipanti) le scrive **Claude Code** (`claude -p --model haiku`).
4. Il risultato è un file Markdown in `~/Documents/Taccuino/`.

> Taccuino **non emette mai suoni**: registra soltanto. Niente beep e niente notifiche sonore.

```
 microfono ─┐                         ┌─ L: tu + chi è in stanza ─┐
            ├─ ffmpeg → audio.flac ───┤                           ├─ diarization → turni → Whisper (GPU)
 sistema  ──┘    (durante la call)    └─ R: partecipanti remoti  ─┘                        │
                                                                                          ▼
                       ~/Documents/Taccuino/2026-11-25 14.30 - Titolo.md  ◄── claude -p --model haiku
```

## Etichette degli speaker

- **Io**: lo speaker con più parlato sul canale microfono.
- **In sala N**: altre voci sul microfono (riunioni dal vivo).
- **Speaker N**: partecipanti remoti (canale di sistema).
- Claude prova a dedurre i nomi reali dal contesto, ad esempio "Marco (Speaker 2)".
- Se usi gli altoparlanti, le righe duplicate dall'eco vengono scartate per similarità. Le cuffie restano meglio.

## Installazione

```bash
git clone https://github.com/PitBosch/taccuino && cd taccuino
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/taccuino devices          # deve comparire GPU (e NPU sul PC di lavoro)
.venv/bin/taccuino download         # Whisper (~830 MB) + modelli di diarization (~35 MB)
.venv/bin/taccuino install-service  # servizio systemd utente, parte al login
journalctl --user -u taccuino -f    # log
```

Requisiti: PipeWire (con pipewire-pulse), ffmpeg, Claude Code autenticato (`claude` nel PATH).
Se `taccuino devices` non vede la GPU: `sudo usermod -aG render $USER` e poi rientra nella sessione.

## Durante la call

Il daemon lancia per tutta la durata della call un **live worker** (processo separato, con Whisper caricato
solo in quel momento):
- trascrive in streaming il FLAC che si sta scrivendo; ogni `live_clean_seconds` (default 15 min, in una pausa, insieme agli appunti)
  Haiku corregge in blocco le frasi accumulate (i "Grazie." inventati li toglie già un filtro deterministico);
- ogni `live_notes_minutes` (default 15) Haiku legge cosa si è detto e aggiunge **appunti mirati**
  (decisioni, azioni, numeri, date, problemi) alla nota della riunione, già nella **sottocartella** di
  `~/Documents/Taccuino` che sceglie lui alla prima passata;
- a fine call Haiku scrive la nota finale **dalla trascrizione live** (pochi secondi, niente GPU), nella
  stessa cartella, con il titolo finale e la sezione "Appunti di Claude durante la riunione".
  Per avere anche gli speaker separati (diarization + secondo passaggio di Whisper, qualche minuto di GPU):
  `taccuino config final_transcribe true`.

Console e sottotitoli leggono i file del worker: si aprono all'istante e non caricano modelli.
`taccuino config live_transcribe false` spegne tutto questo (la console torna a trascrivere da sola).

## Windows (sperimentale)

Stesso funzionamento, con queste differenze:

- **Audio**: microfono + audio di sistema via WASAPI (loopback dell'uscita predefinita), con la libreria
  `soundcard`. Il FLAC è lo stesso di Linux: L = microfono, R = sistema.
- **Rilevamento riunioni**: dal registro "privacy microfono" di Windows (le stesse app che vedi in
  Impostazioni → Privacy → Microfono quando lo stanno usando: Teams, Chrome, Zoom…).
- **Servizio**: un'attività dell'Utilità di pianificazione che avvia il daemon all'accesso, senza finestre.
- **Dati**: `%LOCALAPPDATA%\taccuino` (modelli, riunioni, `daemon.log`). Note in `Documenti\Taccuino`
  (anche se Documenti è su OneDrive).

Installazione (PowerShell):

```powershell
winget install Python.Python.3.12        # o da python.org, con "tcl/tk" selezionato (serve per i sottotitoli)
winget install Gyan.FFmpeg               # ffmpeg nel PATH (riapri il terminale dopo)
setx PYTHONUTF8 1                        # una volta sola: file e note in UTF-8 (accenti!)
# riapri il terminale, poi:
git clone -b live-console https://github.com/PitBosch/taccuino; cd taccuino
py -3.12 -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\taccuino devices           # deve comparire GPU (e NPU sui Core Ultra) + microfoni/uscite
.venv\Scripts\taccuino download          # Whisper (~830 MB) + diarization
.venv\Scripts\taccuino install-service   # attività "Taccuino" all'accesso, parte subito
.venv\Scripts\taccuino status
```

Requisiti: driver grafici Intel aggiornati (per GPU) e driver NPU Intel (Core Ultra), Claude Code installato
e autenticato (`claude` nel PATH: prova `claude -p "ciao"`). Con il profilo `npu`
(`taccuino config profile npu`) Whisper gira sulla NPU e la GPU resta libera durante le call.

Prova:

1. `taccuino start "prova"`, parla 1-2 minuti, `taccuino stop` → dopo qualche minuto la nota in `Documenti\Taccuino`.
2. Apri una call Teams: entro ~5 s `taccuino status` deve dire "registrazione in corso (MSTeams)".
3. Durante la call: `taccuino` (console) o `taccuino sottotitoli` (finestrina).
4. Log del daemon: `%LOCALAPPDATA%\taccuino\daemon.log`. Per fermarlo: `taccuino quit`;
   per toglierlo: `taccuino uninstall-service`.

Limiti noti: non testato su Windows reale; se l'uscita o il microfono predefiniti cambiano durante la call (es. cuffie
collegate dopo), la cattura li riapre da sola entro ~5 s. Se Teams usa un dispositivo diverso dal predefinito di
Windows, l'audio di sistema non viene registrato: allinea il dispositivo di Teams o imposta `system_source`.
La console semplice su Windows non ha la cronologia dei comandi (la TUI sì).

## Il segretario (appunti e Jira)

`n testo` (o `taccuino note "testo"`) non salva solo l'appunto: Haiku lo interpreta con il contesto della call
("paolino maglietta admin tenere" → "Nel montaggio tenere solo il pezzo in cui Paolino indossa la maglietta
"admin"") e lo mette nella nota, nella sezione "I miei appunti".

Se l'appunto è un'azione, il segretario prepara un task Jira:
- **chiesto esplicitamente** ("n crea task: rimontare il video entro lunedì") → lo crea subito;
- **altrimenti** → lo propone: `jira` mostra le proposte, `jira ok 2` / `jira ok tutte` le crea, `jira no 1` scarta.

Jira passa dal **connettore Atlassian già collegato a Claude Code** (nessun token in Taccuino): collegalo da
claude.ai → Impostazioni → Connettori → Atlassian e verifica con `claude mcp list`. Opzioni:
`taccuino config jira_project CHIAVE` (vuoto = il tuo progetto personale), `jira_issue_type` (default Task).

## Vocabolario dal vault Obsidian

`taccuino vocabolario` fa compilare a Haiku il vocabolario (persone, progetti, aziende, sigle, gergo tecnico)
dal tuo vault Obsidian, trovato da solo in home, Documenti o OneDrive (o `taccuino config vault_path …`).
In locale si estraggono solo i *candidati* (titoli, [[link]], alias, #tag, nomi propri, sigle): il testo delle
note non viene inviato. Lo usano Whisper (i 40 termini principali), la correzione, il segretario e le note.
Il daemon lo rigenera da solo a riposo ogni `vocab_refresh_days` (default 7). `taccuino vocabolario --show`
lo mostra; i termini in `taccuino config vocabulary "…"` si aggiungono in testa.

## Console

Scrivi `taccuino` e si apre un'interfaccia in stile Claude Code sulla riunione in corso (o sull'ultima):

```
> r 1
⏺ Si è parlato della revisione del montaggio: tenere il pezzo con Paolino, rivedere l'audio del ponte.
  (4s · Haiku)

  10:06 Io: Ci sono 30 secondi di ritardo, secondo me serve un filtro prima.
  … e poi la parte sul ponte va rivista perché                     ← bozza live (grigia)
╭──────────────────────────────────────────────────────────────────────────────╮
│ > r riassumi · t trascrizione · n nota · /help  —  o fai una domanda         │
╰──────────────────────────────────────────────────────────────────────────────╯
 ● REC 16:18 · Google Chrome input                                       Haiku
```

| comando | cosa fa |
|---|---|
| `r [min]` · `/riassumi` | riassunto degli ultimi N minuti (default 5) |
| `t [min]` · `/trascrizione` | trascrizione degli ultimi N minuti |
| `n testo` · `/nota` | appunto con timestamp |
| `/start [titolo]` · `/stop` | registrazione manuale |
| `/live` · `/stato` · `/help` · `/esci` | |
| qualsiasi frase | domanda a Claude Haiku sulla riunione |

Scrivendo `/` compare il menu dei comandi (Tab completa, ↑↓ per scegliere); Esc annulla una risposta in
arrivo; Ctrl-D esce. Senza terminale (input da pipe) o con `taccuino console --plain` parte la console semplice.

È leggera: il daemon durante la call **registra soltanto**. La trascrizione live parte solo mentre la
console è aperta (o quando chiedi qualcosa), lavora a blocchi sul FLAC che si sta ancora scrivendo e salva
il risultato in `live.jsonl`, quindi trascrive solo l'audio nuovo. Le righe live sono "Io" (microfono) e
"Call" (sistema), senza diarization. Il transcript completo con gli speaker separati arriva a fine riunione.
Anche da script: `taccuino riassumi 5`, `taccuino chiedi "…"`.

## Uso quotidiano

```bash
taccuino status                    # in ascolto / in registrazione / in elaborazione
taccuino start "1:1 con Marco"     # registrazione manuale (riunione dal vivo, senza app)
taccuino note "budget da rivedere" # appunto con timestamp, Claude gli dà priorità
taccuino stop
taccuino list                      # ultime riunioni
taccuino process registrazione.m4a # elabora un file audio/video esistente
taccuino summarize ~/.local/share/taccuino/meetings/<cartella>   # rigenera solo le note
```

Impostazioni (`taccuino config` le mostra tutte, `taccuino config KEY VALUE` le modifica):

| chiave | default | note |
|---|---|---|
| `profile` | `xe` | `xe` (GPU), `npu`, `cpu` |
| `asr_model` | — | sostituisce il modello del profilo (repo HF OpenVINO) |
| `language` | `it` | `en`, oppure `auto` (sconsigliato: Whisper allucina in inglese sui silenzi) |
| `vocabulary` | — | nomi e termini ricorrenti: migliora la trascrizione |
| `claude_extra_prompt` | — | istruzioni extra per le note |
| `ignore_apps` | ffmpeg, obs… | app che aprono il microfono senza essere riunioni |
| `start_delay` / `stop_delay` | 5 s / 20 s | isteresi del rilevamento |
| `min_meeting_seconds` | 60 | sotto questa durata la registrazione viene scartata (es. vocali) |
| `num_speakers` | -1 | -1 = automatico; impostalo se sai quante persone ci sono |
| `cluster_threshold` | 0.6 | più alto = meno speaker |
| `final_transcribe` | false | true = a fine call ritrascrive tutto con diarization (più lento) |
| `live_clean_seconds` | 900 | ogni quanto Haiku corregge le frasi della trascrizione live (0 = solo a fine call) |
| `live_notes_minutes` | 15 | ogni quanti minuti Claude aggiunge appunti durante la call (0 = mai) |
| `keep_audio_days` | 14 | 0 = cancella l'audio subito dopo l'elaborazione |

## Prestazioni misurate (i5-1145G7, Iris Xe 80 EU, 16 GB)

| passo | device | velocità |
|---|---|---|
| Whisper large-v3-turbo int8 | GPU Iris Xe | RTF ~0,2–0,28 (2 min di audio in 24–33 s) |
| Diarization sherpa-onnx | CPU, 4 thread | RTF ~0,075 |
| Note con Claude Haiku | cloud | ~15 s |

Un'ora di riunione richiede circa 20–25 minuti di elaborazione in background a fine call.

## Scelta dei modelli (novembre 2026)

**Trascrizione (OpenVINO, GPU Intel).** Modelli che girano con `openvino_genai` 2026.4:

| modello | dimensione | note |
|---|---|---|
| **Whisper large-v3-turbo int8** (`OpenVINO/…`) | 830 MB | default: ottimo rapporto qualità/velocità su iGPU, conversione ufficiale Intel |
| Whisper large-v3 int8 (`OpenVINO/…`) | 1,6 GB | un po' più preciso, ~3–4× più lento |
| Qwen3-ASR 1.7B int8 (conversioni community) | 2,4 GB | italiano FLEURS ~2,4% WER dal report tecnico; `ASRPipeline` |
| Qwen3-ASR 0.6B int8 | 1 GB | più leggero, italiano ~5% WER |
| distil-whisper large-v3 | — | **solo inglese**: escluso |
| Parakeet v3, Canary, Cohere Transcribe, Fun-ASR-MLT | — | nessuna conversione OpenVINO pronta (servirebbe export manuale) |

Benchmark su Iris Xe (clip italiana di 2 min, finestre da 20 s, `scripts/bench_asr.py`):

| modello | load | RTF | qualità osservata |
|---|---|---|---|
| Whisper large-v3-turbo int8 | 2,4 s | **0,13** | buona; a volte salta frasi pronunciate in fretta |
| Qwen3-ASR 1.7B int8 (`circulus/…`) | 12,5 s | 0,45 | coglie più frasi, ma storpia di più i nomi propri (Florianópolis → "Firenopolis") |

WER su 120 frasi italiane di FLEURS test (30 min di audio, GPU Iris Xe): **Whisper turbo 2,91%** (RTF 0,22),
Qwen3-ASR 1.7B 3,43% (RTF 0,63). Fun-ASR non supporta l'italiano.

Whisper turbo resta il default (3,4× più veloce, qualità comparabile). Qwen3-ASR si può attivare con
`taccuino config asr_model circulus/Qwen3-ASR-1.7B-int8-ov`. Attenzione: la conversione `paloik/…`
ha un layout di file non compatibile con `ASRPipeline`.

**Diarization.** sherpa-onnx è leggero (~35 MB), funziona offline ed è abbastanza veloce su CPU.
Un'alternativa su GPU Intel è `pyannote-openvino` (pyannote 3.1 convertito in IR), ma richiede torch
e pyannote.audio.

**NPU (PC di lavoro, Core Ultra).** `taccuino config profile npu` fa girare Whisper sulla NPU, così la GPU
resta libera durante le call. Serve OpenVINO ≥ 2026.4 (la 2026.3 aveva un bug di stato con Whisper su NPU,
openvinotoolkit/openvino#37937) e il driver [intel/linux-npu-driver](https://github.com/intel/linux-npu-driver).
La diarization resta su CPU.

## Dati

- `~/.local/share/taccuino/meetings/<data>/`: `audio.flac` (stereo L=mic, R=sistema), `transcript.json`,
  `notes.txt`, `meta.json`.
- `~/Documents/Taccuino/*.md`: note finali.
- Al cloud va solo il testo del transcript, inviato a Claude tramite Claude Code. L'audio resta sul PC.
