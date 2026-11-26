"""Windows: attività dell'Utilità di pianificazione che avvia il daemon (senza finestra) all'accesso."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TASK = "Taccuino"


def install_service() -> None:
    from ...config import DATA_DIR  # import qui: config importa platforms
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    pyw = pyw if pyw.exists() else exe
    tr = f'"{pyw}" -X utf8 -m taccuino.cli daemon'
    r = subprocess.run(["schtasks", "/Create", "/SC", "ONLOGON", "/TN", TASK, "/TR", tr, "/RL", "LIMITED", "/F"],
                       capture_output=True, text=True)
    if r.returncode:  # senza admin schtasks /SC ONLOGON è negato: Register-ScheduledTask per il solo utente corrente funziona
        ps = (f"$a=New-ScheduledTaskAction -Execute '{pyw}' -Argument '-X utf8 -m taccuino.cli daemon';"
              "$t=New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME;"
              "$s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
              "-ExecutionTimeLimit ([TimeSpan]::Zero);"
              f"Register-ScheduledTask -TaskName '{TASK}' -Action $a -Trigger $t -Settings $s "
              "-User $env:USERNAME -RunLevel Limited -Force -ErrorAction Stop | Out-Null")
        r2 = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
        if r2.returncode:
            sys.exit("schtasks: " + (r.stderr or r.stdout).strip() + "\n" + r2.stderr.strip())
    subprocess.run(["schtasks", "/Run", "/TN", TASK], capture_output=True)
    print(f"Attività \"{TASK}\" creata: parte a ogni accesso.\nLog: {DATA_DIR / 'daemon.log'}")


def uninstall_service() -> None:
    subprocess.run(["schtasks", "/End", "/TN", TASK], capture_output=True)
    r = subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"], capture_output=True, text=True)
    print("Attività rimossa" if r.returncode == 0 else (r.stderr or r.stdout).strip())
