"""Stato del daemon letto con tolleranza; cartelle dell'archivio proposte a Haiku."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from taccuino import cli
from taccuino.summarize import existing_folders


class State(unittest.TestCase):
    def test_file_incompleto_poi_completo(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            p.write_text('{"pid": 1, "upd')  # il daemon sta riscrivendo
            good = json.dumps({"pid": 1, "updated": time.time()})
            calls = []

            def sleep(t):
                calls.append(t)
                p.write_text(good)  # alla prima attesa il file è completo

            with mock.patch.object(cli, "STATE_PATH", p), mock.patch.object(cli.time, "sleep", sleep):
                self.assertEqual(cli._state()["pid"], 1)
            self.assertEqual(len(calls), 1)

    def test_assente_o_vecchio(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            with mock.patch.object(cli, "STATE_PATH", p):
                self.assertIsNone(cli._state())
                p.write_text(json.dumps({"updated": time.time() - 60}))
                self.assertIsNone(cli._state())


class Folders(unittest.TestCase):
    def test_nascoste_ed_escluse_con_sottoalbero(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for sub in ("Clienti/Alfa", ".obsidian/x", "TaskNotes/y", "Personale"):
                (root / sub).mkdir(parents=True)
            self.assertEqual(existing_folders(root, ["tasknotes"]), ["Clienti", "Personale", "Clienti/Alfa"])


if __name__ == "__main__":
    unittest.main()
