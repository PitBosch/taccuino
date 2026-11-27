"""Tutto ciò che dipende dal sistema operativo sta qui, una cartella per OS (`linux/`, `windows/`).

Il resto di Taccuino importa solo da questo modulo. Ogni cartella espone la stessa interfaccia:

  IS_WIN, NO_WINDOW                    flag e argomenti per subprocess
  try_lock(f), command(exe, *args)     lock di file non bloccante, avvio di .cmd/.bat
  data_dir(), documents_roots()        dove stanno i dati e dove cercare "Documenti"
  claude_exe_name                      nome dell'eseguibile di Claude Code
  total_ram_gb()                       RAM installata (serve a `taccuino setup` per scegliere il modello)
  Recorder                             cattura microfono + audio di sistema in un FLAC stereo 16 kHz (L=mic, R=sistema)
  list_audio_devices()                 testo con le sorgenti audio (per `taccuino devices`)
  mic_users(ignore_apps, ignore_pids)  app che stanno usando il microfono
  install_service() / uninstall_service()  servizio all'accesso (systemd utente / Utilità di pianificazione)
  ffmpeg_hint                          come installare ffmpeg
  native_lib_name, build_hint          nome della libreria Parakeet e come installare git/cmake/compilatore

Per aggiungere un sistema operativo basta una nuova cartella con questa interfaccia.
"""
import sys

if sys.platform == "win32":
    from .windows import *  # noqa: F401,F403
else:
    from .linux import *  # noqa: F401,F403
