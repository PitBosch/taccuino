# Riferimento

## Etichette degli speaker

- **Io**: lo speaker con più parlato sul canale microfono. **In sala N**: altre voci sul microfono (riunioni dal vivo).
- **Speaker N**: partecipanti remoti (canale di sistema). Claude prova a dedurre i nomi dal contesto ("Marco (Speaker 2)").
- Durante la call le righe live sono **Io** (microfono) e, per il canale di sistema, **Call A**, **Call B**, … (voci
  distinte, vedi sotto) oppure **Call** se la voce non si riconosce o la funzione è spenta. Niente diarization completa.

### Voci della call nel live

Con `live_speakers` (default on) ogni riga "Call" fissata riceve un embedding vocale (wespeaker con sherpa-onnx, CPU,
1 thread, al massimo i primi 3 s di parlato) confrontato con le voci già sentite: similarità coseno ≥
`speaker_threshold` → stessa voce, altrimenti voce nuova (max 8). Le righe con meno di 1 s di parlato non creano voci
(restano "Call" se non somigliano a nessuna). Lo stato è in `live.speakers.json`, così le etichette restano le stesse
se il worker riparte. Quando Haiku corregge il testo (stessa chiamata, nessun costo in più) propone un nome per una
voce solo se il testo lo rende chiaro (si presenta, viene chiamata per nome): `live.names.json`. Console, sottotitoli,
appunti e nota finale mostrano il nome se c'è, altrimenti "Call A".

- Costo misurato (i5-1145G7): ~85 ms di CPU per riga (≤ 3 s di parlato), ~0,2 s per caricare il modello alla prima
  riga Call, ~100 MB di RAM in più nel live worker. Il modello serve già scaricato (`taccuino download` o
  `taccuino setup --download --with-diarization`); se manca, o manca sherpa-onnx, le righe restano "Call".
- **Calibrare `speaker_threshold`** (0,55 è un valore iniziale scelto su audio di prova, non su call vere con più
  persone): se la stessa persona finisce divisa in più voci, abbassala (es. 0,5); se persone diverse finiscono nella
  stessa voce, alzala (es. 0,6). Le voci già create in una riunione restano.
- Con gli altoparlanti le righe duplicate dall'eco vengono scartate per similarità. Le cuffie restano meglio.

## Durante la call

Il daemon lancia per tutta la call un **live worker** (processo separato, con Whisper caricato solo in quel momento):

- trascrive in streaming il FLAC che si sta scrivendo; ogni `live_clean_seconds` (15 min, in una pausa) Haiku corregge
  in blocco le frasi accumulate (i "Grazie." inventati li toglie già un filtro deterministico);
- ogni `live_notes_minutes` (15) Haiku aggiunge **appunti mirati** (decisioni, azioni, numeri, date) alla nota, già
  nella sottocartella che sceglie alla prima passata guardando l'archivio (può aprire qualche documento) e di cui
  scrive il motivo nella nota; `exclude_folders` esclude le cartelle di servizio;
- a fine call Haiku scrive la nota finale **dalla trascrizione live** (pochi secondi, niente GPU), col titolo finale
  e la sezione "Appunti di Claude durante la riunione". Per avere anche gli speaker separati (diarization + secondo
  passaggio di Whisper): `taccuino config final_transcribe true`.

`taccuino config live_transcribe false` spegne tutto questo. Console e sottotitoli leggono i file del worker.

## Call di fila nella stessa registrazione

Se tra due call il microfono non viene mai rilasciato, la registrazione è una sola. Tre modi per separarle:

- **Automatico** (`auto_split`, default on): a fine registrazione, se dura più di `split_min_minutes` (40), Haiku
  legge il transcript e, solo se è sicuro, indica dove comincia ogni riunione successiva. Ogni parte diventa una nota
  con la sua cartella e il suo titolo; la nota riporta il motivo del taglio. Il silenzio non è un segnale affidabile
  (nella registrazione reale usata per progettarlo la pausa più lunga era 25 s, dentro la stessa call).
- **Dal vivo**: `/split [titolo]` in console, o `taccuino split`: chiude la riunione e ne apre subito un'altra.
- **A posteriori**: `taccuino split <cartella> [MM:SS]` taglia dove dici tu (senza orario decide Haiku).
  Le cartelle stanno in `meetings/`; l'audio si conserva `keep_audio_days` giorni.

## Il segretario (appunti e Jira)

`n testo` (o `taccuino note "testo"`) non salva solo l'appunto: Haiku lo interpreta con il contesto della call e lo mette
nella nota, sezione "I miei appunti". Se l'appunto è un'azione prepara un task Jira: **chiesto esplicitamente**
("n crea task: …") lo crea subito, **altrimenti** lo propone (`jira`, `jira ok 2` / `jira ok tutte`, `jira no 1`).
Jira passa dal **connettore Atlassian già collegato a Claude Code** (nessun token in Taccuino; verifica con
`claude mcp list`). Opzioni: `jira_project`, `jira_issue_type`.

## Vocabolari

Due fonti, usate da Whisper (prompt corto), dalla correzione di Haiku, dal segretario e dalle note finali:

1. **Dal vault Obsidian** (globale): `taccuino vocabolario` fa compilare a Haiku persone, progetti, aziende, sigle e
   gergo dal vault, trovato da solo (o `vault_path`; se le note stanno già in un vault, è quello). In locale si
   estraggono solo i *candidati* (titoli, link, alias, tag, nomi propri, sigle): il testo delle note non viene inviato.
   Il daemon lo rigenera ogni `vocab_refresh_days` (7; 0 = mai). `taccuino vocabolario --show` lo mostra.
2. **Per progetto** (nella KB): un file `Vocabolario.md` in una cartella dell'archivio, una voce per riga:

   ```
   - Quasar — sistema di acquisizione dati (trascritto anche "Quasi")
   - Orione — nome in codice del progetto
   ```

   Vale per le riunioni archiviate in quella cartella e sotto; finché la cartella non è scelta valgono i termini di
   tutti i progetti, un termine a testa per file. A Whisper arrivano solo i termini (max ~450 caratteri, i primi contano
   di più); Haiku legge anche le note, compresi gli errori tipici. **Questi file contengono dati dei clienti: restano
   nella KB, mai nel repo.**

I termini in `taccuino config vocabulary "…"` si aggiungono in testa.

## Console

`taccuino` apre un'interfaccia in stile Claude Code sulla riunione in corso (o sull'ultima).

| comando | cosa fa |
|---|---|
| `r [min]` · `/riassumi` | riassunto degli ultimi N minuti (default 5) |
| `t [min]` · `/trascrizione` | trascrizione degli ultimi N minuti |
| `n testo` · `/nota` | appunto con timestamp |
| `/start [titolo]` · `/stop` | registrazione manuale |
| `/split [titolo]` | call di fila: chiude questa riunione e ne apre un'altra |
| `/live` · `/stato` · `/help` · `/esci` | |
| qualsiasi frase | domanda a Claude Haiku sulla riunione |

`/` mostra il menu (Tab completa, ↑↓ sceglie); Esc annulla una risposta in arrivo; Ctrl-D esce. Senza terminale o con
`taccuino console --plain` parte la console semplice. Anche da script: `taccuino riassumi 5`, `taccuino chiedi "…"`.

## Comandi

```
taccuino doctor                    controlla i requisiti (sola lettura)
taccuino setup [--yes] [--model turbo-int8|turbo-int4|small-int8|small-int4|base-int8] [--profile xe|npu|cpu]
               [--output-dir DIR] [--download] [--with-diarization] [--service]
taccuino status | start [titolo] | stop | split [DIR [MM:SS]] | note "testo" | list
taccuino process FILE|DIR          elabora un file audio/video o una cartella riunione
taccuino summarize DIR             rigenera solo le note di Claude
taccuino install-service | uninstall-service | quit
taccuino config [CHIAVE [VALORE]]  mostra o cambia le impostazioni
```

## Impostazioni

`taccuino config` le mostra tutte, `taccuino config KEY VALUE` le modifica (il daemon le ricarica).

| chiave | default | note |
|---|---|---|
| `profile` | `xe` | `xe` (GPU), `npu`, `cpu` |
| `asr_model` | — | sostituisce il modello del profilo (repo OpenVINO su Hugging Face) |
| `language` | `it` | `en`, oppure `auto` (sconsigliato: Whisper allucina in inglese sui silenzi) |
| `output_dir` | `Documenti/Taccuino` | dove vanno le note (un vault Obsidian va bene) |
| `exclude_folders` | `[]` | sottocartelle dove Claude non archivia mai |
| `vocabulary`, `vault_path`, `vocab_refresh_days` | — / auto / 7 | vedi Vocabolari |
| `claude_extra_prompt` | — | istruzioni extra per le note |
| `ignore_apps` | ffmpeg, obs… | app che aprono il microfono senza essere riunioni |
| `start_delay` / `stop_delay` | 5 s / 20 s | isteresi del rilevamento |
| `min_meeting_seconds` | 60 | sotto questa durata la registrazione viene scartata |
| `auto_split` / `split_min_minutes` | true / 40 | divisione automatica di call di fila |
| `final_transcribe` | false | true = a fine call ritrascrive tutto con diarization (più lento) |
| `num_speakers` / `cluster_threshold` | -1 / 0.6 | solo con `final_transcribe` |
| `live_clean_seconds` / `live_notes_minutes` | 900 / 15 | correzione e appunti durante la call (0 = solo a fine call / mai) |
| `live_speakers` / `speaker_threshold` | true / 0.55 | voci distinte "Call A/B…" nel live; soglia di similarità (più alta = più voci) |
| `keep_audio_days` | 14 | 0 = cancella l'audio subito dopo l'elaborazione |

## Prestazioni misurate (i5-1145G7, Iris Xe 80 EU, 16 GB)

| passo | device | velocità |
|---|---|---|
| Whisper large-v3-turbo int8 | GPU Iris Xe | RTF ~0,2–0,28 (2 min di audio in 24–33 s) |
| Diarization sherpa-onnx | CPU, 4 thread | RTF ~0,075 |
| Note con Claude Haiku | cloud | ~15 s |

## Scelta dei modelli

Modelli che girano con `openvino_genai` 2026.4 (repo `OpenVINO/…` su Hugging Face; `taccuino setup --model` ne sceglie uno):

| modello | note |
|---|---|
| **whisper-large-v3-turbo-int8** | default: ottimo rapporto qualità/velocità su iGPU; ~830 MB su disco, ~1–2 GB di RAM caricato (misurato) |
| whisper-large-v3-turbo-int4 | più leggero: **da verificare** (RAM e qualità non misurate) |
| whisper-small-int8 / int4, base-int8 | per macchine con poca RAM: qualità inferiore, non misurata qui |
| Qwen3-ASR 1.7B / 0.6B int8 (conversioni community) | `asr_model circulus/Qwen3-ASR-1.7B-int8-ov`; più lento, storpia di più i nomi propri |
| distil-whisper large-v3 | **solo inglese**: escluso |

Benchmark su Iris Xe (clip italiana di 2 min, `scripts/bench_asr.py`): Whisper turbo int8 RTF 0,13; Qwen3-ASR 1.7B int8
RTF 0,45. WER su 120 frasi FLEURS italiane: Whisper turbo 2,91%, Qwen3-ASR 1.7B 3,43%.

**Diarization**: sherpa-onnx (~35 MB, offline, CPU). Serve con `final_transcribe true` (il solo modello di embedding
anche per le voci della call nel live); si scarica con
`taccuino download` (insieme a Whisper) o `taccuino setup --download --with-diarization`.

**NPU**: `taccuino config profile npu` fa girare Whisper sulla NPU (serve OpenVINO ≥ 2026.4 e il driver Intel NPU).
Su Core Ultra 7 155H abbiamo osservato `DEVICE_LOST` in call lunghe: non è il profilo consigliato.

## Dati

- `<dati>/meetings/<data>/`: `audio.flac` (L=mic, R=sistema), `live.jsonl` (righe live), `live.clean.json` (correzioni),
  `live.speakers.json` (voci della call), `live.names.json` (nomi delle voci),
  `transcript.json`, `notes.txt`, `meta.json` (titolo, cartella e motivo, app, divisione).
  `<dati>` = `%LOCALAPPDATA%\taccuino` su Windows, `~/.local/share/taccuino` su Linux (o `TACCUINO_HOME`).
- `<output_dir>/…/*.md`: note finali.
- Al cloud va solo il testo del transcript, inviato a Claude tramite Claude Code. L'audio resta sul PC.
