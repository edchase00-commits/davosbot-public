import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.eval_context_retention import CASES, fitted_inputs, load_fitters


ROOT = Path(__file__).resolve().parents[1]


class ContextEvaluatorTests(unittest.TestCase):
    def test_source_loader_never_imports_runtime_or_reads_private_files(self):
        source = (ROOT / "davosbot/brain.py").read_text(encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=AssertionError("private read")):
            namespace = load_fitters(source, 8192)
            system, history = fitted_inputs(namespace, "Synthetic identity", CASES[0], 180)
        self.assertEqual("Synthetic identity", system)
        self.assertIn("$120", history[0]["content"])
        self.assertIn("Saturday at 7pm", history[0]["content"])
        self.assertNotIn("_init_db_tables", namespace)
        self.assertNotIn("get_response", namespace)

    def test_legacy_loader_uses_the_original_limits(self):
        source = '''
_MAX_OLLAMA_HISTORY_CHARS = 12
_MAX_OLLAMA_HISTORY_TURN_CHARS = 6
def _fit_system_for_ollama(system):
    return system
def _fit_history_for_model(history, max_chars, max_turn_chars):
    return [{"role": "user", "content": str((max_chars, max_turn_chars))}]
'''
        _, history = fitted_inputs(load_fitters(source, 8192), "synthetic", CASES[0], 180)
        self.assertEqual("(12, 6)", history[0]["content"])

    def test_context_limit_is_distinct_from_empty_history(self):
        source = (ROOT / "davosbot/brain.py").read_text(encoding="utf-8")
        namespace = load_fitters(source, 200)
        _, history = fitted_inputs(namespace, "synthetic", CASES[0], 180)
        self.assertIsNone(history)


if __name__ == "__main__":
    unittest.main()
