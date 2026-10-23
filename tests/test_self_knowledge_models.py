from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
# The public exporter sanitizes and renames this asset. Missing both still
# fails the tests; neither pipeline silently skips the model contract.
KNOWLEDGE = ROOT / ("BOT_SELF_KNOWLEDGE.md" if (ROOT / "BOT_SELF_KNOWLEDGE.md").is_file() else "SELF_KNOWLEDGE.md")


class SelfKnowledgeModelTests(unittest.TestCase):
    def test_self_knowledge_names_current_model_routes(self):
        text = KNOWLEDGE.read_text(encoding="utf-8")

        assert "gemini-3.1-flash-lite" in text
        assert "gemini-3.5-flash" in text
        assert "gemini-3.1-flash-image" in text
        assert "Nano Banana" in text
        assert "OpenAI/GPT is not used by default routes" in text


    def test_self_knowledge_has_no_stale_text_model_defaults(self):
        text = KNOWLEDGE.read_text(encoding="utf-8")
        text_without_legacy_image_note = text.replace("gemini-2.5-flash-image", "")

        assert "gemini-2.5-flash" not in text_without_legacy_image_note
        assert "Gemini 2.5 Flash" not in text
        assert "normally `gemma3`" not in text


    def test_memory_reset_baseline_names_current_model_routes(self):
        text = (ROOT / "davosbot" / "commands.py").read_text(encoding="utf-8")

        assert "Gemini 3.1 Flash-Lite as fallback/tool-use" in text
        assert "Gemini 3.5 Flash" in text
        assert "Gemini 2.5 Flash as fallback" not in text
