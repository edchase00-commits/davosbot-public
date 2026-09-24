"""Offline invariants for Work's fixed owner operation adapters."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from davosbot import work_actions as actions


OWNER = "+12025550123"
OTHER = "+12025550124"
FUTURE = "2099-03-04T15:00:00Z"


class WorkActionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "bot.sqlite")
        self.messages = str(Path(self.temp.name) / "messages.sqlite")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE reminders(id INTEGER PRIMARY KEY,chat_id TEXT,message TEXT,due_ts TEXT,
                    origin_chat_id TEXT,sent INTEGER DEFAULT 0,send_attempts INTEGER DEFAULT 0);
                CREATE TABLE cron_jobs(id INTEGER PRIMARY KEY,cron_expression TEXT,action_type TEXT,
                    action_payload TEXT,enabled INTEGER DEFAULT 1,created_by TEXT);
                CREATE TABLE change_log(id INTEGER PRIMARY KEY,request TEXT,reason TEXT);
            """)
        with closing(sqlite3.connect(self.messages)) as conn, conn:
            conn.executescript("""
                CREATE TABLE message(id INTEGER PRIMARY KEY,text TEXT,attributedBody BLOB,is_sent INTEGER,
                    error INTEGER,is_from_me INTEGER);
                CREATE TABLE chat(id INTEGER PRIMARY KEY,chat_identifier TEXT);
                CREATE TABLE chat_message_join(chat_id INTEGER,message_id INTEGER);
            """)
            conn.execute("INSERT INTO chat VALUES(1,?)", (OWNER,))
            conn.execute("INSERT INTO chat VALUES(2,?)", (OTHER,))
        for target, value in (("davosbot.config.OWNER_ID", OWNER), ("davosbot.permissions.OWNER_ID", OWNER),
                              ("davosbot.config.BOT_DB_PATH", self.db), ("davosbot.config.DB_PATH", self.messages)):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    def execute(self, action, args=None):
        return actions.execute_action(action, args or {}, owner=OWNER)

    def reminder(self, owner=OWNER, message="first", due="2099-03-04 15:00:00", **kw):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            cursor = conn.execute("INSERT INTO reminders(chat_id,origin_chat_id,message,due_ts,sent,send_attempts) "
                                  "VALUES(?,?,?,?,?,?)", (owner, kw.get("origin", owner), message, due,
                                                        kw.get("sent", 0), kw.get("attempts", 0)))
            return cursor.lastrowid

    def cron(self, owner=OWNER, enabled=1, **kw):
        payload = {"recipient": owner, "intro": "hello", "intro_mode": "fixed", "retain": "existing"}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            return conn.execute("INSERT INTO cron_jobs(cron_expression,action_type,action_payload,enabled) "
                                "VALUES(?,?,?,?)", (kw.get("time", "08:00"), kw.get("action", "morning_message"),
                                                  json.dumps(payload), enabled)).lastrowid

    def snapshot(self, kind, **args):
        return self.execute(kind + ".list", args)["evidence"]["snapshot"]

    def test_unknown_fields_and_wrong_types_rejected_before_execution(self):
        invalid = [
            ("notify.self", {"message": "hello", "recipient": OTHER}),
            ("reminders.create", {"message": "x", "due_at": "2099-01-01T00:00:00"}),
            ("reminders.cancel", {"id": True, "snapshot": "a" * 64}),
            ("crons.edit", {"id": 1, "snapshot": "a" * 64, "scope": "all"}),
            ("crons.create", {"time_pt": "25:00", "action": "shell_exec"}),
            ("market.alerts", {"enabled": "true"}),
            ("market.quote", {"symbols": ["NVDA;rm"]}),
            ("diagnostics.status", {"command": "whoami"}),
        ]
        for operation, args in invalid:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                actions.validate_action(operation, args)

    def test_nonowner_cannot_execute_or_read(self):
        for actor in (OTHER, "", False):
            result = actions.execute_action("reminders.list", {}, owner=actor)
            self.assertEqual(result["evidence"]["code"], "owner_required")

    def test_reminder_scope_and_legacy_origin_are_preserved(self):
        own = self.reminder()
        legacy = self.reminder(message="legacy", origin="")
        self.reminder(owner=OTHER, message="private other")
        self.reminder(owner=OWNER, origin=OTHER, message="other origin")
        result = self.execute("reminders.list")
        self.assertEqual({r["id"] for r in result["evidence"]["records"]}, {own, legacy})
        self.assertNotIn("private other", json.dumps(result))

    def test_stale_snapshot_rejects_change_and_keeps_both_records(self):
        first = self.reminder()
        snapshot = self.snapshot("reminders")
        self.reminder(message="intervening native creation")
        result = self.execute("reminders.cancel", {"id": first, "snapshot": snapshot})
        self.assertEqual(result["evidence"]["code"], "stale_snapshot")
        self.assertFalse(result["evidence"].get("ambiguous", False))
        self.assertEqual(len(self.execute("reminders.list")["evidence"]["records"]), 2)

    def test_reminder_edit_timezone_and_origin(self):
        first = self.reminder(origin="")
        result = self.execute("reminders.edit", {"id": first, "snapshot": self.snapshot("reminders"),
                                                 "due_at": "2099-03-04T17:00:00+02:00", "message": "changed"})
        self.assertEqual(result["status"], "ok")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            row = conn.execute("SELECT message,due_ts,chat_id,origin_chat_id FROM reminders WHERE id=?", (first,)).fetchone()
        self.assertEqual(row, ("changed", "2099-03-04 15:00:00", OWNER, ""))

    def test_other_reminder_id_cannot_be_mutated_with_owner_snapshot(self):
        other = self.reminder(owner=OTHER)
        result = self.execute("reminders.cancel", {"id": other, "snapshot": self.snapshot("reminders")})
        self.assertEqual(result["evidence"]["code"], "record_not_found")

    def test_failed_edit_transaction_rolls_back(self):
        first = self.reminder()
        snapshot = self.snapshot("reminders")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TRIGGER reject_edit BEFORE UPDATE ON reminders BEGIN SELECT RAISE(ABORT,'private error'); END")
        result = self.execute("reminders.edit", {"id": first, "snapshot": snapshot, "message": "changed"})
        self.assertEqual(result["status"], "error")
        self.assertNotIn("private error", json.dumps(result))
        self.assertEqual(self.snapshot("reminders"), snapshot)

    def test_due_cancel_warns_about_already_running_delivery(self):
        first = self.reminder(due="2020-01-01 00:00:00")
        result = self.execute("reminders.cancel", {"id": first, "snapshot": self.snapshot("reminders")})
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(result["evidence"]["delivery_may_be_in_flight"])
        self.assertEqual(self.execute("reminders.list")["evidence"]["records"], [])

    def test_due_reminder_cannot_be_edited(self):
        first = self.reminder(due="2020-01-01 00:00:00")
        result = self.execute("reminders.edit", {"id": first, "snapshot": self.snapshot("reminders"),
                                                 "message": "changed", "due_at": FUTURE})
        self.assertEqual(result["evidence"]["code"], "reminder_already_due")

    def test_reminder_native_create_has_fixed_owner_and_readback(self):
        from davosbot import reminder_tools
        with patch.object(reminder_tools, "BOT_DB_PATH", self.db):
            result = self.execute("reminders.create", {"message": "real native helper", "due_at": FUTURE})
        self.assertEqual(result["status"], "ok")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            row = conn.execute("SELECT chat_id,origin_chat_id FROM reminders").fetchone()
        self.assertEqual(row, (OWNER, OWNER))

    def test_failed_create_readback_is_ambiguous_not_success(self):
        fake = SimpleNamespace(_set_reminder=lambda *a, **k: "Got it")
        with patch.dict("sys.modules", {"davosbot.reminder_tools": fake}):
            result = self.execute("reminders.create", {"message": "x", "due_at": FUTURE})
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["evidence"]["ambiguous"])

    def test_crons_default_owner_scope_and_explicit_all(self):
        own = self.cron()
        other = self.cron(owner=OTHER)
        self.assertEqual([r["id"] for r in self.execute("crons.list")["evidence"]["records"]], [own])
        self.assertEqual({r["id"] for r in self.execute("crons.list", {"scope": "all"})["evidence"]["records"]}, {own, other})
        result = self.execute("crons.cancel", {"id": other, "snapshot": self.snapshot("crons")})
        self.assertEqual(result["evidence"]["code"], "record_not_found")

    def test_cron_edit_preserves_original_destination_and_other_payload(self):
        job = self.cron(owner=OTHER)
        result = self.execute("crons.edit", {"scope": "all", "id": job, "snapshot": self.snapshot("crons", scope="all"),
                                             "time_pt": "09:15", "day_of_week": "fri", "intro_mode": "rotate"})
        self.assertEqual(result["status"], "ok")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            expr, raw = conn.execute("SELECT cron_expression,action_payload FROM cron_jobs WHERE id=?", (job,)).fetchone()
        payload = json.loads(raw)
        self.assertEqual(expr, "09:15 fri")
        self.assertEqual(payload["recipient"], OTHER)
        self.assertEqual(payload["retain"], "existing")
        self.assertEqual(payload["intro_mode"], "rotate")
        self.assertNotIn("intro", payload)

    def test_cron_action_change_clears_morning_fields(self):
        job = self.cron()
        result = self.execute("crons.edit", {"id": job, "snapshot": self.snapshot("crons"), "action": "sports_recap"})
        self.assertEqual(result["status"], "ok")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            raw = conn.execute("SELECT action_payload FROM cron_jobs WHERE id=?", (job,)).fetchone()[0]
        self.assertNotIn("intro", json.loads(raw))
        self.assertNotIn("intro_mode", json.loads(raw))

    def test_canceled_cron_cannot_be_reenabled_or_edited(self):
        job = self.cron()
        result = self.execute("crons.cancel", {"id": job, "snapshot": self.snapshot("crons")})
        self.assertEqual(result["status"], "ok")
        result = self.execute("crons.edit", {"id": job, "snapshot": self.snapshot("crons"), "time_pt": "09:00"})
        self.assertEqual(result["evidence"]["code"], "record_not_found")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT enabled FROM cron_jobs WHERE id=?", (job,)).fetchone()[0], 0)

    def test_intervening_native_cron_edit_invalidates_snapshot(self):
        job = self.cron()
        old = self.snapshot("crons")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE cron_jobs SET cron_expression='06:00' WHERE id=?", (job,))
        result = self.execute("crons.cancel", {"id": job, "snapshot": old})
        self.assertEqual(result["evidence"]["code"], "stale_snapshot")

    def test_cron_native_create_binds_origin_and_checks_written_row(self):
        def create(time_pt, intro, **kwargs):
            self.assertEqual(kwargs["originating_chat_id"], OWNER)
            self.cron(time=time_pt, action=kwargs["action"])
            return "Done"
        with patch.dict("sys.modules", {"davosbot.tools": SimpleNamespace(_schedule_cron=create)}):
            result = self.execute("crons.create", {"time_pt": "09:00", "action": "sports_recap"})
        self.assertEqual(result["status"], "ok")

    def test_post_insert_row_limit_failure_reports_ambiguous_existing_job(self):
        for _ in range(actions.MAX_ROWS):
            self.cron()
        def create(time_pt, intro, **kwargs):
            self.cron(time=time_pt, action=kwargs["action"])
            return "Done"
        with patch.dict("sys.modules", {"davosbot.tools": SimpleNamespace(_schedule_cron=create)}):
            result = self.execute("crons.create", {"time_pt": "09:00", "action": "sports_recap"})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["evidence"]["code"], "too_many_crons")
        self.assertTrue(result["evidence"]["ambiguous"])
        self.assertNotIn("not completed", result["result"])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cron_jobs").fetchone()[0], actions.MAX_ROWS + 1)

    def test_post_commit_readback_value_error_reports_ambiguous_completed_cancel(self):
        first = self.reminder()
        snapshot = self.snapshot("reminders")
        with patch.object(actions, "_list_records", side_effect=ValueError("too_many_reminders")):
            result = self.execute("reminders.cancel", {"id": first, "snapshot": snapshot})
        self.assertTrue(result["evidence"]["ambiguous"])
        self.assertEqual(result["evidence"]["code"], "too_many_reminders")
        self.assertNotIn("not completed", result["result"])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertIsNone(conn.execute("SELECT id FROM reminders WHERE id=?", (first,)).fetchone())

    def test_validation_failure_is_definitive_and_never_executes(self):
        with patch.object(actions, "_execute") as operation:
            result = self.execute("notify.self", {"message": "x", "recipient": OTHER})
        operation.assert_not_called()
        self.assertEqual(result["evidence"]["code"], "invalid_fields")
        self.assertFalse(result["evidence"].get("ambiguous", False))

    def add_message(self, text, recipient=1, sent=1, error=0):
        with closing(sqlite3.connect(self.messages)) as conn, conn:
            rid = conn.execute("INSERT INTO message(text,is_sent,error,is_from_me) VALUES(?,?,?,1)", (text, sent, error)).lastrowid
            conn.execute("INSERT INTO chat_message_join VALUES(?,?)", (recipient, rid))
        return rid

    def test_old_identical_message_and_other_recipient_do_not_verify_new_send(self):
        old = self.add_message("same")
        self.add_message("same", recipient=2)
        self.assertEqual(actions._new_message_state(self.messages, OWNER, "same", lambda x: x, old), "unknown")
        self.add_message("same", sent=0)
        self.assertEqual(actions._new_message_state(self.messages, OWNER, "same", lambda x: x, old), "pending")

    def track_read_connections(self):
        original = sqlite3.connect
        connections = []

        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            if kwargs.get("uri"):
                connections.append(connection)
                self.addCleanup(connection.close)
            return connection

        return patch.object(actions.sqlite3, "connect", side_effect=connect), connections

    def test_notify_verifies_new_exact_owner_message_without_retry(self):
        calls = []
        def send(owner, message, **kwargs):
            calls.append((owner, kwargs))
            self.add_message(message)
            return True
        track, connections = self.track_read_connections()
        with track, patch("davosbot.imessage.send_message", side_effect=send):
            result = self.execute("notify.self", {"message": "test only in fake DB"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, [(OWNER, {"is_group": False, "recovery_mode": "none"})])
        self.assertEqual(2, len(connections))
        for connection in connections:
            with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                connection.execute("SELECT 1")

    def test_notification_read_connections_close_when_queries_fail(self):
        empty = str(Path(self.temp.name) / "empty-messages.sqlite")
        with closing(sqlite3.connect(empty)):
            pass
        track, connections = self.track_read_connections()
        with track, patch("davosbot.config.DB_PATH", empty), patch("davosbot.imessage.send_message") as send:
            with self.assertRaises(sqlite3.OperationalError):
                actions._notify({"message": "synthetic unsent"}, OWNER)
            with self.assertRaises(sqlite3.OperationalError):
                actions._new_message_state(empty, OWNER, "synthetic unsent", lambda value: value, 0)
        send.assert_not_called()
        self.assertEqual(2, len(connections))
        for connection in connections:
            with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                connection.execute("SELECT 1")

    def test_market_cannot_claim_toggle_success_when_native_hard_disabled(self):
        fake = SimpleNamespace(set_market_alerts_enabled=lambda _: "Hard-disabled", market_alerts_enabled=lambda **_: False)
        with patch("davosbot.work_actions._execute", wraps=actions._execute), patch.dict("sys.modules", {"davosbot.market": fake}):
            # `from . import market` may retain the package attribute after earlier tests.
            import davosbot
            with patch.object(davosbot, "market", fake, create=True):
                result = self.execute("market.alerts", {"enabled": True})
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["evidence"]["enabled"])

    def test_result_size_bound_preserves_snapshot_and_mutation_status(self):
        for number in range(30):
            self.reminder(message="x" * 1000 + str(number))
        result = self.execute("reminders.list")
        self.assertEqual(result["status"], "ok")
        self.assertLessEqual(len(json.dumps(result, separators=(",", ":")).encode()), 7800)
        self.assertTrue(result["evidence"]["truncated"])
        self.assertEqual(result["evidence"]["total_records"], 30)
        self.assertEqual(len(result["evidence"]["snapshot"]), 64)

    def test_diagnostic_results_redact_credentials(self):
        fake = {"status": "ok", "result": "Authorization: Bearer abc123 token=privatevalue", "evidence": {}}
        with patch.object(actions, "_execute", return_value=fake):
            result = self.execute("diagnostics.status")
        self.assertNotIn("abc123", result["result"])
        self.assertNotIn("privatevalue", result["result"])

    def test_capabilities_retrieval_and_optional_page_are_bounded(self):
        result = self.execute("capabilities", {"action": "reminders.edit"})
        self.assertIn("reminders.edit", result["evidence"]["actions"])
        result = self.execute("capabilities", {"limit": 2})
        self.assertEqual(len(result["evidence"]["actions"]), 2)
        self.assertEqual(result["evidence"]["next_offset"], 2)
        self.assertLess(len(json.dumps(self.execute("capabilities")).encode()), 40000)

    def test_capabilities_preserve_nested_workout_schema(self):
        expected_properties = {
            "weight": {"type": "number", "minimum": 0, "maximum": 5000},
            "reps": {"type": "integer", "minimum": 1, "maximum": 1000},
        }
        for args in ({}, {"action": "workouts.log"}):
            with self.subTest(args=args):
                result = self.execute("capabilities", args)
                self.assertEqual(result["status"], "ok")
                schema = result["evidence"]["actions"]["workouts.log"]
                self.assertEqual(schema["fields"]["sets"]["items"]["properties"], expected_properties)
                self.assertEqual(schema, actions.action_catalogue()["workouts.log"])
                self.assertLess(len(json.dumps(result).encode()), 40000)


if __name__ == "__main__":
    unittest.main()
