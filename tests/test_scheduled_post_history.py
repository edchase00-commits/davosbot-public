"""Successful cron sends become durable context for actual inbound follow-ups."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from davosbot import commands, main, memory, permissions, tools
from davosbot.text_safety import normalize_bot_text
from cron_occurrence_fixtures import install_legacy_timing_schema


OWNER = "+15550000001"
FRIEND = "+15550000003"
GROUP = "0123456789abcdef0123456789abcdef"
OTHER_GROUP = "fedcba9876543210fedcba9876543210"


class _FrozenDatetime(datetime):
    current = datetime.now(timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


class ScheduledPostHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db_path = str(Path(temporary.name) / "scheduled-history.sqlite")
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript("""
                CREATE TABLE cron_jobs (
                    id INTEGER PRIMARY KEY, cron_expression TEXT, action_type TEXT,
                    action_payload TEXT, enabled INTEGER DEFAULT 1, created_by TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_run TEXT
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, sender TEXT, role TEXT, content TEXT, ts TEXT
                );
                CREATE TABLE bot_log (id INTEGER PRIMARY KEY, sender TEXT, event_type TEXT, payload TEXT);
            """)
        self.stack = ExitStack()
        install_legacy_timing_schema(self.db_path)
        self.addCleanup(self.stack.close)
        for module in (main, memory, commands, tools):
            self.stack.enter_context(patch.object(module, "BOT_DB_PATH", self.db_path))
        self.stack.enter_context(patch.object(main, "_LAST_CRON_CHECK", 0))
        self.now = datetime.now(timezone.utc)
        _FrozenDatetime.current = self.now
        self.stack.enter_context(patch("datetime.datetime", _FrozenDatetime))
        self.quote = self.stack.enter_context(patch.object(tools, "_get_inspirational_quote", return_value="Notice the useful work in front of you."))
        self.stack.enter_context(patch.object(tools, "_get_sports_recap", return_value="Sports recap: synthetic team won 4-2."))
        self.stack.enter_context(patch.object(commands, "_cmd_drift", return_value="Synthetic health report: all checks passed."))
        self.send = self.stack.enter_context(patch.object(main, "send_message", return_value=True))

    def insert_job(self, action="morning_message", recipient=GROUP, *, test_job=False):
        expression = "TEST_2MIN" if test_job else self.now.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%H:%M")
        payload = {"recipient": recipient, "intro": "A thought for today:", "intro_mode": "fixed"}
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.execute(
                "INSERT INTO cron_jobs (cron_expression, action_type, action_payload, created_by, created_at) "
                "VALUES (?, ?, ?, ?, datetime('now', '-3 minutes'))",
                (expression, action, json.dumps(payload), OWNER),
            )
            conn.commit()
            return cursor.lastrowid

    def job_state(self, job_id=1):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute("SELECT last_run, enabled FROM cron_jobs WHERE id = ?", (job_id,)).fetchone()

    def assert_saved_sent_post(self, recipient, *, recovery_mode="none"):
        sent = self.send.call_args.args[1]
        self.assertEqual([{"role": "assistant", "content": normalize_bot_text(sent)}], memory.get_history(recipient))
        self.assertEqual(recipient, self.send.call_args.args[0])
        self.assertEqual(recipient == GROUP, self.send.call_args.kwargs["is_group"])
        self.assertEqual(recovery_mode, self.send.call_args.kwargs["recovery_mode"])
        self.assertIsNotNone(self.job_state()[0])

    def test_each_native_post_is_recorded_only_in_its_actual_destination(self):
        for action in ("morning_message", "drift_check", "sports_recap"):
            for recipient in (GROUP, OWNER):
                with self.subTest(action=action, recipient=recipient):
                    with closing(sqlite3.connect(self.db_path)) as conn:
                        conn.execute("DELETE FROM cron_jobs")
                        conn.execute("DELETE FROM messages")
                        conn.commit()
                    self.send.reset_mock()
                    main._LAST_CRON_CHECK = 0
                    self.insert_job(action, recipient)
                    main._check_cron_jobs()
                    self.send.assert_called_once()
                    self.assert_saved_sent_post(recipient)
                    self.assertEqual([], memory.get_history(OTHER_GROUP))
                    self.assertEqual([], memory.get_history(OWNER if recipient == GROUP else GROUP))

    def test_failed_or_raised_send_does_not_save_or_advance_job(self):
        self.insert_job()
        for result in (False, RuntimeError("synthetic send failure")):
            with self.subTest(result=type(result).__name__):
                self.send.side_effect = result if isinstance(result, Exception) else None
                self.send.return_value = result
                main._LAST_CRON_CHECK = 0
                main._check_cron_jobs()
                self.assertEqual([], memory.get_history(GROUP))
                self.assertIsNone(self.job_state()[0])

    def test_throttled_target_minute_is_caught_up_and_followup_history_is_recorded_once(self):
        self.now = datetime(2026, 9, 9, 13, 30, tzinfo=timezone.utc)
        self.insert_job()
        # The middle poll is inside 06:30 but is throttled; the next is 06:31.
        for seconds, monotonic in ((-10, 1000), (30, 1040), (60, 1070)):
            _FrozenDatetime.current = self.now + timedelta(seconds=seconds)
            with patch.object(main.time, "monotonic", return_value=monotonic):
                main._check_cron_jobs()
        self.send.assert_called_once()
        self.assert_saved_sent_post(GROUP)
        # A repeated check after the successful occurrence must stay silent.
        with patch.object(main.time, "monotonic", return_value=1140):
            main._check_cron_jobs()
        self.send.assert_called_once()

    def test_successful_post_matches_outbound_normalization_without_duplicate_history(self):
        self.insert_job(action="sports_recap")
        with patch.object(tools, "_get_sports_recap", return_value="  Synthetic  recap\n  Team won.  "):
            main._check_cron_jobs()
        self.assertEqual([{"role": "assistant", "content": "Synthetic recap\nTeam won."}], memory.get_history(GROUP))
        _FrozenDatetime.current = datetime.fromisoformat(self.job_state()[0]).replace(tzinfo=timezone.utc)
        main._LAST_CRON_CHECK = 0
        main._check_cron_jobs()
        self.send.assert_called_once()
        self.assertEqual(1, len(memory.get_history(GROUP)))

    def test_missing_recipient_or_unknown_action_does_not_invent_history(self):
        self.insert_job(recipient="")
        self.insert_job(action="unknown")
        main._check_cron_jobs()
        self.send.assert_not_called()
        self.assertEqual([], memory.get_history(GROUP))

    def test_history_failure_keeps_success_checkpoint_and_never_resends(self):
        self.insert_job()
        with patch.object(main, "save_turn", side_effect=RuntimeError("private synthetic detail")) as save, patch.object(main.logger, "warning") as warning:
            main._check_cron_jobs()
            save.assert_called_once()
            last_run, enabled = self.job_state()
            self.assertIsNotNone(last_run)
            self.assertEqual(1, enabled)
            self.assertTrue(warning.called)
            self.assertNotIn("private synthetic detail", str(warning.call_args))
            # Recheck the persisted occurrence independently of the throttle.
            _FrozenDatetime.current = datetime.fromisoformat(last_run).replace(tzinfo=timezone.utc)
            main._LAST_CRON_CHECK = 0
            main._check_cron_jobs()
        self.send.assert_called_once()
        self.assertEqual([], memory.get_history(GROUP))

    def test_test_job_records_success_and_preserves_one_shot_semantics(self):
        self.insert_job(test_job=True)
        main._check_cron_jobs()
        self.assert_saved_sent_post(GROUP, recovery_mode="inline")
        self.assertEqual(0, self.job_state()[1])
        main._LAST_CRON_CHECK = 0
        main._check_cron_jobs()
        self.send.assert_called_once()

    def test_failed_test_job_remains_unsaved_but_disabled(self):
        self.insert_job(test_job=True)
        self.send.return_value = False
        main._check_cron_jobs()
        self.assertEqual([], memory.get_history(GROUP))
        self.assertEqual(0, self.job_state()[1])

    def test_scheduled_quote_reaches_actual_group_followup_with_speaker_attribution(self):
        self.insert_job()
        memory.save_turn(GROUP, "user", f"{FRIEND}: Who won the game?")
        memory.save_turn(GROUP, "assistant", "Synthetic team won yesterday.")
        main._check_cron_jobs()
        delivered = normalize_bot_text(self.send.call_args.args[1])
        self.assertIn(self.quote.return_value, delivered)
        for module in (main, commands, permissions):
            self.stack.enter_context(patch.object(module, "is_owner", lambda sender: sender == OWNER))
            self.stack.enter_context(patch.object(module, "is_admin", lambda sender: sender == OWNER))
        for name in ("is_owner_in_chat", "is_gc_enabled", "is_approved_user", "check_rate_limit"):
            self.stack.enter_context(patch.object(main, name, return_value=True))
        self.stack.enter_context(patch.object(main, "_is_rate_limited", return_value=False))
        for name in ("get_persona", "decatur_behavior_fast_reply", "match_skill"):
            self.stack.enter_context(patch.object(main, name, return_value=None))
        for name in ("build_system_prompt", "build_light_chat_system_prompt"):
            self.stack.enter_context(patch.object(main, name, return_value="synthetic system"))
        for name in ("extract_and_update_memory", "update_heartbeat", "_log_message_trace", "_log_quality_signal", "log_session_error"):
            self.stack.enter_context(patch.object(main, name))
        errors = self.stack.enter_context(patch.object(main, "log_error"))
        model = self.stack.enter_context(patch.object(main, "get_response", return_value="It means to focus on the useful task in front of you."))
        main.handle_message({"sender": FRIEND, "chat_identifier": GROUP, "text": "@Davos what does that mean?"})
        errors.assert_not_called()
        model.assert_called_once()
        self.assertEqual({"role": "assistant", "content": delivered}, model.call_args.args[1][-1])
        self.assertEqual("what does that mean?", model.call_args.args[2])
        self.assertEqual(GROUP, model.call_args.kwargs["originating_chat_id"])
        history = memory.get_history(GROUP)
        self.assertEqual({"role": "user", "content": f"{FRIEND}: what does that mean?"}, history[-2])
        self.assertEqual([], memory.get_history(FRIEND))


if __name__ == "__main__":
    unittest.main()
