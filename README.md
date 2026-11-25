# Taccuino

Note di riunione **100% locali** in stile Granola: scrivi appunti veloci durante la call, Taccuino
registra microfono + audio di sistema, trascrive in tempo reale e alla fine fonde i tuoi appunti con il
transcript in note strutturate (sintesi, decisioni, azioni). Italiano e inglese.

Gira su **Intel Iris Xe / Arc** (GPU integrata) e **Intel NPU** (Core Ultra) tramite
[OpenVINO GenAI](https://github.com/openvinotoolkit/openvino.genai). Niente cloud, niente bot che entra
nella call: cattura l'audio del PC come fa Granola.

> Taccuino **non riproduce mai audio**: registra solo. Nessun beep, nessuna notifica sonora.

## Come funziona

```
 mic (PipeWire) ──┐                    ┌─ canale L "Io"   ─┐
                  ├─ ffmpeg 16 kHz ────┤                   ├─ VAD → Whisper (GPU/NPU) → transcript live
 monitor sistema ─┘                    └─ canale R "Altri"─┘
                                                     ▼
                tue note + transcript ──► LLM (GPU/NPU) ──► note finali (Markdown)
```

- Due canali separati → il transcript distingue **Io** / **Altri** senza diarizzazione.
- Segmentazione sui silenzi (VAD a energia adattiva), segmenti da 4–25 s.
- Riunioni lunghe: map-reduce (riassunto per parti, poi note finali).
- Archivio SQLite in `~/.local/share/taccuino/`, export Markdown, "Chiedi" sulla riunione.
- Import di file audio/video esistenti.

## Installazione

```bash
git clone <questa repo> && cd taccuino
python3 -m venv .venv && .venv/bin/pip install -e .
# se `taccuino devices` non vede la GPU: sudo usermod -aG render $USER (poi rilogin)
.venv/bin/taccuino devices             # deve elencare GPU (e NPU sul PC di lavoro)
.venv/bin/taccuino download            # ~3 GB per il profilo xe
.venv/bin/taccuino serve               # → http://127.0.0.1:8765
```

Driver GPU su Ubuntu 24.04: `intel-opencl-icd` (già presente) basta; per Arc/Lunar Lake conviene il
compute-runtime recente di Intel.

Comandi utili:

```bash
taccuino transcribe riunione.mp3 --language it
taccuino notes riunione.mp3                 # transcript + note in console
taccuino bench riunione.mp3                 # velocità ASR/LLM sul profilo attivo
taccuino config profile npu                 # cambia profilo
taccuino config language it                 # forza lingua (default: automatica)
taccuino config overrides '{"llm_model":"OpenVINO/gemma-4-E4B-it-int4-ov"}'
```

## Profili hardware

| Profilo   | ASR                              | LLM                         | Per chi |
|-----------|----------------------------------|-----------------------------|---------|
| `xe`      | whisper-large-v3-turbo int8 · GPU | Qwen3-4B int4 · GPU          | Iris Xe (Tiger Lake, questo PC), Arc |
| `npu`     | whisper-large-v3-turbo int8 · NPU | Qwen3-8B int4-cw · NPU       | Core Ultra, GPU libera |
| `npu+gpu` | whisper-large-v3-turbo int8 · NPU | Qwen3-8B int4 · GPU          | Lunar/Arrow Lake: probabilmente il migliore |
| `cpu`     | whisper-small int8 · CPU          | Qwen3-1.7B int4 · CPU        | fallback |

## Ricerca modelli (novembre 2026)

### Speech-to-text IT + EN

| Modello | Note | Verdetto |
|---|---|---|
| **Whisper large-v3-turbo** | 809M, 4 layer di decoder → ~5–8× più veloce di large-v3 con WER vicino. Ottimo italiano e code-switching IT/EN. Pre-convertito da Intel (`OpenVINO/whisper-large-v3-turbo-int8-ov`), supportato su CPU/GPU/NPU con timestamp per parola. | **Scelto** |
| Whisper large-v3 | Un po' più preciso, molto più lento su iGPU. | Opzione qualità su Arc |
| Parakeet TDT 0.6B v3 (NVIDIA) | Ottimo italiano (FLEURS ~3% WER dichiarato), velocissimo, ma niente conversione OpenVINO ufficiale (serve export ONNX a mano). | Candidato futuro |
| Cohere Transcribe (03-2026), Canary-Qwen 2.5B, Granite Speech | Top dell'Open ASR Leaderboard in inglese; supporto OpenVINO/iGPU assente o non ufficiale, e più pesanti. | No per ora |
| Qwen3-ASR | Supportato dal nuovo ASRPipeline di OpenVINO 2026.3+. | Da valutare |

### LLM per note/riassunti IT + EN (deve stare in ~3 GB su iGPU)

| Modello | Note | Verdetto |
|---|---|---|
| **Qwen3-4B int4** | Tra i migliori small model in italiano (MMLU-IT ~0.62 contro ~0.52 di Gemma 3 4B in una valutazione CLiC-it 2025), contesto 32k, `/no_think` per risposte dirette. | **Scelto per Xe** |
| Qwen3.5-4B int4 | Più recente, migliore nelle classifiche generali; pre-convertito (`OpenVINO/Qwen3.5-4B-int4-ov`). | Alternativa da provare via `overrides` |
| Gemma 4 E4B / E2B int4 | Buon italiano, il più documentato su NPU (~17–18 tok/s su Lunar Lake). | Alternativa NPU (`gemma-4-E2B-it-int4-cw-ov`) |
| Qwen3-8B int4(-cw) | Qualità superiore, ~5 GB: lento su Iris Xe, ok su NPU/Arc. | **Scelto per NPU** |
| Phi-4-mini | Buono in inglese, italiano meno naturale. | No |

### NPU (PC di lavoro)

- Serve un **Core Ultra** (Meteor Lake, Lunar Lake, Arrow Lake) con driver
  [intel/linux-npu-driver](https://github.com/intel/linux-npu-driver) (Linux) o driver NPU ≥ 32.0.100.5540
  (Windows). Verifica con `taccuino devices`.
- **Whisper su NPU funziona bene** ed è il vantaggio principale: la trascrizione live gira a basso consumo
  e lascia libera la GPU. Usare OpenVINO ≥ 2026.4 (in 2026.3 c'era un bug di stato riutilizzato tra
  inferenze, issue openvino#37937).
- **LLM su NPU**: solo modelli int4 simmetrici channel-wise (suffisso `-cw-`); la NPU compila il grafo
  con una lunghezza di prompt fissa (`MAX_PROMPT_LEN`, qui 4096): per questo il profilo `npu` usa chunk più
  piccoli e map-reduce. La prima compilazione è lenta, poi viene messa in cache (`npu-cache/`).
- Su Lunar Lake la GPU Arc 140V è spesso più veloce della NPU per l'LLM → profilo `npu+gpu`.
- Su Windows lo stesso codice funziona sostituendo la cattura `-f pulse` di ffmpeg con `-f dshow`/WASAPI
  (non ancora implementato).

## Limiti noti

- Con altoparlanti il microfono riprende anche gli altri → righe duplicate "Io/Altri". Usa le cuffie.
- Il VAD è a energia: in ambienti molto rumorosi alza `threshold` in `audio.py`.
- La UI ascolta solo su `127.0.0.1`.

## Fonti

- [OpenVINO 2026 release notes](https://docs.openvino.ai/2026/about-openvino/release-notes-openvino.html)
- [OpenVINO 2026.4: ASR pipeline](https://medium.com/openvino-toolkit/openvino-2026-4-broader-model-support-faster-inference-and-node-js-speech-recognition-393ea73ebcff)
- [Whisper su NPU, bug di stato (#37937)](https://github.com/openvinotoolkit/openvino/issues/37937)
- [Gemma 4 su NPU Lunar Lake](https://zenn.dev/jkudo/articles/ae85d7d099e672?locale=en)
- [Valutazione small LM italiano/inglese (CLiC-it 2025)](https://aclanthology.org/2025.clicit-1.101.pdf)
- [Best local STT 2026](https://openvoxai.com/blog/best-local-stt-transcription-models-2026) ·
  [Northflank STT 2026](https://northflank.com/blog/best-open-source-speech-to-text-stt-model-in-2026-benchmarks) ·
  [Cohere Transcribe](https://huggingface.co/blog/CohereLabs/cohere-transcribe-03-2026-release)
