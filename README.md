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
| `claude_model` | `haiku` | modello usato da Claude Code |
| `claude_extra_prompt` | — | istruzioni extra per le note |
| `ignore_apps` | ffmpeg, obs… | app che aprono il microfono senza essere riunioni |
| `start_delay` / `stop_delay` | 5 s / 20 s | isteresi del rilevamento |
| `min_meeting_seconds` | 60 | sotto questa durata la registrazione viene scartata (es. vocali) |
| `num_speakers` | -1 | -1 = automatico; impostalo se sai quante persone ci sono |
| `cluster_threshold` | 0.6 | più alto = meno speaker |
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

Il benchmark tra i candidati si lancia con `scripts/bench_asr.py`.

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
