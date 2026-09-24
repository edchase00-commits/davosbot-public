import json
import gc
import inspect
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from davosbot import tools
OWNER = "+15550000001"


class CronEditingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "davosbot.db")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE cron_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    cron_expression TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    action_payload TEXT,
                    enabled INTEGER DEFAULT 1,
                    created_by TEXT,
                    last_run TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO cron_jobs (cron_expression, action_type, action_payload, enabled, created_by) VALUES (?, ?, ?, 1, 'owner')",
                ("06:30", "morning_message", json.dumps({"recipient": "chat-a", "intro": "Happy Tuesday boys!"})),
            )
            conn.execute(
                """
                CREATE TABLE bot_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    sender TEXT,
                    event_type TEXT,
                    payload TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
        self.patchers = [
            patch.object(tools, "BOT_DB_PATH", self.db_path),
            patch("davosbot.permissions.is_owner", lambda sender: sender == OWNER),
            patch("davosbot.permissions.is_admin", lambda sender: sender in {OWNER, "admin"}),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        gc.collect()
        self.tmp.cleanup()

    def _row(self, cron_id=1):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT cron_expression, action_type, action_payload, enabled FROM cron_jobs WHERE id = ?",
                (cron_id,),
            ).fetchone()
        finally:
            conn.close()

    def _insert_cron(self, expr, recipient, action="morning_message", payload_extra=None):
        payload = {"recipient": recipient}
        if payload_extra:
            payload.update(payload_extra)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO cron_jobs (cron_expression, action_type, action_payload, enabled, created_by) VALUES (?, ?, ?, 1, 'owner')",
                (expr, action, json.dumps(payload)),
            )
            conn.commit()
        finally:
            conn.close()

    def test_edit_cron_rotates_greeting_without_changing_recipient(self):
        reply = tools._edit_cron(1, sender=OWNER, intro_mode="rotate")

        expr, action, raw, enabled = self._row()
        payload = json.loads(raw)
        self.assertIn("Updated cron #1", reply)
        self.assertEqual("06:30", expr)
        self.assertEqual("morning_message", action)
        self.assertEqual(1, enabled)
        self.assertEqual("chat-a", payload["recipient"])
        self.assertEqual("rotate", payload["intro_mode"])
        self.assertNotIn("intro", payload)

    def test_edit_cron_reports_already_matching_without_rewriting(self):
        tools._edit_cron(1, sender=OWNER, intro_mode="rotate")
        before = self._row()

        reply = tools._edit_cron(1, sender=OWNER, intro_mode="rotate")
        after = self._row()

        self.assertIn("already matches", reply)
        self.assertNotIn("Updated cron #1", reply)
        self.assertEqual(before, after)

    def test_edit_cron_from_text_can_update_single_current_chat_job(self):
        reply = tools._edit_cron_from_text(
            OWNER,
            "change the morning job to 7:15am and rotate greeting",
            originating_chat_id="chat-a",
        )

        expr, action, raw, enabled = self._row()
        payload = json.loads(raw)
        self.assertIn("Updated cron #1", reply)
        self.assertEqual("07:15", expr)
        self.assertEqual("rotate", payload["intro_mode"])

    def test_edit_cron_from_text_requires_id_when_not_current_chat(self):
        reply = tools._edit_cron_from_text(
            OWNER,
            "change the morning job to 7:15am",
            originating_chat_id="chat-b",
        )

        self.assertIn("No active cron job found in this chat", reply)

    def test_edit_cron_by_id_changes_day_time_and_intro(self):
        reply = tools._edit_cron_from_text(
            OWNER,
            'change cron #1 to 8pm friday greeting "Morning, no weekday lies"',
            originating_chat_id="chat-b",
        )

        expr, action, raw, enabled = self._row()
        payload = json.loads(raw)
        self.assertIn("Updated cron #1", reply)
        self.assertEqual("20:00 fri", expr)
        self.assertEqual("Morning, no weekday lies", payload["intro"])
        self.assertEqual("fixed", payload["intro_mode"])

    def test_short_edit_by_bare_id_changes_time(self):
        reply = tools._edit_cron_from_text(
            OWNER,
            "set #1 to 8pm",
            originating_chat_id="chat-b",
        )

        expr, action, raw, enabled = self._row()
        self.assertIn("Updated cron #1", reply)
        self.assertEqual("20:00", expr)
        self.assertEqual("morning_message", action)
        self.assertEqual(1, enabled)

    def test_make_cron_with_id_routes_to_edit_not_schedule(self):
        parsed = tools._parse_cron_schedule_command("make cron #1 8pm")
        reply = tools._edit_cron_from_text(
            OWNER,
            "make cron #1 8pm",
            originating_chat_id="chat-a",
        )

        self.assertIsNone(parsed)
        self.assertIn("Updated cron #1", reply)
        self.assertEqual("20:00", self._row()[0])

    def test_make_cron_with_id_changes_action(self):
        reply = tools._edit_cron_from_text(
            OWNER,
            "make #1 a drift check",
            originating_chat_id="chat-a",
        )

        self.assertIn("Updated cron #1", reply)
        self.assertEqual("drift_check", self._row()[1])

    def test_named_morning_one_disambiguates_current_chat_jobs(self):
        self._insert_cron("09:00", "chat-a", action="drift_check")

        reply = tools._edit_cron_from_text(
            OWNER,
            "make the morning one 7am",
            originating_chat_id="chat-a",
        )

        self.assertIn("Updated cron #1", reply)
        self.assertEqual("07:00", self._row(cron_id=1)[0])
        self.assertEqual("09:00", self._row(cron_id=2)[0])

    def test_weekday_in_old_greeting_does_not_change_cadence(self):
        reply = tools._edit_cron_from_text(
            OWNER,
            "fix the morning job so it doesn't say Happy Tuesday anymore and rotate greeting",
            originating_chat_id="chat-a",
        )

        expr, action, raw, enabled = self._row()
        payload = json.loads(raw)
        self.assertIn("Updated cron #1", reply)
        self.assertEqual("06:30", expr)
        self.assertEqual("rotate", payload["intro_mode"])

    def test_edit_cron_does_not_reenable_disabled_jobs(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE cron_jobs SET enabled = 0 WHERE id = 1")
            conn.commit()
        finally:
            conn.close()

        reply = tools._edit_cron(1, sender=OWNER, time_pt="7:15am")

        expr, action, raw, enabled = self._row()
        self.assertIn("disabled", reply)
        self.assertEqual("06:30", expr)
        self.assertEqual(0, enabled)

    def test_schedule_cron_from_text_creates_daily_current_chat_job(self):
        reply = tools._schedule_cron_from_text(
            OWNER,
            "create a daily cron at 7:45am and rotate greeting",
            originating_chat_id="chat-a",
        )

        expr, action, raw, enabled = self._row(cron_id=2)
        payload = json.loads(raw)
        self.assertIn("scheduled", reply)
        self.assertEqual("07:45", expr)
        self.assertEqual("morning_message", action)
        self.assertEqual(1, enabled)
        self.assertEqual("chat-a", payload["recipient"])
        self.assertEqual("rotate", payload["intro_mode"])

    def test_schedule_cron_from_text_asks_for_missing_time(self):
        reply = tools._schedule_cron_from_text(
            OWNER,
            "create a new morning cron",
            originating_chat_id="chat-a",
        )

        self.assertIn("need a Pacific time", reply)
        self.assertIsNone(self._row(cron_id=2))

    def test_schedule_cron_from_text_is_owner_only(self):
        reply = tools._schedule_cron_from_text(
            "admin",
            "create a daily cron at 7:45am",
            originating_chat_id="chat-a",
        )

        self.assertIn("the owner-only", reply)
        self.assertIsNone(self._row(cron_id=2))

    def test_schedule_cron_from_text_does_not_handle_edit_phrases(self):
        reply = tools._schedule_cron_from_text(
            OWNER,
            "change the morning cron to 7:45am",
            originating_chat_id="chat-a",
        )

        self.assertIsNone(reply)

    def test_schedule_cron_from_basic_chat_supports_nightly_quote(self):
        reply = tools._schedule_cron_from_text(
            OWNER,
            "send me a quote nightly at 9pm",
            originating_chat_id="chat-a",
        )

        expr, action, raw, enabled = self._row(cron_id=2)
        self.assertIn("scheduled", reply)
        self.assertEqual("21:00", expr)
        self.assertEqual("morning_message", action)
        self.assertEqual("chat-a", json.loads(raw)["recipient"])
        self.assertEqual(1, enabled)

    def test_schedule_short_daily_quote_without_at(self):
        reply = tools._schedule_cron_from_text(
            OWNER,
            "daily quote 7am",
            originating_chat_id="chat-a",
        )

        self.assertIn("scheduled", reply)
        self.assertEqual("07:00", self._row(cron_id=2)[0])

    def test_schedule_cron_supports_noon_and_midnight(self):
        noon = tools._parse_cron_schedule_command("daily quote at noon")
        midnight = tools._parse_cron_schedule_command("nightly quote at midnight")

        self.assertEqual("12:00", noon["time_pt"])
        self.assertEqual("00:00", midnight["time_pt"])

    def test_recurring_reminder_is_not_miscreated_as_quote_cron(self):
        reply = tools._schedule_cron_from_text(
            OWNER,
            "every day at 7 remind me to stretch",
            originating_chat_id="chat-a",
        )

        self.assertIsNone(reply)
        self.assertIsNone(self._row(cron_id=2))

    def test_natural_cron_cancel_uses_time_and_current_chat(self):
        self._insert_cron("09:00", "chat-a")

        reply = tools._cancel_cron_from_text(
            OWNER,
            "cancel the 6:30 daily",
            originating_chat_id="chat-a",
        )

        self.assertIn("Disabled cron #1", reply)
        self.assertEqual(0, self._row(cron_id=1)[3])
        self.assertEqual(1, self._row(cron_id=2)[3])

    def test_short_cancel_by_bare_id(self):
        reply = tools._cancel_cron_from_text(
            OWNER,
            "turn off #1",
            originating_chat_id="chat-b",
        )

        self.assertIn("Disabled cron #1", reply)
        self.assertEqual(0, self._row(cron_id=1)[3])

    def test_natural_cron_cancel_refuses_ambiguous_match(self):
        self._insert_cron("07:30", "chat-a")

        reply = tools._cancel_cron_from_text(
            OWNER,
            "kill the morning job",
            originating_chat_id="chat-a",
        )

        self.assertIn("2 possible jobs", reply)
        self.assertIn("#1", reply)
        self.assertIn("#2", reply)
        self.assertEqual(1, self._row(cron_id=1)[3])
        self.assertEqual(1, self._row(cron_id=2)[3])

    def test_natural_cron_cancel_ignores_normal_job_conversation(self):
        reply = tools._cancel_cron_from_text(
            OWNER,
            "stop talking about your job",
            originating_chat_id="chat-a",
        )

        self.assertIsNone(reply)
        self.assertEqual(1, self._row(cron_id=1)[3])

    def test_rotating_morning_body_avoids_weekday_specific_greetings(self):
        payload = {"recipient": "chat-a", "intro_mode": "rotate"}
        body = tools._render_morning_message_body(
            payload,
            "Do the thing.",
            now_pt=datetime(2026, 5, 6, 6, 30),
        )

        self.assertIn("Do the thing.", body)
        self.assertNotRegex(body.lower(), r"\b(mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b")

    def test_list_crons_all_uses_stable_ids_and_destinations(self):
        self._insert_cron("09:00", OWNER, action="drift_check")
        self._insert_cron("10:00", "83ebe9a629cb4d509946f84fcae65247")

        reply = tools._list_crons("chat-a", scope="all", requester_id=OWNER)

        self.assertIn("Active recurring jobs across all chats", reply)
        self.assertIn("#1 |", reply)
        self.assertIn("#2 |", reply)
        self.assertIn("#3 |", reply)
        self.assertIn("DM with you", reply)
        self.assertIn("GC 83ebe9a", reply)
        self.assertIn("[id 83ebe9a6...e65247]", reply)

    def test_group_chat_label_closes_imessage_db_connection(self):
        source = inspect.getsource(tools._group_chat_label)

        self.assertIn("with closing(sqlite3.connect(str(db_path))) as conn", source)

    def test_list_crons_mine_filters_owner_dm_only(self):
        self._insert_cron("09:00", OWNER, action="drift_check")
        self._insert_cron("10:00", "83ebe9a629cb4d509946f84fcae65247")

        reply = tools._list_crons("chat-a", scope="mine", requester_id=OWNER)

        self.assertIn("Active recurring jobs to you", reply)
        self.assertIn("#2 |", reply)
        self.assertIn("DM with you", reply)
        self.assertNotIn("#1 |", reply)
        self.assertNotIn("#3 |", reply)

    def test_list_crons_current_still_uses_stable_id(self):
        reply = tools._list_crons("chat-a", requester_id=OWNER)

        self.assertIn("Recurring jobs in this chat", reply)
        self.assertIn("#1 |", reply)
        self.assertNotRegex(reply, r"(?m)^1\. daily")

    def test_list_crons_warns_on_duplicate_destinations(self):
        self._insert_cron("06:30", "chat-a", payload_extra={"intro_mode": "rotate"})

        reply = tools._list_crons("chat-a", scope="all", requester_id=OWNER)

        self.assertIn("Check these before deleting", reply)
        self.assertIn("Possible duplicate: #1, #2", reply)
        self.assertIn("DM: DM chat-a", reply)

    def test_admin_can_create_current_chat_sports_recap_cron(self):
        with patch.object(tools, "_get_sports_recap", lambda: "TEST SPORTS RECAP"):
            reply = tools._sports_recap_cron_from_text(
                "admin",
                "@davos create a new cron job to run at 6pm pst daily sports recap with Seattle emphasis",
                originating_chat_id="83ebe9a629cb4d509946f84fcae65247",
            )

        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT id, cron_expression, action_type, action_payload, created_by FROM cron_jobs WHERE id = 2"
            ).fetchone()
            log_row = conn.execute(
                "SELECT event_type FROM bot_log WHERE event_type = 'sports_recap_cron_created'"
            ).fetchone()
        finally:
            conn.close()

        self.assertIn("Created sports recap cron #2", reply)
        self.assertIn("TEST UPDATE:\nTEST SPORTS RECAP", reply)
        self.assertEqual((2, "18:00", "sports_recap"), row[:3])
        self.assertEqual("admin", row[4])
        payload = json.loads(row[3])
        self.assertEqual("83ebe9a629cb4d509946f84fcae65247", payload["recipient"])
        self.assertEqual("clean_scoreboard", payload["style"])
        self.assertEqual("pro_playoffs_unc_seattle_college", payload["focus"])
        self.assertEqual(("sports_recap_cron_created",), log_row)

    def test_admin_sports_recap_edit_updates_existing_current_chat_job(self):
        with patch.object(tools, "_get_sports_recap", lambda: "TEST SPORTS RECAP"):
            tools._sports_recap_cron_from_text(
                "admin",
                "create daily sports recap cron at 6pm",
                originating_chat_id="chat-a",
            )
            reply = tools._sports_recap_cron_from_text(
                "admin",
                "change the sports recap cron to 7pm",
                originating_chat_id="chat-a",
            )

        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT cron_expression, action_type, action_payload FROM cron_jobs WHERE action_type = 'sports_recap'"
            ).fetchall()
        finally:
            conn.close()

        self.assertIn("Updated sports recap cron #2", reply)
        self.assertEqual(1, len(rows))
        self.assertEqual("19:00", rows[0][0])
        self.assertEqual("chat-a", json.loads(rows[0][2])["recipient"])

    def test_admin_can_refresh_current_chat_sports_cron_without_time(self):
        self._insert_cron(
            "18:00",
            "chat-a",
            action="sports_recap",
            payload_extra={"style": "old", "focus": "major_4_playoffs_seattle"},
        )

        with patch.object(tools, "_get_sports_recap", lambda: "TEST SPORTS RECAP"):
            reply = tools._sports_recap_cron_from_text(
                "admin",
                "fix the sports cron",
                originating_chat_id="chat-a",
            )

        expr, action, raw, enabled = self._row(cron_id=2)
        payload = json.loads(raw)
        self.assertIn("Refreshed sports recap cron #2", reply)
        self.assertEqual("18:00", expr)
        self.assertEqual("sports_recap", action)
        self.assertEqual("clean_scoreboard", payload["style"])
        self.assertEqual("pro_playoffs_unc_seattle_college", payload["focus"])

    def test_dm_sports_recap_for_other_chat_refuses_to_guess_destination(self):
        reply = tools._sports_recap_cron_from_text(
            OWNER,
            "set up daily sports recap cron for Cole at 6pm",
            originating_chat_id=OWNER,
        )

        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM cron_jobs WHERE action_type = 'sports_recap'"
            ).fetchone()[0]
        finally:
            conn.close()

        self.assertIn("won't guess a different chat", reply)
        self.assertEqual(0, count)

    def test_sports_recap_edit_without_existing_current_chat_does_not_create(self):
        reply = tools._sports_recap_cron_from_text(
            "admin",
            "fix the sports recap cron to 7pm",
            originating_chat_id="chat-a",
        )

        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM cron_jobs WHERE action_type = 'sports_recap'"
            ).fetchone()[0]
        finally:
            conn.close()

        self.assertIn("No active sports recap cron found in this chat", reply)
        self.assertEqual(0, count)

    def test_describe_cron_uses_stable_id_and_destination(self):
        reply = tools._describe_cron_from_text(
            "admin",
            "describe cron #1",
            originating_chat_id="chat-a",
        )

        self.assertIn("Cron #1 (enabled)", reply)
        self.assertIn("When: daily at 6:30 am PT", reply)
        self.assertIn("Destination: DM: DM chat-a", reply)
        self.assertIn("Morning message with fixed intro", reply)

    def test_admin_cannot_describe_other_chat_cron_by_id(self):
        self._insert_cron("09:00", OWNER, action="drift_check")

        reply = tools._describe_cron_from_text(
            "admin",
            "describe cron #2",
            originating_chat_id="chat-a",
        )

        self.assertIn("not in this chat", reply)

    def test_tool_schema_enums_do_not_use_empty_strings(self):
        def walk(value):
            if isinstance(value, dict):
                if "enum" in value:
                    self.assertNotIn("", value["enum"])
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(tools.TOOL_DEFINITIONS)


if __name__ == "__main__":
    unittest.main()
