import unittest

from davosbot.personality import DECATUR_BEHAVIOR_EMOJIS
from davosbot.text_safety import (
    is_imessage_reaction,
    is_imessage_reaction_text,
    normalize_bot_text,
)


class TextSafetyTests(unittest.TestCase):
    def test_normalize_bot_text_blocks_glitched_phrase_and_preserves_emojis(self):
        text = normalize_bot_text("my g 🔫 ✊🏿 💣 locked in 😂 😭")

        self.assertNotIn("my g", text.lower())
        self.assertIn("🔫", text)
        self.assertIn("💣", text)
        self.assertIn("✊🏿", text)
        self.assertIn("😂", text)
        self.assertIn("😭", text)

    def test_normalize_bot_text_preserves_full_decatur_pack(self):
        text = normalize_bot_text("Decatur behavior emojis:\n" + DECATUR_BEHAVIOR_EMOJIS)

        self.assertEqual("Decatur behavior emojis:\n" + DECATUR_BEHAVIOR_EMOJIS, text)

    def test_detects_imessage_tapback_text(self):
        self.assertTrue(is_imessage_reaction_text('Loved "see you at 8"'))
        self.assertTrue(is_imessage_reaction_text("Laughed at “that was brutal”"))
        self.assertTrue(is_imessage_reaction_text("Liked an image"))
        self.assertFalse(is_imessage_reaction_text("I loved that image scan"))

    def test_detects_imessage_tapback_metadata(self):
        self.assertTrue(is_imessage_reaction("normal looking text", 2000, "guid"))
        self.assertFalse(is_imessage_reaction("normal looking text", 0, ""))


if __name__ == "__main__":
    unittest.main()
