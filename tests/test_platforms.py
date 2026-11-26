"""Le due cartelle di piattaforma espongono la stessa interfaccia; il rilevamento del microfono funziona su entrambe."""
import sys
import types
import unittest

sys.modules.setdefault("fcntl", types.ModuleType("fcntl"))  # su Windows manca: serve solo ad importare il modulo Linux

from taccuino.platforms import linux, windows  # noqa: E402
from taccuino.platforms.linux.detect import users_from_objects  # noqa: E402
from taccuino.platforms.windows.detect import _app_name, users_from_entries  # noqa: E402

INTERFACE = {"IS_WIN", "NO_WINDOW", "try_lock", "command", "data_dir", "documents_roots", "claude_exe_name",
             "total_ram_gb", "Recorder", "list_audio_devices", "mic_users", "install_service", "uninstall_service",
             "ffmpeg_hint"}


class Interface(unittest.TestCase):
    def test_stessa_interfaccia(self):
        for mod in (linux, windows):
            self.assertEqual(INTERFACE - set(dir(mod)), set(), mod.__name__)

    def test_command_cmd_solo_su_windows(self):
        self.assertEqual(windows.command("claude.cmd", "-p"), ["cmd.exe", "/c", "claude.cmd", "-p"])
        self.assertEqual(linux.command("claude.cmd", "-p"), ["claude.cmd", "-p"])


class WindowsDetect(unittest.TestCase):
    def test_solo_app_in_uso_e_non_ignorate(self):
        entries = [("MSTeams_8wekyb3d8bbwe", False, 5, 0),                                # in uso
                   ("C:#Program Files#Google#Chrome#Application#chrome.exe", True, 5, 9),  # finito
                   ("C:#tools#obs.exe", True, 5, 0)]                                      # in uso ma ignorata
        users = users_from_entries(entries, ["obs"], own_exes=set())
        self.assertEqual([u.app for u in users], ["MSTeams"])

    def test_nome_app(self):
        self.assertEqual(_app_name("MSTeams_8wekyb3d8bbwe", False), ("MSTeams", "MSTeams"))
        self.assertEqual(_app_name("C:#x#chrome.exe", True), ("chrome", "chrome.exe"))

    def test_il_nostro_python_non_conta(self):
        entries = [("C:#py#pythonw.exe", True, 5, 0)]
        self.assertEqual(users_from_entries(entries, [], own_exes={"c:\\py\\pythonw.exe"}), [])


def node(i, **props):
    return {"id": i, "info": {"props": {"media.class": "Stream/Input/Audio", **props}}}


class LinuxDetect(unittest.TestCase):
    def test_stream_di_input(self):
        objs = [node(1, **{"application.name": "Zoom", "application.process.binary": "zoom",
                           "application.process.id": "42"}),
                node(2, **{"application.name": "Monitor", "stream.capture.sink": "true"}),   # audio di sistema
                node(3, **{"application.name": "taccuino"}),                                   # noi
                node(4, **{"application.name": "obs", "application.process.binary": "obs"}),   # ignorata
                {"id": 5, "info": {"props": {"media.class": "Audio/Sink"}}}]                   # non è un input
        users = users_from_objects(objs, ["obs"])
        self.assertEqual([(u.app, u.pid) for u in users], [("Zoom", 42)])


if __name__ == "__main__":
    unittest.main()
