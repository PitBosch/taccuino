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


class FindVault(unittest.TestCase):
    """Regressione: `Desktop` con un .obsidian dentro e il vero vault annidato; note nel .venv non contano."""

    def test_vault_annidato_e_ambienti_virtuali(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "Desktop" / ".obsidian").mkdir(parents=True)
            (home / "Desktop" / "KB" / ".obsidian").mkdir(parents=True)
            (home / "Desktop" / "KB" / "nota.md").write_text("x")
            (home / "Desktop" / "repo" / ".venv" / "Lib" / "site-packages").mkdir(parents=True)
            for i in range(50):
                (home / "Desktop" / "repo" / ".venv" / "Lib" / "site-packages" / f"{i}.md").write_text("x")
            s = Settings(output_dir=str(home / "Documenti" / "Taccuino"))
            with mock.patch.object(vocab.Path, "home", return_value=home):
                self.assertEqual(vocab.find_vault(s), home / "Desktop" / "KB")
                self.assertEqual([p.name for p in vocab._notes(home / "Desktop")], ["nota.md"])

    def test_se_le_note_sono_gia_in_un_vault_e_quello(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            for v in ("A", "B"):
                (home / "Desktop" / v / ".obsidian").mkdir(parents=True)
            (home / "Desktop" / "A" / "1.md").write_text("x")  # A ha più note, ma le note di Taccuino vanno in B
            (home / "Desktop" / "A" / "2.md").write_text("x")
            s = Settings(output_dir=str(home / "Desktop" / "B"))
            with mock.patch.object(vocab.Path, "home", return_value=home):
                self.assertEqual(vocab.find_vault(s), home / "Desktop" / "B")


if __name__ == "__main__":
    unittest.main()
