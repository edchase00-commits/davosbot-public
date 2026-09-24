import ast
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from davosbot.reminder_parser import parse_deterministic_reminder


ROOT = Path(__file__).resolve().parents[1]


class DeterministicReminderParserTests(unittest.TestCase):
    def test_relative_minutes_before_message(self):
        now = datetime(2026, 5, 21, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

        parsed = parse_deterministic_reminder("remind me in 10 minutes to check the oven", now=now)

        self.assertEqual("check the oven", parsed.message)
        self.assertEqual("2026-05-21 19:10:00", parsed.due_ts)

    def test_relative_hours_after_message(self):
        now = datetime(2026, 5, 21, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

        parsed = parse_deterministic_reminder("remind me to call Cole in 2 hours", now=now)

        self.assertEqual("call Cole", parsed.message)
        self.assertEqual("2026-05-21 21:00:00", parsed.due_ts)

    def test_tomorrow_at_time_after_message(self):
        now = datetime(2026, 5, 21, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

        parsed = parse_deterministic_reminder("remind me to call Cole tomorrow at 9am", now=now)

        self.assertEqual("call Cole", parsed.message)
        self.assertEqual("2026-05-22 16:00:00", parsed.due_ts)

    def test_time_before_message(self):
        now = datetime(2026, 5, 21, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

        parsed = parse_deterministic_reminder("remind me at 5pm to stretch", now=now)

        self.assertEqual("stretch", parsed.message)
        self.assertEqual("2026-05-22 00:00:00", parsed.due_ts)

    def test_polite_set_reminder_for_time_before_message(self):
        now = datetime(2026, 5, 21, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

        parsed = parse_deterministic_reminder("can you set reminder for 5pm to stretch", now=now)

        self.assertEqual("stretch", parsed.message)
        self.assertEqual("2026-05-22 00:00:00", parsed.due_ts)

    def test_set_reminder_for_tomorrow_before_message(self):
        now = datetime(2026, 5, 21, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

        parsed = parse_deterministic_reminder("set reminder for tomorrow at 9am to call Cole", now=now)

        self.assertEqual("call Cole", parsed.message)
        self.assertEqual("2026-05-22 16:00:00", parsed.due_ts)

    def test_relaxed_tomorrow_time_before_message(self):
        now = datetime(2026, 6, 10, 15, 30, tzinfo=ZoneInfo("America/Los_Angeles"))

        parsed = parse_deterministic_reminder("remind me tomorrow 9am GitHub", now=now)

        self.assertEqual("GitHub", parsed.message)
        self.assertEqual("2026-06-11 16:00:00", parsed.due_ts)

    def test_relaxed_tomorrow_time_after_message(self):
        now = datetime(2026, 6, 10, 15, 30, tzinfo=ZoneInfo("America/Los_Angeles"))

        parsed = parse_deterministic_reminder("remind me file expense report tomorrow 9am", now=now)

        self.assertEqual("file expense report", parsed.message)
        self.assertEqual("2026-06-11 16:00:00", parsed.due_ts)

    def test_relaxed_tmw_time_before_message(self):
        now = datetime(2026, 6, 10, 15, 30, tzinfo=ZoneInfo("America/Los_Angeles"))

        parsed = parse_deterministic_reminder("remind me tmw at 5pm Amazon gift card", now=now)

        self.assertEqual("Amazon gift card", parsed.message)
        self.assertEqual("2026-06-12 00:00:00", parsed.due_ts)

    def test_relaxed_tmw_time_after_message(self):
        now = datetime(2026, 6, 10, 15, 30, tzinfo=ZoneInfo("America/Los_Angeles"))

        parsed = parse_deterministic_reminder("remind me GitHub tmw at 9am", now=now)

        self.assertEqual("GitHub", parsed.message)
        self.assertEqual("2026-06-11 16:00:00", parsed.due_ts)

    def test_relaxed_month_date_time_before_message(self):
        now = datetime(2026, 6, 10, 15, 30, tzinfo=ZoneInfo("America/Los_Angeles"))

        parsed = parse_deterministic_reminder("remind me June 21 at 9am to build an app", now=now)

        self.assertEqual("build an app", parsed.message)
        self.assertEqual("2026-06-21 16:00:00", parsed.due_ts)

    def test_relaxed_numeric_date_time_before_message(self):
        now = datetime(2026, 6, 10, 15, 30, tzinfo=ZoneInfo("America/Los_Angeles"))

        parsed = parse_deterministic_reminder("remind me 6/21 at 9am to build an app", now=now)

        self.assertEqual("build an app", parsed.message)
        self.assertEqual("2026-06-21 16:00:00", parsed.due_ts)

    def test_non_schedule_text_is_not_parsed(self):
        self.assertIsNone(parse_deterministic_reminder("reminder fix"))


class DeterministicReminderRouteTests(unittest.TestCase):
    def test_main_helper_calls_set_reminder_with_originating_chat(self):
        tree = ast.parse((ROOT / "davosbot" / "main.py").read_text(encoding="utf-8"))
        nodes = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_handle_deterministic_reminder_schedule"
        ]
        module = ast.Module(body=nodes, type_ignores=[])
        ast.fix_missing_locations(module)
        namespace = {
            "__name__": "davosbot.main",
            "__package__": "davosbot",
            "parse_deterministic_reminder": lambda text: type(
                "Parsed", (), {"message": "check oven", "due_ts": "2026-05-21 19:10:00"}
            )(),
        }
        exec(compile(module, str(ROOT / "davosbot" / "main.py"), "exec"), namespace)
        calls = []

        with patch("davosbot.tools._set_reminder", lambda message, due_ts, originating_chat_id: calls.append((message, due_ts, originating_chat_id)) or "Got it"):
            reply = namespace["_handle_deterministic_reminder_schedule"]("remind me in 10 minutes to check oven", "origin-chat")

        self.assertEqual("Got it", reply)
        self.assertEqual([("check oven", "2026-05-21 19:10:00", "origin-chat")], calls)


if __name__ == "__main__":
    unittest.main()
