"""Implementazione Windows dell'interfaccia di `taccuino.platforms`."""
from .audio import Recorder, list_devices as list_audio_devices  # noqa: F401
from .detect import mic_users  # noqa: F401
from .service import install_service, uninstall_service  # noqa: F401
from .system import (IS_WIN, NO_WINDOW, claude_exe_name, command, data_dir, documents_roots,  # noqa: F401
                     ffmpeg_hint, total_ram_gb, try_lock)
