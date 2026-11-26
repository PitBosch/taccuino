"""Voci della call nel live: clustering online, persistenza, nomi da Haiku, retrocompatibilità, degradazione."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from taccuino import speakers
from taccuino.config import Settings
from taccuino.live import FRAME, SR, Cleaner, LiveTranscript, parse_names
from taccuino.pipeline import Line
from taccuino.speakers import CallSpeakers, Embedder, Voices

DIM = 256
RNG = np.random.default_rng(7)
BASES = [v / np.linalg.norm(v) for v in RNG.standard_normal((4, DIM))]


def sample(k: int, noise: float = 0.5) -> np.ndarray:
    """Embedding sintetico della "voce" k: direzione fissa + rumore (similarità col centro ~0.8)."""
    return BASES[k] + noise * RNG.standard_normal(DIM) / np.sqrt(DIM)


class FakeEmbedder:
    """Al posto di wespeaker: la "voce" è codificata nell'ampiezza del canale Call (0.1 → voce 0, 0.2 → 1, …)."""

    def __init__(self):
        self.calls = []

    def __call__(self, samples, sr):
        self.calls.append(len(samples))
        k = int(round(float(np.abs(samples).mean()) * 10)) - 1
        return sample(k)


class TestVoices(unittest.TestCase):
    def test_tre_voci_etichette_coerenti(self):
        v = Voices(None, threshold=0.5)
        seq = [0, 1, 0, 2, 1, 1, 0, 2, 2, 0, 1]
        labels = [v.assign(sample(k), 2.5) for k in seq]
        names = {k: labels[seq.index(k)] for k in set(seq)}
        self.assertEqual(sorted(names.values()), ["Call A", "Call B", "Call C"])
        self.assertEqual(labels, [names[k] for k in seq])

    def test_righe_corte_non_creano_voci(self):
        v = Voices(None, threshold=0.5)
        self.assertEqual(v.assign(sample(0), 0.7), "Call")  # nessuna voce ancora: resta "Call"
        self.assertEqual(v.sums, [])
        self.assertEqual(v.assign(sample(0), 2.0), "Call A")
        self.assertEqual(v.assign(sample(0), 0.7), "Call A")  # corta ma simile: va alla voce esistente
        self.assertEqual(v.assign(sample(1), 0.7), "Call")    # corta e diversa: niente voce nuova
        self.assertEqual(len(v.sums), 1)
        self.assertEqual(v.counts, [1])  # le righe corte non spostano il centroide

    def test_limite_voci(self):
        v = Voices(None, threshold=0.5, max_voices=2)
        self.assertEqual(v.assign(sample(0), 2), "Call A")
        self.assertEqual(v.assign(sample(1), 2), "Call B")
        self.assertIn(v.assign(sample(2), 2), ("Call A", "Call B"))
        self.assertEqual(len(v.sums), 2)

    def test_persistenza(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / speakers.FILE
            v = Voices(p, threshold=0.5)
            self.assertEqual([v.assign(sample(k), 2) for k in (0, 1)], ["Call A", "Call B"])
            json.loads(p.read_text(encoding="utf-8"))  # json valido
            # un altro processo (es. il worker ripartito, o la console che fa da driver) riprende lo stato
            w = Voices(p, threshold=0.5)
            self.assertEqual([w.assign(sample(k), 2) for k in (1, 0, 2)], ["Call B", "Call A", "Call C"])
            # il primo processo vede la voce nuova creata dal secondo
            self.assertEqual(v.assign(sample(2), 2), "Call C")

    def test_stato_rovinato_ignorato(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / speakers.FILE
            p.write_text("{non json", encoding="utf-8")
            self.assertEqual(Voices(p, 0.5).assign(sample(0), 2), "Call A")


class TestCallSpeakers(unittest.TestCase):
    def test_solo_parlato_e_massimo_3_secondi(self):
        with tempfile.TemporaryDirectory() as d:
            fake = FakeEmbedder()
            cs = CallSpeakers(Path(d), 0.5, embedder=fake)
            x = np.full(SR * 8, 0.1, dtype=np.float32)
            mask = np.ones(len(x) // FRAME, dtype=bool)
            mask[: len(mask) // 2] = False  # metà silenzio: si usa solo il parlato
            self.assertEqual(cs.label(x, mask, FRAME, SR), "Call A")
            self.assertEqual(fake.calls, [int(speakers.MAX_SECS * SR / FRAME) * FRAME])

    def test_troppo_poco_parlato(self):
        with tempfile.TemporaryDirectory() as d:
            fake = FakeEmbedder()
            cs = CallSpeakers(Path(d), 0.5, embedder=fake)
            x = np.full(SR, 0.1, dtype=np.float32)
            mask = np.zeros(len(x) // FRAME, dtype=bool)
            mask[:5] = True  # 0,15 s
            self.assertEqual(cs.label(x, mask, FRAME, SR), "Call")
            self.assertEqual(fake.calls, [])


def stereo(regions, secs=10.0):
    """Audio stereo finto: (inizio, fine, canale, ampiezza)."""
    x = np.zeros((int(secs * SR), 2), dtype=np.float32)
    for a, b, c, amp in regions:
        x[int(a * SR): int(b * SR), c] = amp
    return x


class TestLiveIntegrazione(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mdir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def lt(self, **kw):
        lt = LiveTranscript(self.mdir, Settings(**kw))
        lt.log = lambda m: None
        return lt

    def test_righe_call_etichettate(self):
        lt = self.lt()
        lt.voices = CallSpeakers(self.mdir, 0.5, embedder=FakeEmbedder())
        x = stereo([(0, 3, 1, 0.1), (3.5, 4.5, 0, 0.1), (5, 8, 1, 0.2)])
        new = [Line(0.0, 3.0, "Call", "uno"), Line(3.5, 4.5, "Io", "due"), Line(5.0, 8.0, "Call", "tre")]
        lt._write(new, 10.0, x, 0.0)
        self.assertEqual([l.speaker for l in lt.lines()], ["Call A", "Io", "Call B"])
        # seconda passata: le stesse voci tornano con la stessa etichetta (start ≠ 0)
        lt._write([Line(20.0, 23.0, "Call", "quattro")], 30.0, stereo([(0, 3, 1, 0.2)]), 20.0)
        self.assertEqual(lt.lines()[-1].speaker, "Call B")
        self.assertTrue((self.mdir / speakers.FILE).exists())

    def test_interruttore_spento(self):
        lt = self.lt(live_speakers=False)
        lt._write([Line(0.0, 3.0, "Call", "uno")], 10.0, stereo([(0, 3, 1, 0.1)]), 0.0)
        self.assertEqual(lt.lines()[0].speaker, "Call")
        self.assertIsNone(lt.voices)  # il modello non viene nemmeno caricato

    def test_senza_sherpa_onnx(self):
        msgs = []
        lt = self.lt()
        lt.log = msgs.append
        with mock.patch.dict(sys.modules, {"sherpa_onnx": None}):  # import sherpa_onnx → ImportError
            for i in range(3):
                lt._write([Line(i * 10.0, i * 10 + 3.0, "Call", f"r{i}")], i * 10 + 10.0,
                          stereo([(0, 3, 1, 0.1)]), i * 10.0)
        self.assertEqual([l.speaker for l in lt.lines()], ["Call"] * 3)
        self.assertEqual(len(msgs), 1)  # log una volta sola

    def test_senza_modello(self):
        msgs = []
        emb = Embedder(log=msgs.append)
        with mock.patch("taccuino.engines.embedding_model", return_value=None):
            self.assertIsNone(emb(np.ones(SR * 2, dtype=np.float32), SR))
            self.assertIsNone(emb(np.ones(SR * 2, dtype=np.float32), SR))
        self.assertEqual(len(msgs), 1)

    def test_clean_lines_retrocompatibile(self):
        rows = [{"start": 0.0, "end": 2.0, "speaker": "Io", "text": "ciao"},
                {"start": 2.0, "end": 4.0, "speaker": "Call", "text": "buongiorno a tutti"}]
        (self.mdir / "live.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        lt = self.lt()
        self.assertEqual([(l.speaker, l.text) for l in lt.clean_lines()], [("Io", "ciao"), ("Call", "buongiorno a tutti")])
        (self.mdir / "live.names.json").write_text("[rotto", encoding="utf-8")
        self.assertEqual(lt.names(), {})
        self.assertEqual(lt.clean_lines()[1].speaker, "Call")


HAIKU = """0|Buongiorno, sono Pluto.
1|Ciao Pluto, ti sento bene.
2|Perfetto, iniziamo.
NOME|Call A|Pluto
NOME|Call B|?
NOME|Call C|Topolino
"""


class TestNomi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mdir = Path(self.tmp.name)
        rows = [{"start": 0.0, "end": 2.0, "speaker": "Call A", "text": "buongiorno sono pluto"},
                {"start": 2.0, "end": 4.0, "speaker": "Io", "text": "ciao pluto ti sento bene"},
                {"start": 4.0, "end": 6.0, "speaker": "Call B", "text": "perfetto iniziamo"}]
        (self.mdir / "live.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        self.lt = LiveTranscript(self.mdir, Settings())

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_names(self):
        voices = {"Call A", "Call B"}
        self.assertEqual(parse_names(HAIKU, voices), {"Call A": "Pluto"})  # "?" e voce inesistente scartati
        self.assertEqual(parse_names("NOME|call b|**Paperino**", voices), {"Call B": "Paperino"})
        self.assertEqual(parse_names("NOME|Call A|sconosciuto\nNOME|Call B|non è chiaro chi parli qui", voices), {})

    def test_cleaner_salva_nomi_con_una_sola_chiamata(self):
        with mock.patch("taccuino.summarize.run_claude", return_value=HAIKU) as rc, \
                mock.patch("taccuino.live.vocab_prompt", return_value=""):
            Cleaner(self.lt).run()
        self.assertEqual(rc.call_count, 1)
        self.assertIn("NOME|Call X|Nome", rc.call_args[0][1])
        self.assertEqual(json.loads((self.mdir / "live.names.json").read_text(encoding="utf-8")), {"Call A": "Pluto"})
        self.assertEqual([(l.speaker, l.text) for l in self.lt.clean_lines()],
                         [("Pluto", "Buongiorno, sono Pluto."), ("Io", "Ciao Pluto, ti sento bene."),
                          ("Call B", "Perfetto, iniziamo.")])
        self.assertEqual(self.lt.who("Call B"), "Call B")

    def test_niente_regola_nomi_senza_voci(self):
        rows = [{"start": 0.0, "end": 2.0, "speaker": "Call", "text": "ciao"}]
        (self.mdir / "live.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        with mock.patch("taccuino.summarize.run_claude", return_value="0|Ciao.\nNOME|Call A|Pluto") as rc, \
                mock.patch("taccuino.live.vocab_prompt", return_value=""):
            Cleaner(self.lt).run()
        self.assertNotIn("NOME|", rc.call_args[0][1])
        self.assertFalse((self.mdir / "live.names.json").exists())
        self.assertEqual(self.lt.clean_lines()[0].speaker, "Call")

    def test_claude_non_disponibile(self):
        with mock.patch("taccuino.summarize.run_claude", side_effect=RuntimeError("offline")), \
                mock.patch("taccuino.live.vocab_prompt", return_value=""):
            Cleaner(self.lt).run()
        self.assertEqual([l.speaker for l in self.lt.clean_lines()], ["Call A", "Io", "Call B"])


if __name__ == "__main__":
    unittest.main()
