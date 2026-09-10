"""Read-only inbox health evidence, independent of PM2 or delivery actions."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from davosbot import inbox
from scripts import runtime_smoke


NOW = 1_788_566_400.0


class InboxHealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "synthetic-inbox.sqlite"
        inbox.initialize_schema(self.path)
        self.execute("INSERT INTO inbound_source (id,initialized_at,last_poll_at) VALUES (1,?,?)", (NOW, NOW))

    def execute(self, sql, args=()):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(sql, args)

    def add(self, state, *, age=0):
        self.execute("""INSERT INTO inbound_messages
            (message_guid,source_rowid,sender,chat_identifier,observed_at,updated_at,claimed_at,state,reason)
            VALUES ('synthetic-private-guid',1,'+15550000001','private-group',?,?,?,?,'synthetic-private-reason')""",
            (NOW - age, NOW, NOW - age if state == "processing" else None, state))

    def check(self):
        return runtime_smoke.check_inbox(self.path, now=NOW)

    def test_historical_holds_and_uncertain_records_do_not_fail_current_intake(self):
        for state in ("held", "uncertain", "handler_returned", "ignored"):
            with self.subTest(state=state):
                self.execute("DELETE FROM inbound_messages")
                self.add(state, age=86400)
                result = self.check()
                self.assertTrue(result.ok)
                self.assertEqual(1, result.data["counts"][state])
                encoded = json.dumps(result.data)
                for private in ("synthetic-private", "+15550000001", "private-group", "sender", "message_guid", "held_reasons"):
                    self.assertNotIn(private, encoded)

    def test_active_source_or_session_hold_fails_despite_fresh_poll(self):
        for field in ("last_error", "session_error"):
            self.execute("UPDATE inbound_source SET last_error=NULL,session_error=NULL")
            self.execute(f"UPDATE inbound_source SET {field}=?", ("synthetic-private-error",))
            result = self.check()
            self.assertFalse(result.ok)
            self.assertIn("active source/session hold", result.detail)
            self.assertTrue(result.data["source_error_present"])
            self.assertNotIn("synthetic-private", json.dumps(result.data) + result.detail)

    def test_missing_schema_initialization_or_poll_is_not_healthy(self):
        for statement in (
            "UPDATE inbound_source SET initialized_at=NULL",
            "UPDATE inbound_source SET last_poll_at=NULL",
            "DELETE FROM inbound_source",
            "DROP TABLE inbound_messages",
        ):
            with self.subTest(statement=statement):
                # Restore a healthy source between independent invalid cases.
                inbox.initialize_schema(self.path)
                self.execute("DELETE FROM inbound_source")
                self.execute("INSERT INTO inbound_source (id,initialized_at,last_poll_at) VALUES (1,?,?)", (NOW, NOW))
                self.execute(statement)
                self.assertFalse(self.check().ok)
        absent = self.path.with_name("absent.sqlite")
        self.assertFalse(runtime_smoke.check_inbox(absent, now=NOW).ok)
        self.assertFalse(absent.exists())

    def test_poll_pending_and_processing_use_the_reviewed_300_second_boundary(self):
        for kind in ("poll", "pending", "processing"):
            for age, expected in ((300, True), (301, False)):
                with self.subTest(kind=kind, age=age):
                    self.execute("DELETE FROM inbound_messages")
                    self.execute("UPDATE inbound_source SET last_poll_at=?", (NOW,))
                    if kind == "poll":
                        self.execute("UPDATE inbound_source SET last_poll_at=?", (NOW - age,))
                    else:
                        self.add(kind, age=age)
                    result = self.check()
                    self.assertEqual(expected, result.ok)
                    if not expected:
                        self.assertIn("stale/delayed", result.detail)
                    self.assertNotIn("hung", result.detail)

    def test_invalid_aggregate_values_fail_without_echoing_raw_data(self):
        baseline = inbox.inbox_health(self.path, now=NOW)
        invalid = [
            {**baseline, "last_poll_age_seconds": value}
            for value in (True, "synthetic-private-value", float("nan"), float("inf"), -1)
        ]
        invalid.extend(({**baseline, "counts": {"pending": -1}},
                        {**baseline, "counts": {"processing": True}},
                        {**baseline, "counts": {"pending": 1}, "oldest_pending_age_seconds": None}))
        for data in invalid:
            with patch.object(inbox, "inbox_health", return_value=data):
                result = self.check()
            self.assertFalse(result.ok)
            self.assertNotIn("synthetic-private", json.dumps(result.data) + result.detail)

    def test_real_probe_is_read_only_closes_connections_and_preserves_database(self):
        connect = sqlite3.connect
        retained = []
        before = self.path.read_bytes()
        def track(*args, **kwargs):
            conn = connect(*args, **kwargs)
            retained.append(conn)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE forbidden(value)")
            return conn
        with patch.object(inbox.sqlite3, "connect", side_effect=track):
            self.assertTrue(self.check().ok)
        self.assertEqual(1, len(retained))
        with self.assertRaises(sqlite3.ProgrammingError):
            retained[0].execute("SELECT 1")
        self.assertEqual(before, self.path.read_bytes())

    def test_fresh_pm2_and_heartbeat_do_not_override_intake_failure(self):
        self.execute("UPDATE inbound_source SET session_error='untracked_runtime_session'")
        text = runtime_smoke.format_results([
            runtime_smoke.CheckResult("pm2", True, "online"),
            runtime_smoke.CheckResult("heartbeat", True, "current"), self.check(),
        ])
        self.assertIn("Overall: FAIL (inbox)", text)


if __name__ == "__main__":
    unittest.main()
