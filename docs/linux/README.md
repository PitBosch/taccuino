# Taccuino su Linux

> **Stato**: il codice Linux è quello della prima versione di Taccuino (sviluppata e usata su Linux con Intel Iris Xe),
> spostato in `taccuino/platforms/linux/` senza cambiarne il comportamento. Dopo la ristrutturazione **non è stato
> riprovato su una macchina Linux reale**: i test automatici coprono l'interfaccia e il rilevamento del microfono,
> non la cattura audio né systemd. Se qualcosa non va, segnalalo.

## Come funziona qui

- **Audio**: ffmpeg legge due sorgenti PipeWire/PulseAudio (microfono e monitor dell'uscita) e scrive un FLAC stereo
  16 kHz: L = microfono, R = audio di sistema.
- **Rilevamento riunioni**: PipeWire (`pw-dump`): stream di input audio che non siano il monitor né Taccuino stesso.
- **Servizio**: unità systemd utente (`~/.config/systemd/user/taccuino.service`).
- **Dati**: `~/.local/share/taccuino` (modelli, riunioni, `config.json`). Note in `~/Documents/Taccuino`.

Codice: `taccuino/platforms/linux/`.

## Installazione

```bash
git clone https://github.com/PitBosch/taccuino && cd taccuino
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/taccuino doctor                       # deve comparire GPU (e NPU sui Core Ultra)
.venv/bin/taccuino setup --download --service   # profilo e modello per la tua macchina, Whisper, servizio systemd
journalctl --user -u taccuino -f                # log
```

Requisiti: PipeWire con pipewire-pulse, ffmpeg, Claude Code autenticato (`claude` nel PATH), driver Intel per la GPU
(se `taccuino devices` non la vede: `sudo usermod -aG render $USER` e rientra nella sessione). Per l'NPU:
[intel/linux-npu-driver](https://github.com/intel/linux-npu-driver) e OpenVINO ≥ 2026.4.

## Prova

1. `taccuino start "prova"`, parla 1-2 minuti, `taccuino stop` → dopo qualche minuto la nota in `~/Documents/Taccuino`.
2. Apri una call: entro ~5 s `taccuino status` deve dire "registrazione in corso".
3. Fermare: `taccuino quit`. Togliere il servizio: `taccuino uninstall-service`.

## Limiti e cose da sapere

- **NPU**: sul Core Ultra 7 155H (Windows) l'NPU ha dato `DEVICE_LOST` in call lunghe; su Linux non misurato. Il
  profilo consigliato resta la GPU (`xe`).
- **RAM**: vedi `docs/windows/README.md`: stesso modello, stessi numeri (non misurati su Linux).
- Con gli altoparlanti il microfono riprende anche la call: le righe doppie vengono scartate per similarità.
  Le cuffie restano meglio.
