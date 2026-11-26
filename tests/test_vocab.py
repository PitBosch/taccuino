"""Vocabolari di progetto nella KB: scelta del file giusto e prompt per Whisper/Haiku."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from taccuino import vocab
from taccuino.config import Settings


def make_kb(tmp: Path) -> Settings:
    (tmp / "Clienti" / "Alfa").mkdir(parents=True)
    (tmp / "Clienti" / "Beta").mkdir(parents=True)
    (tmp / "Clienti" / "Alfa" / "Vocabolario.md").write_text(
        '# Alfa\ntesto libero\n- Quasar — sistema (trascritto anche "Quasi")\n- Orione\n', encoding="utf-8")
    (tmp / "Clienti" / "Beta" / "Vocabolario.md").write_text("- Zeta\n- **Eta** — nota\n", encoding="utf-8")
    (tmp / "Vocabolario.md").write_text("- Generale\n", encoding="utf-8")
    return Settings(output_dir=str(tmp))


class ProjectVocab(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = make_kb(Path(self.tmp.name))
        p = mock.patch.object(vocab, "VOCAB_PATH", Path(self.tmp.name) / "nessun-vocabolario.txt")
        p.start()
        self.addCleanup(p.stop)

    def test_cartella_nota_usa_solo_quel_progetto_e_i_genitori(self):
        w = vocab.prompt_text(self.s, 40, "Clienti/Alfa")
        self.assertEqual(set(w.split(", ")), {"Quasar", "Orione", "Generale"})  # un termine a testa per file
        self.assertTrue(w.startswith("Quasar"))
        self.assertNotIn("Zeta", w)

    def test_cartella_ignota_prende_un_termine_a_testa_da_ogni_progetto(self):
        w = vocab.prompt_text(self.s, 40, None).split(", ")
        self.assertEqual(set(w), {"Quasar", "Orione", "Zeta", "Eta", "Generale"})
        self.assertLess(w.index("Zeta"), w.index("Orione"))  # alternati, non un file dopo l'altro

    def test_haiku_riceve_le_note(self):
        h = vocab.prompt_text(self.s, None, "Clienti/Alfa")
        self.assertIn('Quasar — sistema (trascritto anche "Quasi")', h)

    def test_limite_di_caratteri_per_whisper(self):
        (Path(self.tmp.name) / "Vocabolario.md").write_text("\n".join(f"- termine{i:03d}x" for i in range(200)))
        self.assertLessEqual(len(vocab.prompt_text(self.s, 500, "")), vocab.WHISPER_CHARS)

    def test_cartella_esclusa(self):
        self.s.exclude_folders = ["Beta"]
        self.assertNotIn("Zeta", vocab.prompt_text(self.s, 40, None))


if __name__ == "__main__":
    unittest.main()
