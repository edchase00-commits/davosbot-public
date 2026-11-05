"""Synthetic inbound messages exercise actual group dispatch and durable cron writes."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from davosbot import commands, cron_creation, main, permissions, research_cron, tools


OWNER = "+15550000001"
ADMIN = "+15550000002"
FRIEND = "+15550000003"
UNKNOWN = "+15550000004"
GROUP = "0123456789abcdef0123456789abcdef"
OTHER_GROUP = "fedcba9876543210fedcba9876543210"
REAL_IS_OWNER = permissions.is_owner
REAL_IS_ADMIN = permissions.is_admin


class GroupCronCreationPermissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db_path = str(Path(temporary.name) / "cron-tests.sqlite")
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript("""
                CREATE TABLE cron_jobs (
                    id INTEGER PRIMARY KEY, cron_expression TEXT, action_type TEXT,
                    action_payload TEXT, enabled INTEGER DEFAULT 1, created_by TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_run TEXT
                );
                CREATE TABLE bot_log (id INTEGER PRIMARY KEY, sender TEXT, event_type TEXT, payload TEXT);
            """)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(tools, "BOT_DB_PATH", self.db_path))
        stack.enter_context(patch.object(commands, "BOT_DB_PATH", self.db_path))
        stack.enter_context(patch.object(main, "BOT_DB_PATH", self.db_path))
        stack.enter_context(patch.object(cron_creation, "_pending", {}))
        stack.enter_context(patch.object(research_cron, "_pending", {}))
        self.clock = stack.enter_context(patch.object(cron_creation.time, "monotonic", return_value=100))
        for module in (main, commands):
            stack.enter_context(patch.object(module, "is_owner", lambda sender: sender == OWNER))
            stack.enter_context(patch.object(module, "is_admin", lambda sender: sender in {OWNER, ADMIN}))
        stack.enter_context(patch("davosbot.permissions.is_owner", lambda sender: sender == OWNER))
        stack.enter_context(patch("davosbot.permissions.is_admin", lambda sender: sender in {OWNER, ADMIN}))
        self.owner_present = stack.enter_context(patch.object(main, "is_owner_in_chat", return_value=True))
        self.enabled = stack.enter_context(patch.object(main, "is_gc_enabled", return_value=True))
        stack.enter_context(patch.object(main, "is_approved_user", lambda sender: sender in {OWNER, ADMIN, FRIEND}))
        stack.enter_context(patch.object(main, "_is_rate_limited", return_value=False))
        self.rate_limit = stack.enter_context(patch.object(main, "check_rate_limit", return_value=True))
        for name in ("get_persona", "decatur_behavior_fast_reply", "match_skill"):
            stack.enter_context(patch.object(main, name, return_value=None))
        stack.enter_context(patch.object(main, "build_system_prompt", return_value="synthetic system"))
        stack.enter_context(patch.object(main, "build_light_chat_system_prompt", return_value="synthetic system"))
        stack.enter_context(patch.object(main, "get_history", return_value=[]))
        for name in ("save_turn", "extract_and_update_memory", "update_heartbeat", "_log_message_trace", "_log_quality_signal", "log_session_error"):
            stack.enter_context(patch.object(main, name))
        self.errors = stack.enter_context(patch.object(main, "log_error"))
        self.model = stack.enter_context(patch.object(main, "get_response", return_value="MODEL_FALLTHROUGH"))
        stack.enter_context(patch.object(tools, "_get_sports_recap", return_value="SYNTHETIC SCOREBOARD"))
        self.send = stack.enter_context(patch.object(main, "send_message", return_value=True))
        stack.enter_context(patch.dict(main._image_buffer, {}, clear=True))
        stack.enter_context(patch.dict(main._text_buffer, {}, clear=True))

    def ask(self, text, sender=OWNER, chat=GROUP):
        self.send.reset_mock()
        self.model.reset_mock()
        main.handle_message({"sender": sender, "chat_identifier": chat, "text": text})
        self.errors.assert_not_called()
        if not self.send.called:
            return None
        self.send.assert_called_once()
        self.assertEqual(chat, self.send.call_args.args[0])
        self.assertTrue(self.send.call_args.kwargs["is_group"])
        return self.send.call_args.args[1]

    def rows(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(
                "SELECT id, cron_expression, action_type, action_payload, enabled FROM cron_jobs ORDER BY id"
            ).fetchall()

    def assert_saved(self, schedule, action, count=1, chat=GROUP):
        rows = self.rows()
        self.assertEqual(count, len(rows))
        row = rows[-1]
        self.assertEqual((schedule, action), row[1:3])
        self.assertEqual(chat, json.loads(row[3])["recipient"])
        self.assertEqual(1, row[4])
        self.model.assert_not_called()

    def test_owner_complete_requests_preserve_schedule_action_and_current_chat(self):
        cases = (
            ("please send us an inspirational quote every morning at 8am", "08:00", "morning_message"),
            ("could you create a bot health report cron every Friday at 7pm please", "19:00 fri", "drift_check"),
            ("please send us a sports recap every Monday at 6pm", "18:00 mon", "sports_recap"),
        )
        for index, (text, schedule, action) in enumerate(cases, 1):
            with self.subTest(text=text):
                self.assertNotEqual("MODEL_FALLTHROUGH", self.ask("@Davos " + text))
                self.assert_saved(schedule, action, count=index)

    def test_real_owner_gates_accept_normalized_phone_variants(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(permissions, "OWNER_ID", OWNER))
            for module in (main, commands, permissions):
                stack.enter_context(patch.object(module, "is_owner", REAL_IS_OWNER))
                stack.enter_context(patch.object(module, "is_admin", REAL_IS_ADMIN))
            for sender in (OWNER, "15550000001", "5550000001", "(555) 000-0001"):
                with self.subTest(sender=sender):
                    reply = self.ask("@Davos create sports recap cron every Friday at 6pm", sender)
                    self.assertTrue("Created sports recap" in reply or "already exists" in reply)
                    self.assert_saved("18:00 fri", "sports_recap")

    def test_owner_missing_action_time_and_day_accept_same_sender_unmentioned_followups(self):
        self.assertIn("What should", self.ask("@Davos create a weekly cron"))
        self.assertIn("weekday", self.ask("bot health report"))
        self.assertIn("Pacific time", self.ask("Friday"))
        self.assertEqual([], self.rows())
        self.assertIn("scheduled", self.ask("8am please"))
        self.assert_saved("08:00 fri", "drift_check")
        self.assertIn("#1", self.ask("@Davos list crons"))

    def test_owner_polite_time_followup_completes_draft(self):
        self.assertIn("Pacific time", self.ask("@Davos create a daily quote cron"))
        self.assertIn("scheduled", self.ask("let's do 8am please"))
        self.assert_saved("08:00", "morning_message")

    def test_quote_choice_in_pending_draft_does_not_call_market_lookup(self):
        self.assertIn("What should", self.ask("@Davos create new cron"))
        self.assertIn("Pacific time", self.ask("quote"))
        self.assertIn("scheduled", self.ask("8am works for me"))
        self.assert_saved("08:00", "morning_message")

    def test_actual_market_request_keeps_its_route_during_a_cron_draft(self):
        self.ask("@Davos create a health report cron")
        with patch.object(main, "handle_market_query", return_value="SYNTHETIC AAPL QUOTE") as market:
            self.assertEqual("SYNTHETIC AAPL QUOTE", self.ask("@Davos what's the AAPL stock quote?"))
            market.assert_called_once_with("what's the AAPL stock quote?")
        self.assertEqual([], self.rows())
        self.assertIn("scheduled", self.ask("8am"))
        self.assert_saved("08:00", "drift_check")

    def test_new_custom_report_with_negated_stop_never_cancels_an_existing_job(self):
        self.ask("@Davos create a health report cron at 4:20am")
        original = self.rows()
        request = (
            "@Davos set up a new cron job every Tuesday at 4:20 AM listing seven fantasy football "
            "waiver wire targets, reasons and FAAB bids from a 100 dollar budget. "
            "Start this Tuesday and don’t stop until the first Tuesday in February."
        )
        reply = self.ask(request)
        self.assertNotIn("No active cron", reply)
        self.assertEqual(original[0], self.rows()[0])
        self.model.assert_not_called()

    def test_complete_research_with_negated_stop_saves_new_job_and_keeps_existing_job(self):
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 9, 8, 18, tzinfo=research_cron.PACIFIC)
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)
        self.ask("@Davos create a health report cron at 4:20am")
        original = self.rows()[0]
        request = "@Davos Create a weekly fantasy waiver report top 7 with why and FAAB (out of 200), every Tuesday at 12:34 PT starting upcoming Tuesday, and don't stop until first Tuesday in February"
        with patch.object(research_cron, "datetime", Frozen):
            reply = self.ask(request)
        self.assertIn("Saved research cron #2", reply)
        self.assertEqual(original, self.rows()[0])
        self.assert_saved("12:34 tue", "research_report", count=2)
        payload = json.loads(self.rows()[1][3])
        self.assertEqual((7, 200, "2026-09-15", "2027-02-02"), (payload["top_n"], payload["faab_budget"], payload["start_date"], payload["end_date"]))

    def test_negated_stop_is_not_cancellation_but_explicit_cancel_still_works(self):
        self.ask("@Davos create a health report cron at 7am")
        self.assertIn("scheduled", self.ask("@Davos create a quote cron at 8am and don't stop"))
        self.assert_saved("08:00", "morning_message", count=2)
        self.assertEqual([1, 1], [row[4] for row in self.rows()])
        self.assertIn("Disabled cron #2", self.ask("@Davos please cancel cron #2"))
        self.assertEqual([1, 0], [row[4] for row in self.rows()])

    def test_negated_existing_job_commands_neither_cancel_nor_create(self):
        self.ask("@Davos create a quote cron at 8am")
        original = self.rows()
        for text in (
            "Don't stop the daily quote at 8am", "Please do not create a daily quote cron at 8am",
            "Never change cron #1 to 9am",
        ):
            with self.subTest(text=text):
                self.assertEqual("MODEL_FALLTHROUGH", self.ask("@Davos " + text))
                self.assertEqual(original, self.rows())

    def test_custom_start_or_end_dates_are_not_silently_discarded(self):
        for suffix in ("until February", "starting next Tuesday", "end on Friday"):
            with self.subTest(suffix=suffix):
                reply = self.ask("@Davos create a quote cron at 8am " + suffix)
                self.assertIn("start and end dates are not supported", reply)
                self.assertEqual([], self.rows())

    def test_questions_about_repeated_wisdom_do_not_create_or_deny_crons(self):
        questions = (
            "Why do the daily wisdom messages repeat; how are you generating them?",
            "I like the quote, but we don't want the same quote every day. How are you generating it?",
            "How do you create the daily quotes?",
            "Why did you stop the daily quote at 8am?",
        )
        for sender in (OWNER, ADMIN, FRIEND):
            for question in questions:
                with self.subTest(sender=sender, question=question):
                    reply = self.ask("@Davos " + question, sender)
                    self.assertEqual("MODEL_FALLTHROUGH", reply)
                    self.assertEqual([], self.rows())

    def test_admin_sports_complete_natural_request_uses_existing_permission(self):
        self.assertIn("Created sports recap", self.ask("@Davos please send us a sports recap every Friday at 6pm", ADMIN))
        self.assert_saved("18:00 fri", "sports_recap")

    def test_admin_sports_missing_time_and_weekday_can_be_completed(self):
        self.assertIn("weekday", self.ask("@Davos create a weekly sports recap cron at 8am", ADMIN))
        self.assertEqual([], self.rows())
        self.assertIn("Created sports recap", self.ask("Friday please", ADMIN))
        self.assert_saved("08:00 fri", "sports_recap")
        self.assertIn("already exists", self.ask("@Davos create sports recap cron daily at 9am", ADMIN))
        self.assert_saved("08:00 fri", "sports_recap")

    def test_admin_sports_edit_preserves_id_destination_and_duplicate_prevention(self):
        self.ask("@Davos create sports recap cron every Friday at 6pm", ADMIN)
        self.assertIn("Updated sports recap", self.ask("@Davos change sports recap cron to Monday at 7pm", ADMIN))
        self.assert_saved("19:00 mon", "sports_recap")
        self.assertEqual(1, self.rows()[0][0])

    def test_other_senders_and_chats_cannot_complete_an_owner_draft(self):
        self.ask("@Davos create a quote cron")
        for sender, chat in ((ADMIN, GROUP), (FRIEND, GROUP), (OWNER, OTHER_GROUP), (UNKNOWN, GROUP)):
            with self.subTest(sender=sender, chat=chat):
                self.assertIsNone(self.ask("8am", sender, chat))
                self.assertEqual([], self.rows())
        self.assertIn("scheduled", self.ask("8am"))
        self.assert_saved("08:00", "morning_message")

    def test_non_owner_cannot_create_generic_crons_or_change_sports_draft_action(self):
        for sender in (ADMIN, FRIEND):
            self.assertIn("the owner-only", self.ask("@Davos create quote cron at 8am", sender))
            self.assertEqual([], self.rows())
        self.assertIn("admin-only", self.ask("@Davos create sports recap cron at 8am", FRIEND))
        self.ask("@Davos create sports recap cron", ADMIN)
        self.assertIn("can only schedule a sports recap", self.ask("bot health report at 8am", ADMIN))
        self.assertEqual([], self.rows())
        self.assertIn("Created sports recap", self.ask("8am", ADMIN))
        self.assert_saved("08:00", "sports_recap")

    def test_draft_cancel_and_normal_mentioned_command_keep_existing_jobs_safe(self):
        self.ask("@Davos create a quote cron at 7am")
        self.ask("@Davos create a health report cron")
        self.assertIn("pong", self.ask("@Davos ping"))
        self.assertIn("Cancelled", self.ask("never mind"))
        self.assertIsNone(self.ask("8am"))
        self.assert_saved("07:00", "morning_message")
        self.ask("@Davos create sports recap cron", ADMIN)
        self.assertIn("Cancelled", self.ask("cancel cron draft", ADMIN))
        self.assertIsNone(self.ask("8am", ADMIN))
        self.assertEqual(1, len(self.rows()))

    def test_pending_continuations_still_require_current_access_and_rate_limit(self):
        self.ask("@Davos create sports recap cron", ADMIN)
        with patch.object(main, "is_admin", return_value=False):
            self.assertIsNone(self.ask("8am", ADMIN))
        with patch.object(main, "is_approved_user", return_value=False):
            self.assertIsNone(self.ask("8am", ADMIN))
        self.rate_limit.return_value = False
        self.assertIn("message limit", self.ask("8am", ADMIN))
        self.assertEqual([], self.rows())
        self.rate_limit.return_value = True
        self.assertIn("Created sports recap", self.ask("8am", ADMIN))
        self.assert_saved("08:00", "sports_recap")

    def test_ambiguous_time_and_timezone_do_not_destroy_admin_sports_draft(self):
        self.assertIn("AM or PM", self.ask("@Davos create sports recap cron at 8", ADMIN))
        self.assertIn("Pacific time", self.ask("8am ET", ADMIN))
        self.assertEqual([], self.rows())
        self.assertIn("Created sports recap", self.ask("@Davos 8pm PT", ADMIN))
        self.assert_saved("20:00", "sports_recap")

    def test_expired_disabled_absent_owner_and_unmentioned_new_requests_stay_silent(self):
        self.assertIsNone(self.ask("create quote cron at 8am"))
        self.ask("@Davos create quote cron")
        self.enabled.return_value = False
        self.assertIsNone(self.ask("8am"))
        self.enabled.return_value = True
        self.owner_present.return_value = False
        self.assertIsNone(self.ask("8am"))
        self.owner_present.return_value = True
        self.clock.return_value = 401
        self.assertIsNone(self.ask("8am"))
        self.assertEqual([], self.rows())

    def test_unrelated_and_side_effect_followups_do_not_consume_draft(self):
        self.ask("@Davos create quote cron")
        for text in ("yes", "send Pat a message at 8am", "run a shell script at 8am", "hello", "8am and buy lunch"):
            with self.subTest(text=text):
                self.assertIsNone(self.ask(text))
                self.assertEqual([], self.rows())
        self.assertIn("scheduled", self.ask("8am"))
        self.assert_saved("08:00", "morning_message")

    def test_unsupported_schedules_fail_closed_without_substitute_jobs(self):
        for text in (
            "create quote cron weekdays at 8am", "create quote cron every Monday and Friday at 8am",
            "create quote cron every second Friday at 8am", "create quote cron at 8am ET",
            "create quote cron at 8am and 9pm", "create quote cron for another chat at 8am",
            "create shell cron daily at 8am",
        ):
            with self.subTest(text=text):
                reply = self.ask("@Davos " + text)
                self.assertIsInstance(reply, str)
                self.assertNotEqual("MODEL_FALLTHROUGH", reply)
                self.assertEqual([], self.rows())


if __name__ == "__main__":
    unittest.main()
