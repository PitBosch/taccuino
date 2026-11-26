"""Divisione di una registrazione con più riunioni: taglio dei file e lettura della risposta di Haiku."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from taccuino import split
from taccuino.config import Settings
from taccuino.pipeline import Line


def meeting(d: Path) -> Path:
    m = d / "2026-01-01_100000"
    m.mkdir(parents=True)
    rows = [{"start": float(i * 10), "end": float(i * 10 + 8), "speaker": "Call", "text": f"riga {i}"} for i in range(10)]
    (m / "live.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (m / "live.clean.json").write_text(json.dumps({str(i): f"corretta {i}" for i in range(10)}), encoding="utf-8")
    (m / "live_notes.md").write_text("appunti")
    (m / "meta.json").write_text(json.dumps({"started": 1_700_000_000.0, "manual": False, "title": "T",
                                             "apps": ["Teams"], "folder": "A", "output": "x.md"}))
    return m


class SplitMeeting(unittest.TestCase):
    def test_taglio_righe_correzioni_e_meta(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(split, "MEETINGS_DIR", Path(d)):
            m = meeting(Path(d))
            new = split.split_meeting(m, 50.0, "cambio call")
            a = [json.loads(x) for x in (m / "live.jsonl").read_text().splitlines()]
            b = [json.loads(x) for x in (new / "live.jsonl").read_text().splitlines()]
            self.assertEqual((len(a), len(b)), (5, 5))
            self.assertEqual((b[0]["start"], b[0]["text"]), (0.0, "riga 5"))  # tempi ricalcolati dal taglio
            cb = json.loads((new / "live.clean.json").read_text())
            self.assertEqual(cb["0"], "corretta 5")  # indici riallineati
            self.assertEqual(len(json.loads((m / "live.clean.json").read_text())), 5)
            meta = json.loads((new / "meta.json").read_text())
            self.assertEqual((meta["started"], meta["title"], meta["split_reason"]), (1_700_000_050.0, "", "cambio call"))
            self.assertNotIn("folder", meta)  # la cartella della seconda parte si sceglie da capo
            self.assertFalse((m / "live_notes.md").exists())  # gli appunti live coprivano tutto: si rigenerano

    def test_taglio_fuori_dalla_registrazione(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(split, "MEETINGS_DIR", Path(d)):
            m = meeting(Path(d))
            for at in (0.0, 5000.0):
                with self.assertRaises(ValueError):
                    split.split_meeting(m, at)

    def test_parse_ts(self):
        self.assertEqual(split.parse_ts("36:21"), 2181.0)
        self.assertEqual(split.parse_ts("1:02:03"), 3723.0)
        self.assertEqual(split.parse_ts("90"), 90.0)


class FindSplits(unittest.TestCase):
    lines = [Line(float(i * 30), float(i * 30 + 20), "Call", f"testo {i}") for i in range(20)]

    def ask(self, answers):
        it = iter(answers)
        return mock.patch("taccuino.summarize.run_claude", side_effect=lambda *a, **k: next(it))

    def test_una_sola_riunione(self):
        with self.ask(["NESSUNA"]):
            self.assertEqual(split.find_splits(Settings(), self.lines), [])

    def test_riga_esatta_dal_secondo_passaggio(self):
        # blocco 4 (240 s): finestra da 180 s a 360 s → righe 6..11; la riga 2 della finestra è quella da 240 s
        with self.ask(["INIZIO: 4 | saluti e nuovo argomento", "RIGA: 2"]):
            cuts = split.find_splits(Settings(), self.lines)
        self.assertEqual(cuts, [(240.0, "saluti e nuovo argomento")])

    def test_risposta_illeggibile_ripiega_sul_blocco(self):
        with self.ask(["INIZIO: 4 | motivo", "boh"]):
            self.assertEqual(split.find_splits(Settings(), self.lines)[0][0], 240.0)


if __name__ == "__main__":
    unittest.main()
