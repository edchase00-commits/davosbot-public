"""Reminder input accuracy through pure parsing and real scoped SQLite writes."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from davosbot import brain, group_chat, main, reminder_parser, reminder_tools
from davosbot.db import connect_bot_db
from davosbot.reminder_parser import (
    ReminderParseError, parse_deterministic_reminder, parse_reminder_cancel_positions,
)
import test_no_web_tool_permissions as route_fixture
from test_no_web_tool_permissions import OWNER, ADMIN, FRIEND, GROUP


NOW = datetime(2026, 8, 6, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
OTHER_GROUP = "abcdef0123456789abcdef0123456789"


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class ReminderInputParserTests(unittest.TestCase):
    def parse(self, text, now=NOW):
        return parse_deterministic_reminder(text, now=now)

    def test_dotted_clock_does_not_drop_minutes_or_ampm(self):
        for clock, utc in (("7.35am", "14:35:00"), ("7.35 p.m.", "02:35:00"),
                           ("00.35", "07:35:00"), ("12.35am", "07:35:00")):
            with self.subTest(clock=clock):
                result = self.parse(f"remind me tomorrow at {clock} to pack lunch")
                self.assertEqual("pack lunch", result.message)
                day = "08" if "p.m." in clock else "07"
                self.assertEqual(f"2026-08-{day} {utc}", result.due_ts)

    def test_calendar_day_and_weekday_are_one_constraint(self):
        for text in (
            "remind me Sunday August 23 at 9am to charge camera",
            "remind me on Sunday, August 23 at 9am to charge camera",
            "remind me to charge camera on Sunday August 23 at 9am",
            "remind me Sunday 8/23/2026 at 9am to charge camera",
            "remind me to charge camera Sunday 8/23/26 at 9am",
        ):
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertEqual("charge camera", result.message)
                self.assertEqual("2026-08-23 16:00:00", result.due_ts)

    def test_calendar_conflicts_and_invalid_dates_are_not_partial_success(self):
        for text in (
            "remind me Monday August 23 at 9am to charge camera",
            "remind me to charge camera Monday 8/23/2026 at 9am",
            "remind me September 31 at 9am to charge camera",
            "remind me February 29 2027 at 9am to charge camera",
            "remind me July 2 2026 at 9am to charge camera",
            "remind me Sunday on August 23 at 9am to charge camera",
        ):
            with self.subTest(text=text), self.assertRaises(ReminderParseError):
                self.parse(text)

    def test_yearless_date_rollover_and_leap_year(self):
        now = datetime(2026, 12, 29, 12, tzinfo=NOW.tzinfo)
        self.assertEqual("2027-01-03 17:00:00", self.parse(
            "remind me Sunday January 3 at 9am to charge camera", now).due_ts)
        self.assertEqual("2028-02-29 17:00:00", self.parse(
            "remind me Tuesday February 29 2028 at 9am to charge camera").due_ts)

    def test_compact_duration_preserves_multiline_unicode_and_punctuation(self):
        message = "Water plants.\n\nCheck mail: café 🌱\n- Keep this list!"
        for text in (f"remind me in 4h\n{message}", f"remind me to {message}\nin 4 hrs"):
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertEqual(message, result.message)
                self.assertEqual("2026-08-06 23:00:00", result.due_ts)
        self.assertEqual("tomatoes", self.parse("remind me in 15m tomatoes").message)
        self.assertEqual("Water plants,\nthen check mail,", self.parse(
            "remind me tomorrow at 7.35am to Water plants,\nthen check mail,").message)

    def test_duration_keeps_event_time_in_body(self):
        result = self.parse("remind me in 4h event starts at 9am")
        self.assertEqual("event starts at 9am", result.message)
        self.assertEqual("2026-08-06 23:00:00", result.due_ts)

    def test_invalid_clock_and_compound_duration_clarify(self):
        for text in (
            "remind me tomorrow at 7.3am to pack lunch",
            "remind me tomorrow at 7.99am to pack lunch",
            "remind me tomorrow at 25:15 to pack lunch",
            "remind me tomorrow at 7am or 9am to pack lunch",
            "remind me in 0h to pack lunch",
            "remind me in -4 hours to pack lunch",
            "remind me in 1 hour 30 minutes to pack lunch",
            "remind me in 999999999999999999999999 hours to pack lunch",
        ):
            with self.subTest(text=text), self.assertRaises(ReminderParseError):
                self.parse(text)

    def test_cancel_ranges_are_inclusive_and_deduplicated(self):
        for text in ("cancel reminders 2-5", "delete my reminders #2 through #5 pls",
                     "remove reminders 2–5", "cancel reminders 2, 3-5 and 4"):
            with self.subTest(text=text):
                self.assertEqual([2, 3, 4, 5], parse_reminder_cancel_positions(text))

    def test_invalid_cancel_selectors_reject_whole_request(self):
        for text in ("cancel reminders 5-2", "cancel reminders 0-3", "cancel reminders 2-",
                     "cancel reminders 2.5", "cancel reminders 2-5 except 4",
                     "cancel reminders 2-50000", "cancel reminders 2 and 9am"):
            with self.subTest(text=text), self.assertRaises(ReminderParseError):
                parse_reminder_cancel_positions(text)
        with self.assertRaises(ReminderParseError):
            parse_reminder_cancel_positions("cancel reminders " + "2 " * 3000)

    def test_nonpositional_cancellation_remains_nonpositional(self):
        for text in ("cancel my 9am reminder", "delete the 2pm reminder",
                     "cancel my gym reminder", "we discussed reminders 2 and 5"):
            self.assertEqual([], parse_reminder_cancel_positions(text))

    def test_optional_mention_preservation_keeps_body_and_default_behavior(self):
        raw = "@Davos remind me in 4h\n\nquote @davos;"
        self.assertEqual("remind me in 4h\n\nquote @davos;", group_chat.strip_mention(raw, preserve_body=True))
        self.assertEqual("cancel reminders 2-", group_chat.strip_mention("@Davos cancel reminders 2-", preserve_body=True))
        self.assertEqual("cancel reminders 2", group_chat.strip_mention("@Davos cancel reminders 2-"))


class ReminderInputDispatchTests(unittest.TestCase):
    def setUp(self):
        # Reuse the suite's real DM/group route fixture, which captures all sends
        # and isolates unrelated providers, persona files and private runtime state.
        route_fixture.NoWebRouteTests.setUp(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "reminders.sqlite")
        with connect_bot_db(self.db) as conn:
            conn.execute("CREATE TABLE reminders (id INTEGER PRIMARY KEY, chat_id TEXT, "
                         "message TEXT, due_ts TEXT, sent INTEGER DEFAULT 0, origin_chat_id TEXT, "
                         "send_attempts INTEGER DEFAULT 0)")
        for module in (main, reminder_tools):
            self.stack.enter_context(patch.object(module, "BOT_DB_PATH", self.db))
        self.stack.enter_context(patch.object(main, "classify_reminder_intent", brain.classify_reminder_intent))
        self.stack.enter_context(patch.object(reminder_parser, "datetime", FrozenDateTime))
        self.stack.enter_context(patch.object(reminder_tools, "datetime", FrozenDateTime))
        self.stack.enter_context(patch.object(reminder_tools, "_utcnow_naive",
                                             return_value=NOW.astimezone(timezone.utc).replace(tzinfo=None)))

    def rows(self):
        with connect_bot_db(self.db) as conn:
            return conn.execute("SELECT chat_id, origin_chat_id, message, due_ts FROM reminders ORDER BY id").fetchall()

    def route(self, text, *, group=False, sender=OWNER):
        self.model.reset_mock()
        self.send.reset_mock()
        if group:
            main.handle_group_message(sender, GROUP, "@Davos " + text)
        else:
            main.handle_dm(sender, text)

    def seed(self):
        with connect_bot_db(self.db) as conn:
            for destination in (OWNER, GROUP, OTHER_GROUP):
                for i in range(1, 7):
                    conn.execute("INSERT INTO reminders (chat_id, origin_chat_id, message, due_ts) VALUES (?,?,?,?)",
                                 (destination, destination, f"synthetic item {i}", f"2026-08-{10+i:02d} 17:00:00"))

    def test_actual_owner_dm_and_group_create_exact_time_and_origin(self):
        self.route("remind me Sunday August 23 at 7.35am to charge camera")
        self.model.assert_not_called()
        self.assertEqual(OWNER, self.send.call_args.args[0])
        self.assertIn("7:35 am PT", self.send.call_args.args[1])
        self.route("remind me in 4h\nWater plants\n\nCheck mail;", group=True)
        self.model.assert_not_called()
        self.assertEqual(GROUP, self.send.call_args.args[0])
        self.assertTrue(self.send.call_args.kwargs["is_group"])
        self.assertEqual([
            (OWNER, OWNER, "charge camera", "2026-08-23 14:35:00"),
            (GROUP, GROUP, "Water plants\n\nCheck mail;", "2026-08-06 23:00:00"),
        ], self.rows())

    def test_actual_dispatch_conflict_clarifies_without_model_or_write(self):
        for group in (False, True):
            for text in ("remind me Monday August 23 at 9am to charge camera",
                         "remind me tomorrow at 7.3am to pack lunch",
                         "remind me September 31 at 9am to charge camera"):
                with self.subTest(group=group, text=text):
                    self.route(text, group=group)
                    self.model.assert_not_called()
                    self.assertIn("didn't save a reminder", self.send.call_args.args[1])
                    self.assertEqual([], self.rows())

    def test_actual_range_cancel_uses_current_chat_original_positions(self):
        self.seed()
        self.route("cancel reminders 2-5", group=True)
        self.model.assert_not_called()
        self.assertEqual(GROUP, self.send.call_args.args[0])
        remaining = self.rows()
        self.assertEqual(["synthetic item 1", "synthetic item 6"], [r[2] for r in remaining if r[0] == GROUP])
        self.assertEqual(6, len([r for r in remaining if r[0] == OWNER]))
        self.assertEqual(6, len([r for r in remaining if r[0] == OTHER_GROUP]))
        self.route("delete reminders 3 through 5")
        self.model.assert_not_called()
        self.assertEqual(["synthetic item 1", "synthetic item 2", "synthetic item 6"],
                         [r[2] for r in self.rows() if r[0] == OWNER])

    def test_actual_group_mentions_preserve_invalid_selector(self):
        self.seed()
        before = self.rows()
        for text in ("@Davos cancel reminders 2-", "Davos, cancel reminders 2-",
                     "cancel reminders 2- @Davos", "cancel reminders 2- Davos",
                     "@Davos no search cancel reminders 2-"):
            with self.subTest(text=text):
                self.model.reset_mock()
                main.handle_group_message(OWNER, GROUP, text)
                self.model.assert_not_called()
                self.assertIn("didn't cancel", self.send.call_args.args[1])
                self.assertEqual(before, self.rows())

    def test_actual_invalid_cancel_never_partially_mutates(self):
        self.seed()
        before = self.rows()
        for group in (False, True):
            for selector in ("5-2", "2-5 except 4", "2-", "2-50000", "2-7"):
                with self.subTest(group=group, selector=selector):
                    self.route(f"cancel reminders {selector}", group=group)
                    self.model.assert_not_called()
                    self.assertEqual(before, self.rows())
                    self.assertNotIn("Cancelled:", self.send.call_args.args[1])
                    self.assertNotIn("Cancelled reminders:", self.send.call_args.args[1])

    def test_nonowner_group_cannot_use_native_reminder_mutations(self):
        self.seed()
        before = self.rows()
        for sender in (ADMIN, FRIEND):
            for text in ("remind me in 4h to pack lunch", "cancel reminders 2-5"):
                with self.subTest(sender=sender, text=text):
                    self.route(text, sender=sender, group=True)
                    self.assertEqual(before, self.rows())


if __name__ == "__main__":
    unittest.main()
