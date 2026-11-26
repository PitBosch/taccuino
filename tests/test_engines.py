"""ASR: su errore del device (DEVICE_LOST) ricarica lo stesso modello sullo stesso device e riprova, max 2 volte."""
import time
import unittest
from pathlib import Path

import numpy as np

from taccuino.engines import ASR


class Res:
    def __init__(self, text):
        self.texts = [text]


class Pipe:
    def __init__(self, fails=0, text="ciao a tutti"):
        self.fails, self.text = fails, text

    def generate(self, audio, **kw):
        if self.fails:
            self.fails -= 1
            raise RuntimeError("ZE_RESULT_ERROR_DEVICE_LOST")
        return Res(self.text)


def make(reloaded_fails=0, vocabulary=""):
    a = object.__new__(ASR)
    a.whisper, a.language, a.vocabulary, a.device = True, "it", vocabulary, "GPU"
    a.path, a.cache, opened = "x", Path("."), []

    def cls(path, device, **kw):
        opened.append(device)
        return Pipe(reloaded_fails)

    a.cls, a.pipe = cls, Pipe(1)
    return a, opened


X = np.zeros(16000, dtype=np.float32)


class Retry(unittest.TestCase):
    def setUp(self):
        real = time.sleep
        time.sleep = lambda _: None
        self.addCleanup(lambda: setattr(time, "sleep", real))

    def test_ricarica_la_gpu_e_riprova(self):
        a, opened = make()
        self.assertEqual(a.transcribe(X), "ciao a tutti")
        self.assertEqual(opened, ["GPU"])

    def test_dopo_due_tentativi_solleva_e_non_cambia_device(self):
        a, opened = make(reloaded_fails=5)
        with self.assertRaises(RuntimeError):
            a.transcribe(X)
        self.assertEqual(opened, ["GPU", "GPU"])


class PromptEcho(unittest.TestCase):
    def test_scarta_il_prompt_ripetuto_su_silenzio(self):
        a, _ = make(vocabulary="Quasar Cloud, Orione, Databricks")
        a.pipe = Pipe(0, "Quasar Cloud, Orione, Databricks")
        self.assertEqual(a._transcribe(X), "")

    def test_tiene_le_frasi_vere(self):
        a, _ = make(vocabulary="Databricks, Orione")
        a.pipe = Pipe(0, "ho letto i file di Orione ieri")
        self.assertEqual(a._transcribe(X), "ho letto i file di Orione ieri")
        a.pipe = Pipe(0, "Databricks.")
        self.assertEqual(a._transcribe(X), "Databricks.")


if __name__ == "__main__":
    unittest.main()
