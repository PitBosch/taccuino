# Taccuino su Windows

Testato su Windows 11 con Intel Core Ultra 7 155H (Arc iGPU + NPU), 15,5 GB di RAM: servizio all'accesso, cattura
microfono + audio di sistema, trascrizione live su GPU, note e titoli con Haiku, scelta della cartella.

## Come funziona qui

- **Audio**: microfono + loopback dell'uscita predefinita via WASAPI (libreria `soundcard`). Stesso FLAC di Linux:
  L = microfono, R = audio di sistema. Se microfono o uscita predefiniti cambiano durante la call, la cattura li
  riapre da sola entro ~5 s.
- **Rilevamento riunioni**: dal registro "privacy microfono" (le app che vedi in Impostazioni → Privacy → Microfono
  mentre lo usano: Teams, Chrome, Zoom…).
- **Servizio**: attività dell'Utilità di pianificazione che avvia il daemon all'accesso, senza finestre. Funziona
  anche senza amministratore (ripiega su `Register-ScheduledTask` per il solo utente).
- **Dati**: `%LOCALAPPDATA%\taccuino` (modelli, riunioni, `config.json`, `daemon.log`). Note in `Documenti\Taccuino`
  (anche se Documenti è su OneDrive).

Codice: `taccuino/platforms/windows/`.

## Installazione (PowerShell)

```powershell
winget install Python.Python.3.12        # o da python.org, con "tcl/tk" (serve ai sottotitoli)
winget install Gyan.FFmpeg               # ffmpeg nel PATH; riapri il terminale dopo
setx PYTHONUTF8 1                        # una volta sola: file e note in UTF-8 (accenti!)
# riapri il terminale, poi:
git clone https://github.com/PitBosch/taccuino; cd taccuino
py -3.12 -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\taccuino doctor            # tutto ✓ tranne modello e daemon (si sistemano al passo dopo)
.venv\Scripts\taccuino setup --download --service
.venv\Scripts\taccuino status
```

Requisiti: driver grafici Intel aggiornati (GPU), driver NPU Intel se vuoi l'NPU, Claude Code installato e
autenticato (`claude -p "ciao"` deve rispondere).

Per avere `taccuino` da qualsiasi PowerShell, aggiungi al profilo (PowerShell 7: `$PROFILE`; Windows PowerShell 5.1 ha
un profilo diverso, `Documents\WindowsPowerShell\Microsoft.PowerShell_profile.ps1`):

```powershell
function taccuino { $env:PYTHONUTF8 = 1; & "C:\percorso\del\repo\.venv\Scripts\taccuino.exe" @args }
```

## Prova

1. `taccuino start "prova"`, parla 1-2 minuti, `taccuino stop` → dopo qualche minuto la nota in `Documenti\Taccuino`.
2. Apri una call Teams: entro ~5 s `taccuino status` deve dire "registrazione in corso (MSTeams)".
3. Durante la call: `taccuino` (console) o `taccuino sottotitoli` (finestrina).
4. Log: `%LOCALAPPDATA%\taccuino\daemon.log`. Fermare: `taccuino quit`. Togliere il servizio: `taccuino uninstall-service`.

## Limiti e cose da sapere

- **NPU**: con il profilo `npu` abbiamo visto `ZE_RESULT_ERROR_DEVICE_LOST` due volte durante call Teams (causa non
  confermata). Il profilo consigliato è la GPU (`xe`); su errore del device Whisper si ricarica sullo stesso device
  e riprova (2 volte). `taccuino setup` non sceglie mai l'NPU da solo.
- **RAM**: con Whisper large-v3-turbo int8 caricato il processo del live worker occupa circa 1-2 GB. Su macchine più
  strette `taccuino setup` propone `turbo-int4` o `small-int8` (soglie euristiche, non misurate: prova con
  `scripts/bench_asr.py`). Un solo modello è in RAM alla volta.
- **Teams con dispositivo diverso dal predefinito di Windows**: l'audio di sistema non viene registrato. Allinea il
  dispositivo di Teams o imposta `system_source`.
- **PowerShell 5.1 e 7** hanno profili separati. `schtasks /SC ONLOGON` richiede amministratore (c'è il ripiego).
- La console semplice (`--plain`) non ha la cronologia dei comandi; la TUI sì.
- Mai usare `os.kill(pid, 0)` su Windows per vedere se un processo vive: termina il processo.

## Se qualcosa non va

`taccuino doctor` dice cosa manca. Poi: `taccuino status`, `%LOCALAPPDATA%\taccuino\daemon.log`, e per un worker
live fermo `meetings\<cartella>\live.driver` (ultimo battito) e `live.jsonl` (righe trascritte).
