import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_reminder_postcondition_helpers():
    tree = ast.parse((ROOT / "davosbot" / "main.py").read_text(encoding="utf-8"))
    names = {
        "_looks_like_reminder_confirmation",
        "_guard_unsaved_reminder_reply",
    }
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "re": re,
        "logger": type("Logger", (), {"warning": lambda *args, **kwargs: None})(),
        "__name__": "davosbot.main",
        "__package__": "davosbot",
    }
    exec(compile(module, str(ROOT / "davosbot" / "main.py"), "exec"), namespace)
    return namespace


class ReminderPostconditionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        helpers = _load_reminder_postcondition_helpers()
        cls.looks_like_confirmation = staticmethod(helpers["_looks_like_reminder_confirmation"])
        cls.guard = staticmethod(helpers["_guard_unsaved_reminder_reply"])

    def test_detects_fake_reminder_confirmation(self):
        self.assertTrue(self.looks_like_confirmation("Got it \u2014 I'll remind you tomorrow."))
        self.assertTrue(self.looks_like_confirmation("I\u2019ll remind you tomorrow."))
        self.assertTrue(self.looks_like_confirmation("Reminder set."))
        self.assertFalse(self.looks_like_confirmation("What time should I remind you?"))
        self.assertFalse(self.looks_like_confirmation("I couldn't save that reminder."))

    def test_guard_replaces_confirmation_when_no_row_inserted(self):
        reply = self.guard("Got it \u2014 I'll remind you tomorrow.", before_count=2, after_count=2)

        self.assertEqual("I didn't actually save that reminder. Please resend it.", reply)

    def test_guard_allows_confirmation_when_row_inserted(self):
        reply = self.guard("Got it \u2014 I'll remind you tomorrow.", before_count=2, after_count=3)

        self.assertEqual("Got it \u2014 I'll remind you tomorrow.", reply)

    def test_guard_allows_non_confirmation_followup(self):
        reply = self.guard("What time should I remind you?", before_count=2, after_count=2)

        self.assertEqual("What time should I remind you?", reply)


if __name__ == "__main__":
    unittest.main()
