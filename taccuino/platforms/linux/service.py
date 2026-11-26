"""Linux: servizio systemd utente che avvia il daemon."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

UNIT = Path.home() / ".config/systemd/user/taccuino.service"

SERVICE = """[Unit]
Description=Taccuino - note di riunione locali
After=pipewire.service pipewire-pulse.service
Wants=pipewire-pulse.service

[Service]
ExecStart={exe} daemon
Restart=on-failure
RestartSec=5
Environment=PATH={path}
Nice=5

[Install]
WantedBy=default.target
"""


def install_service() -> None:
    from ...config import load_settings  # import qui: config importa platforms
    s = load_settings()
    exe = shutil.which("taccuino") or str(Path(sys.argv[0]).resolve())
    UNIT.parent.mkdir(parents=True, exist_ok=True)
    path = ":".join(dict.fromkeys([str(Path(exe).parent), str(Path(s.claude_bin).parent),
                                   str(Path.home() / ".local/bin"), "/usr/local/bin", "/usr/bin", "/bin"]))
    UNIT.write_text(SERVICE.format(exe=exe, path=path))
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "taccuino.service"], check=True)
    print(f"Installato {UNIT}\nLog: journalctl --user -u taccuino -f")


def uninstall_service() -> None:
    subprocess.run(["systemctl", "--user", "disable", "--now", "taccuino.service"])
    UNIT.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"])
    print("Servizio rimosso")
