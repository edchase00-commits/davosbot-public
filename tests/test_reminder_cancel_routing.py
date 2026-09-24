import unittest
from unittest.mock import patch

from davosbot import main, tools


class ReminderCancelRoutingTests(unittest.TestCase):
    def test_plain_english_multi_cancel_uses_deterministic_positions(self):
        calls = []

        def fake_cancel(positions, originating_chat_id=""):
            calls.append((positions, originating_chat_id))
            return "cancelled test"

        with patch.object(tools, "_cancel_reminders", fake_cancel):
            reply = main._handle_deterministic_reminder_cancel(
                "delete reminders 1 and 2 pls",
                "owner-chat",
            )

        self.assertEqual("cancelled test", reply)
        self.assertEqual([([1, 2], "owner-chat")], calls)

    def test_cancel_time_does_not_parse_as_position(self):
        self.assertEqual([], main._parse_reminder_cancel_positions("cancel my 9am reminder"))
        self.assertEqual([], main._parse_reminder_cancel_positions("delete the 2pm reminder"))


if __name__ == "__main__":
    unittest.main()
