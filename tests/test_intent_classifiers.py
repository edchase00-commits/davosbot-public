import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_brain_classifiers():
    tree = ast.parse((ROOT / "davosbot" / "brain.py").read_text(encoding="utf-8"))
    wanted_assigns = {
        "_REMINDER_WORD_RE",
        "_SCHEDULING_REMINDER_RE",
        "_CANCEL_REMINDER_RE",
        "_LIST_REMINDER_RE",
        "_REMINDER_NEGATIVE_RE",
        "_CRON_NOUN_RE",
        "_CRON_LIST_VERBS_RE",
        "_CRON_SCHEDULE_VERBS_RE",
        "_CRON_CANCEL_VERBS_RE",
    }
    wanted_funcs = {"classify_reminder_intent", "classify_cron_list_intent"}
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id in wanted_assigns for target in node.targets):
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted_funcs:
            nodes.append(node)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"re": re}
    exec(compile(module, str(ROOT / "davosbot" / "brain.py"), "exec"), namespace)
    return namespace


def _load_persona_switch_detector(resolver):
    tree = ast.parse((ROOT / "davosbot" / "commands.py").read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "_PERSONA_SWITCH_RE" for target in node.targets):
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "_detect_persona_switch":
            nodes.append(node)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"re": re, "resolve_persona_name": resolver}
    exec(compile(module, str(ROOT / "davosbot" / "commands.py"), "exec"), namespace)
    return namespace["_detect_persona_switch"]


def _load_cron_scope_helpers():
    tree = ast.parse((ROOT / "davosbot" / "commands.py").read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {
            "_cron_scope_from_text", "_wants_all_crons", "_parse_cancel_cron_id",
        }:
            nodes.append(node)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"re": re}
    exec(compile(module, str(ROOT / "davosbot" / "commands.py"), "exec"), namespace)
    return namespace


def _load_change_log_triage_classifier():
    from davosbot.change_log_triage import _classify_change_request
    return _classify_change_request


class IntentClassifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        brain = _load_brain_classifiers()
        cls.classify_reminder_intent = staticmethod(brain["classify_reminder_intent"])
        cls.classify_cron_list_intent = staticmethod(brain["classify_cron_list_intent"])

    def test_reminder_classifier_keeps_positive_mentions_out_of_command_path(self):
        self.assertEqual("none", self.classify_reminder_intent("the reminder worked great"))
        self.assertEqual("none", self.classify_reminder_intent("good reminder, thanks"))
        self.assertEqual("casual", self.classify_reminder_intent("that reminder never fired"))
        self.assertEqual("schedule", self.classify_reminder_intent("remind me tomorrow to call Cole"))
        self.assertEqual("schedule", self.classify_reminder_intent("can you remind me tomorrow to call Cole"))
        self.assertEqual("schedule", self.classify_reminder_intent("set reminder for 5pm to stretch"))
        self.assertEqual("cancel", self.classify_reminder_intent("cancel that reminder"))
        self.assertEqual("cancel", self.classify_reminder_intent("delete reminders 1 and 2 pls"))
        self.assertEqual("list", self.classify_reminder_intent("what are my open reminders"))
        self.assertEqual("list", self.classify_reminder_intent("do i have any reminders"))
        self.assertEqual("list", self.classify_reminder_intent("show pending reminders"))
        self.assertEqual("list", self.classify_reminder_intent("reminders are what again"))

    def test_cron_list_classifier_only_catches_listing_intent(self):
        self.assertTrue(self.classify_cron_list_intent("do we have any current cron jobs"))
        self.assertTrue(self.classify_cron_list_intent("show all recurring jobs"))
        self.assertTrue(self.classify_cron_list_intent("list all jobs across chats"))
        self.assertTrue(self.classify_cron_list_intent("show me my automations"))
        self.assertFalse(self.classify_cron_list_intent("schedule a cron at 8am"))
        self.assertFalse(self.classify_cron_list_intent("delete cron id 7"))
        self.assertFalse(self.classify_cron_list_intent("that job was funny"))

    def test_persona_switch_detector_requires_known_persona(self):
        known = {"gruden": "gruden", "hansi": "hansi flick", "hansi flick": "hansi flick"}
        detector = _load_persona_switch_detector(lambda name, include_hidden=True: known.get(name.lower()))

        self.assertEqual("gruden", detector("go full gruden"))
        self.assertEqual("hansi flick", detector("switch persona to hansi"))
        self.assertIsNone(detector("be careful"))
        self.assertIsNone(detector("use the browser"))
        self.assertIsNone(detector("atl roast"))
        self.assertIsNone(detector("roast atl"))
        self.assertIsNone(detector("be atl roast"))

    def test_cron_scope_helpers_parse_phone_phrases(self):
        helpers = _load_cron_scope_helpers()
        scope = helpers["_cron_scope_from_text"]
        wants_all = helpers["_wants_all_crons"]

        self.assertEqual("all", scope("list all cron jobs"))
        self.assertEqual("mine", scope("list the crons just to me"))
        self.assertEqual("direct", scope("list all dm crons"))
        self.assertEqual("groups", scope("show group chat jobs"))
        self.assertEqual("current", scope("list crons"))
        self.assertTrue(wants_all("list all cron jobs"))

    def test_cancel_cron_id_parser_does_not_mistake_times_for_ids(self):
        helpers = _load_cron_scope_helpers()
        parse = helpers["_parse_cancel_cron_id"]

        self.assertEqual(7, parse("delete #7"))
        self.assertEqual(7, parse("delete id 7"))
        self.assertEqual(7, parse("cancel cron 7"))
        self.assertIsNone(parse("delete 6:30 daily"))

    def test_change_log_triage_classifier_uses_risk_colors(self):
        classify = _load_change_log_triage_classifier()

        self.assertEqual("green", classify("docs cleanup and help text wording"))
        self.assertEqual("yellow", classify("cron list UX for all chats"))
        self.assertEqual("yellow", classify("group persona creation flow"))
        self.assertEqual("red", classify("private message send routing"))
        self.assertEqual("red", classify("touch permissions.py admin password gate"))


if __name__ == "__main__":
    unittest.main()
