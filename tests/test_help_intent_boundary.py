"""Help requests must be whole requests, not phrases inside another task."""

import ast
from pathlib import Path
import re
import unittest


def _load_detector():
    source = Path(__file__).resolve().parents[1] / "davosbot" / "brain.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if
             isinstance(node, ast.FunctionDef) and node.name == "detect_help_intent"
             or isinstance(node, ast.Assign) and any(
                 isinstance(target, ast.Name) and target.id == "_HELP_INTENT_RE"
                 for target in node.targets)]
    namespace = {"re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    return namespace["detect_help_intent"]


class HelpIntentBoundaryTests(unittest.TestCase):
    def test_direct_catalogue_requests_remain_help(self):
        detect = _load_detector()
        for text in ("help", "HELP!", "capabilities?", "What can you do?",
                     "What do you do for me?", "what are my commands",
                     "What capabilities do you have?", "How do I use this bot?",
                     "how do I use you", "Please what can you do please?",
                     "what can Davos do?"):
            with self.subTest(text=text):
                self.assertTrue(detect(text))

    def test_embedded_or_task_specific_phrases_stay_conversation(self):
        detect = _load_detector()
        for text in ("Help pick crops for a farmer. What do you do?",
                     "What can you do about a slice with my driver?",
                     "How do I use this golf club?", "How do you use this spreadsheet?",
                     "What capabilities does this computer have?",
                     "Don't show help", "Explain the sentence 'what can you do?'",
                     "What can you do? Then delete the file.", "help me pick crops",
                     "show me what are my commands means in Spanish", ""):
            with self.subTest(text=text):
                self.assertFalse(detect(text))


if __name__ == "__main__":
    unittest.main()
