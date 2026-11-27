"""Parakeet: scelta del motore dal modello configurato, taglio dell'audio lungo nei punti di silenzio."""
import unittest
from unittest import mock

import numpy as np

from taccuino import engines
from taccuino.config import ASR_MODELS, ASR_PROFILES, is_parakeet
from taccuino.engines import split_quiet


class TestModello(unittest.TestCase):
    def test_riconosce_parakeet(self):
        self.assertTrue(is_parakeet(ASR_MODELS["parakeet-q4"]))
        self.assertTrue(is_parakeet(ASR_PROFILES["parakeet"][0]))
        self.assertFalse(is_parakeet(ASR_MODELS["turbo-int8"]))

    def test_make_asr_sceglie_il_motore(self):
        with mock.patch.object(engines, "ParakeetASR") as pk, mock.patch.object(engines, "ASR") as ov:
            engines.make_asr(*ASR_PROFILES["parakeet"], language="it")
            engines.make_asr(*ASR_PROFILES["xe"], language="it")
        self.assertEqual(pk.call_count, 1)
        self.assertEqual(ov.call_count, 1)

    def test_libreria_mancante_dice_come_compilarla(self):
        from taccuino import native
        from pathlib import Path
        with self.assertRaisesRegex(RuntimeError, "parakeet-build"):
            native.load(Path("/non/esiste/libtaccuino-asr"))


class TestTaglio(unittest.TestCase):
    def test_audio_corto_resta_intero(self):
        x = np.ones(16000 * 10, dtype=np.float32)
        self.assertEqual(len(split_quiet(x, 30)), 1)

    def test_taglia_nel_silenzio(self):
        sr = 16000
        x = np.random.default_rng(0).normal(0, 0.3, sr * 70).astype(np.float32)
        x[int(25.0 * sr):int(25.5 * sr)] = 0  # pausa a 25 s, nell'ultimo terzo della finestra da 30 s
        parts = split_quiet(x, 30)
        self.assertEqual(sum(len(p) for p in parts), len(x))
        self.assertTrue(all(len(p) <= 30 * sr for p in parts))
        self.assertTrue(25.0 <= len(parts[0]) / sr <= 25.5)


if __name__ == "__main__":
    unittest.main()
