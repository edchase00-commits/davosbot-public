import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from davosbot import personality
class PersonaResolutionTests(unittest.TestCase):
    def test_visible_multiword_persona_supports_unique_short_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "hansi flick.md").write_text("Hansi mode", encoding="utf-8")
            (root / "baby gal.md").write_text("Baby gal mode", encoding="utf-8")

            with patch.object(personality, "_PERSONAS_DIR", root):
                self.assertEqual("hansi flick", personality.resolve_persona_name("hansi"))
                self.assertEqual("hansi flick", personality.resolve_persona_name("hansiflick"))
                self.assertEqual("hansi flick", personality.resolve_persona_name("hansi-flick"))
                self.assertEqual("baby gal", personality.resolve_persona_name("baby"))

    def test_ambiguous_short_alias_does_not_guess(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "hansi flick.md").write_text("Hansi mode", encoding="utf-8")
            (root / "hansi dude.md").write_text("Other hansi mode", encoding="utf-8")

            with patch.object(personality, "_PERSONAS_DIR", root):
                self.assertIsNone(personality.resolve_persona_name("hansi"))
                self.assertEqual("hansi flick", personality.resolve_persona_name("hansi flick"))
                self.assertEqual("hansi flick", personality.resolve_persona_name("hansiflick"))

    def test_hidden_personas_stay_exact_invocation_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "_secret boss.md").write_text("Hidden mode", encoding="utf-8")
            (root / "gruden.md").write_text("Hidden by name", encoding="utf-8")

            with patch.object(personality, "_PERSONAS_DIR", root):
                self.assertEqual("secret boss", personality.resolve_persona_name("secret boss"))
                self.assertEqual("secret boss", personality.resolve_persona_name("secretboss"))
                self.assertIsNone(personality.resolve_persona_name("secret"))
                self.assertEqual("gruden", personality.resolve_persona_name("gruden"))
                self.assertEqual([], personality.list_personas())


if __name__ == "__main__":
    unittest.main()
