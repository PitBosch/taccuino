"""`taccuino setup`: scelta di profilo e modello in base alla macchina; non tocca ciò che è già configurato."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from taccuino import config, setup_wizard
from taccuino.config import ASR_MODELS, ASR_PROFILES


class Recommend(unittest.TestCase):
    def test_con_gpu_il_modello_dipende_dalla_ram(self):
        gpu = ["CPU", "GPU", "NPU"]
        self.assertEqual(setup_wizard.recommend(16, gpu)[:2], ("xe", "turbo-int8"))
        self.assertEqual(setup_wizard.recommend(8, gpu)[:2], ("xe", "turbo-int4"))
        self.assertEqual(setup_wizard.recommend(4, gpu)[:2], ("xe", "small-int8"))

    def test_senza_gpu_cpu_e_modello_piccolo(self):
        self.assertEqual(setup_wizard.recommend(32, ["CPU"])[:2], ("cpu", "small-int8"))

    def test_la_npu_non_e_mai_scelta_da_sola(self):
        self.assertEqual(setup_wizard.recommend(32, ["CPU", "NPU"])[0], "cpu")

    def test_i_modelli_esistono_nel_catalogo(self):
        for key in ("turbo-int8", "turbo-int4", "small-int8"):
            self.assertIn(key, ASR_MODELS)
        self.assertIn(ASR_PROFILES["xe"][0], ASR_MODELS.values())


class Setup(unittest.TestCase):
    def run_setup(self, raw: dict | None, **kw):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            if raw is not None:
                cfg.write_text(json.dumps(raw))
            with mock.patch.object(config, "CONFIG_PATH", cfg), mock.patch.object(setup_wizard, "CONFIG_PATH", cfg), \
                    mock.patch.object(config, "DATA_DIR", Path(d)), mock.patch.object(setup_wizard, "DATA_DIR", Path(d)), \
                    mock.patch.object(setup_wizard, "ov_devices", return_value=["CPU", "GPU"]), \
                    mock.patch.object(setup_wizard.platforms, "total_ram_gb", return_value=kw.pop("ram", 16.0)), \
                    mock.patch("taccuino.vocab.find_vault", return_value=None):
                setup_wizard.setup(yes=True, **kw)
            return json.loads(cfg.read_text()) if cfg.exists() else {}

    def test_prima_installazione_scrive_il_profilo(self):
        self.assertEqual(self.run_setup(None)["profile"], "xe")

    def test_ram_bassa_sceglie_int4(self):
        self.assertEqual(self.run_setup(None, ram=9.0)["asr_model"], ASR_MODELS["turbo-int4"])

    def test_non_sovrascrive_quello_gia_impostato(self):
        out = self.run_setup({"profile": "npu", "output_dir": "X"}, ram=4.0)
        self.assertEqual((out["profile"], out["output_dir"]), ("npu", "X"))

    def test_le_opzioni_vincono(self):
        out = self.run_setup({"profile": "npu"}, profile="cpu", output_dir="Y")
        self.assertEqual((out["profile"], out["output_dir"]), ("cpu", "Y"))


if __name__ == "__main__":
    unittest.main()
