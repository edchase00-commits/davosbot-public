import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import Mock, patch

from davosbot import cron_creation, main, tools


OWNER = "+15550000001"
GROUP = "0123456789abcdef0123456789abcdef"


class CronCreationPermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "cron-tests.sqlite")
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript("""
                CREATE TABLE cron_jobs (
                    id INTEGER PRIMARY KEY, cron_expression TEXT, action_type TEXT,
                    action_payload TEXT, enabled INTEGER DEFAULT 1, created_by TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_run TEXT
                );
                CREATE TABLE bot_log (
                    id INTEGER PRIMARY KEY, sender TEXT, event_type TEXT, payload TEXT
                );
            """)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(tools, "BOT_DB_PATH", self.db_path))
        self.stack.enter_context(patch.object(cron_creation, "_pending", {}))
        self.clock = self.stack.enter_context(patch.object(cron_creation.time, "monotonic", return_value=100))
        self.stack.enter_context(patch("davosbot.permissions.is_owner", lambda sender: sender == OWNER))
        self.stack.enter_context(patch("davosbot.permissions.is_admin", lambda sender: sender in {OWNER, "admin"}))
        self.stack.enter_context(patch.object(tools, "_get_sports_recap", return_value="TEST SCOREBOARD"))

    def rows(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(
                "SELECT cron_expression, action_type, action_payload FROM cron_jobs ORDER BY id",
            ).fetchall()

    def ask(self, text, sender=OWNER, chat=OWNER):
        return tools._schedule_cron_from_text(sender, text, originating_chat_id=chat)

    def route(self, text, group=False):
        """Use real DM/GC dispatch and cron handlers; stub unrelated services and sends."""
        with ExitStack() as stack:
            for name in (
                "_get_buffered_image", "_handle_screenshot_issue_log", "_handle_priority_intake_command",
                "_handle_self_status_question", "_handle_model_status_question", "_handle_natural_model_request",
                "handle_private_send_confirmation", "handle_private_send_request", "get_persona",
                "handle_style_directive_message", "_fast_chat_reply", "_describe_cron_from_text",
                "handle_group_command", "_non_owner_length_rejection",
                "_market_fast_reply", "_viral_banter_reply", "_log_group_error_intake_if_needed",
            ):
                stack.enter_context(patch.object(main, name, return_value=None))
            stack.enter_context(patch.object(main, "is_owner", lambda sender: sender == OWNER))
            stack.enter_context(patch.object(main, "is_admin", lambda sender: sender in {OWNER, "admin"}))
            stack.enter_context(patch.object(main, "is_owner_in_chat", return_value=True))
            stack.enter_context(patch.object(main, "is_gc_enabled", return_value=True))
            stack.enter_context(patch.object(main, "is_ufc_fight_card_request", return_value=False))
            stack.enter_context(patch.object(main, "looks_like_tone_feedback", return_value=False))
            stack.enter_context(patch.object(main, "save_turn"))
            stack.enter_context(patch.object(main, "get_response", side_effect=AssertionError("Cron intake should not use LLM")))
            send = stack.enter_context(patch.object(main, "send_message", return_value=True))
            if group:
                main.handle_group_message(OWNER, GROUP, "@Davos " + text)
                self.assertEqual(GROUP, send.call_args.args[0])
                self.assertTrue(send.call_args.kwargs["is_group"])
            else:
                main.handle_dm(OWNER, text)
                self.assertEqual(OWNER, send.call_args.args[0])
            return send.call_args.args[1]

    def test_owner_dm_and_group_complete_new_cron_action_time_with_correct_origin(self):
        for group in (False, True):
            with self.subTest(group=group), patch.object(tools, "_schedule_cron", return_value="TEST SAVED") as schedule:
                self.assertIn("What should", self.route("create a new cron", group))
                self.assertIn("Pacific time", self.route("bot health report", group))
                self.assertEqual("TEST SAVED", self.route("8am", group))
                schedule.assert_called_once_with(
                    "08:00", "", action="drift_check", day_of_week="", intro_mode="",
                    originating_chat_id=GROUP if group else OWNER,
                )

    def test_real_dispatch_persists_only_complete_cron_in_group(self):
        self.route("create new cron", group=True)
        self.route("quote", group=True)
        self.assertEqual([], self.rows())
        self.assertIn("scheduled", self.route("8am", group=True))

        expr, action, payload = self.rows()[0]
        self.assertEqual(("08:00", "morning_message"), (expr, action))
        self.assertEqual(GROUP, json.loads(payload)["recipient"])
        self.assertIsNone(self.ask("8am", chat=GROUP))
        self.assertEqual(1, len(self.rows()))

    def test_nonowner_and_other_chat_cannot_consume_owner_draft(self):
        self.ask("create new quote cron", chat=GROUP)
        self.assertIsNone(self.ask("8am", sender="admin", chat=GROUP))
        self.assertIsNone(self.ask("8am", sender="friend", chat=GROUP))
        self.assertIsNone(self.ask("8am", chat=OWNER))
        self.assertIn("the owner-only", self.ask("create new quote cron at 8am", sender="admin", chat=GROUP))
        self.assertEqual([], self.rows())
        self.assertIn("scheduled", self.ask("8am", chat=GROUP))
        self.assertEqual(GROUP, json.loads(self.rows()[0][2])["recipient"])

    def test_expired_draft_and_unrelated_commands_do_not_trigger_creation(self):
        self.ask("create a quote cron")
        for command in ("help", "list crons", "cancel 7", "weather tomorrow", "fix yourself", "yes"):
            with self.subTest(command=command):
                self.assertIsNone(self.ask(command))
        self.clock.return_value = 401
        self.assertIsNone(self.ask("8am"))
        self.assertEqual([], self.rows())

    def test_followup_cadence_and_action_preserve_collected_fields(self):
        self.ask("create a new cron at 8am")
        self.assertIn("scheduled", self.ask("daily quote"))
        self.assertEqual(("08:00", "morning_message"), self.rows()[0][:2])
        self.ask("create a health report cron")
        self.assertIn("scheduled", self.ask("every Friday at 9am"))
        self.assertEqual(("09:00 fri", "drift_check"), self.rows()[1][:2])

    def test_weekly_missing_day_requires_followup(self):
        self.assertIn("Which weekday", self.ask("create a weekly quote cron at 8am"))
        self.assertEqual([], self.rows())
        self.assertIn("scheduled", self.ask("Friday"))
        self.assertEqual("08:00 fri", self.rows()[0][0])

    def test_bare_hour_asks_am_pm_and_keeps_draft(self):
        self.route("create a quote cron")
        self.assertIn("AM or PM", self.route("8"))
        self.assertEqual([], self.rows())
        self.assertIn("scheduled", self.route("8pm"))
        self.assertEqual("20:00", self.rows()[0][0])
        self.assertIn("AM or PM", self.ask("create a bot health report cron at 8"))
        self.assertIn("scheduled", self.ask("bot health report at 08:00"))
        self.assertEqual("08:00", self.rows()[1][0])

    def test_draft_cancel_routing_never_disables_existing_cron(self):
        self.ask("create a quote cron at 8am", chat=GROUP)
        for command in ("cancel new cron", "cancel cron draft"):
            with self.subTest(command=command):
                self.route("create a health report cron", group=True)
                self.assertIn("Cancelled the new cron draft", self.route(command, group=True))
                self.assertIsNone(self.ask("9am", chat=GROUP))
                self.assertIn("No new cron draft", self.route(command, group=True))
                with closing(sqlite3.connect(self.db_path)) as conn:
                    self.assertEqual([(1,)], conn.execute("SELECT enabled FROM cron_jobs").fetchall())
        self.ask("create a health report cron", chat=GROUP)
        self.assertIn("owner-only", tools._cancel_cron_from_text("admin", "cancel new cron", GROUP))
        self.assertIn("No new cron draft", tools._cancel_cron_from_text(OWNER, "cancel new cron", OWNER))
        self.assertIn("scheduled", self.ask("9am", chat=GROUP))

    def test_each_friday_noon_and_midnight_are_supported(self):
        for phrase, expected in (
            ("give me a motivational quote each Friday at 7am", "07:00 fri"),
            ("daily quote noon", "12:00"),
            ("nightly quote midnight", "00:00"),
            ("create a quote cron for Friday at 8am", "08:00 fri"),
        ):
            with self.subTest(phrase=phrase):
                self.assertIn("scheduled", self.ask(phrase))
                self.assertEqual(expected, self.rows()[-1][0])

    def test_unsupported_and_ambiguous_requests_never_write(self):
        phrases = (
            "create weather cron every day at 8am", "create reminder cron at 8am",
            "create a backup cron at midnight", "create quote cron weekdays at 8am",
            "create quote cron every second Friday at 8am", "create quote cron every 2nd Friday at 8am",
            "create quote cron each second Friday at 8am", "create quote cron every day except Friday at 8am",
            "create quote cron daily on Friday at 8am",
            "create quote cron every Monday and Friday at 8am", "create quote cron every two hours at 8am",
            "create quote cron at 8 and 9pm", "create quote cron at 8am and 9",
            "create quote cron between 8 and 9pm", "create quote cron at 8am and 9pm",
            "create quote cron at 13pm", "create quote cron at 0am", "create quote cron at 8am ET",
            "create quote cron at 8am America/New_York", "create quote cron at 8am London time",
            "create quote cron for Cole at 8am", "create quote cron for another chat at 8am",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                reply = self.ask(phrase)
                self.assertIsInstance(reply, str)
                self.assertNotIn("scheduled", reply)
                self.assertEqual([], self.rows())

    def test_invalid_timezone_followup_clarifies_without_losing_valid_draft(self):
        self.ask("create quote cron")
        self.assertIn("Pacific time", self.ask("8am ET"))
        self.assertEqual([], self.rows())
        self.assertIn("scheduled", self.ask("8am PT"))

    def test_single_quoted_greeting_weekday_stays_content(self):
        for greeting in ("Happy Tuesday boys", "It's Friday boys", "Let's win Monday"):
            with self.subTest(greeting=greeting):
                self.assertIn("scheduled", self.ask(f"create daily morning cron at 8am say '{greeting}'"))
                expr, action, payload = self.rows()[-1]
                self.assertEqual("08:00", expr)
                self.assertEqual(greeting, json.loads(payload)["intro"])

    def test_sports_missing_time_from_main_creates_draft_and_finishes_once(self):
        self.assertIn("Pacific time", self.route("create a sports recap cron", group=True))
        self.assertEqual([], self.rows())
        self.assertIn("Created sports recap", self.route("8am", group=True))
        expr, action, payload = self.rows()[0]
        self.assertEqual(("08:00", "sports_recap"), (expr, action))
        self.assertEqual(GROUP, json.loads(payload)["recipient"])

    def test_sports_weekday_creation_edit_and_dedup_preserved(self):
        created = tools._sports_recap_cron_from_text(
            "admin", "create sports recap cron every Friday at 6pm", originating_chat_id=GROUP,
        )
        self.assertIn("every Fri", created)
        self.assertEqual("18:00 fri", self.rows()[0][0])
        edited = tools._sports_recap_cron_from_text(
            "admin", "change sports cron to Monday at 7pm", originating_chat_id=GROUP,
        )
        self.assertIn("every Mon", edited)
        self.assertEqual("19:00 mon", self.rows()[0][0])
        tools._sports_recap_cron_from_text("admin", "fix the sports cron", originating_chat_id=GROUP)
        self.assertEqual("19:00 mon", self.rows()[0][0])
        duplicate = tools._sports_recap_cron_from_text(
            "admin", "create sports recap cron at 8pm", originating_chat_id=GROUP,
        )
        self.assertIn("already exists", duplicate)
        self.assertEqual(1, len(self.rows()))

    def test_sports_unsupported_schedule_never_inserts_or_updates(self):
        phrases = (
            "each second Friday at 8am", "every day except Friday at 8am", "daily on Friday at 8am",
            "at 8 and 9pm", "at 8am America/New_York",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.route("create sports recap cron " + phrase, group=True)
                self.assertEqual([], self.rows())
        self.route("create sports recap cron every Friday at 6pm", group=True)
        original = self.rows()
        for phrase in phrases + ("at 8",):
            with self.subTest(edit=phrase):
                self.route("change sports recap cron " + phrase, group=True)
                self.assertEqual(original, self.rows())

    def test_direct_sports_creation_supersedes_only_same_chat_draft(self):
        self.ask("create inspirational quote cron")
        self.route("create inspirational quote cron", group=True)
        self.assertIn("Created sports recap", self.route("create sports recap cron every Friday at 8am", group=True))
        self.assertIsNone(self.ask("9am", chat=GROUP))
        self.assertEqual(1, len(self.rows()))
        self.assertIn("scheduled", self.ask("9am"))
        self.assertEqual(OWNER, json.loads(self.rows()[1][2])["recipient"])


if __name__ == "__main__":
    unittest.main()
