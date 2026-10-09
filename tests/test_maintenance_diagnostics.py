import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import maintenance_diagnostics as maintenance
from davosbot.inbox import initialize_schema


class MaintenanceDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "bot.sqlite"
        initialize_schema(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE change_log (id INTEGER PRIMARY KEY, request TEXT, reason TEXT, created_ts TEXT);
                CREATE TABLE bot_log (id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT, payload TEXT);
                INSERT INTO change_log VALUES (1, '[GREEN] docs', '', datetime('now'));
                INSERT INTO change_log VALUES (2, '[RED] owner permission', '', datetime('now'));
                INSERT INTO inbound_source (id,initialized_at,last_poll_at)
                VALUES (1,strftime('%s','now'),strftime('%s','now'));
            """)
        for name, value in (("REPORT_DIR", self.root / "reports"), ("STATE_PATH", self.root / "state.json")):
            p = patch.object(maintenance, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(maintenance, "_load_config", return_value=SimpleNamespace(BOT_DB_PATH=str(self.db)))
        p.start()
        self.addCleanup(p.stop)

    def add_error(self, age="now", payload="private synthetic payload"):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO bot_log VALUES (NULL,datetime(?),'provider_error',?)", (age, payload))

    def test_failure_reaches_report_state_and_exit_code(self):
        self.add_error(payload="do not copy this private synthetic payload")
        with patch.object(maintenance, "_quick_smoke", return_value=(True, "ok")):
            result = maintenance.collect_diagnostics(update_state=True)
            code = maintenance.main([])
        self.assertFalse(result.ok)
        self.assertEqual(1, code)
        self.assertEqual(1, result.recent_error_count)
        report = result.report.read_text(encoding="utf-8")
        self.assertIn("Overall: FAIL", report)
        self.assertIn("provider_error", report)
        self.assertNotIn("private synthetic payload", report)
        self.assertFalse(json.loads(maintenance.STATE_PATH.read_text())["ok"])

    def test_old_errors_do_not_keep_current_diagnostics_failed_forever(self):
        self.add_error("2000-01-01")
        with patch.object(maintenance, "_quick_smoke", return_value=(True, "ok")):
            result = maintenance.collect_diagnostics()
        self.assertTrue(result.ok)
        self.assertIn("Total 2 | GREEN 1 | YELLOW 0 | RED 1", result.report.read_text())

    def test_invalid_error_timestamp_is_visible_without_copying_its_content(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO bot_log VALUES (NULL,'invalid-private-date','provider_error','')")
        with patch.object(maintenance, "_quick_smoke", return_value=(True, "ok")):
            result = maintenance.collect_diagnostics()
        self.assertFalse(result.ok)
        self.assertIn("unknown time", result.report.read_text())
        self.assertNotIn("invalid-private-date", result.report.read_text())

    def test_smoke_timeout_and_failure_produce_failed_report(self):
        for outcome in ((False, "token=syntheticvalue"), subprocess.TimeoutExpired("synthetic", 1)):
            with self.subTest(outcome=type(outcome).__name__):
                kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with patch.object(maintenance, "_quick_smoke", **kwargs):
                    result = maintenance.collect_diagnostics()
                self.assertFalse(result.ok)
                self.assertIn("Overall: FAIL", result.report.read_text())
                self.assertNotIn("syntheticvalue", result.report.read_text())

    def test_missing_or_broken_database_reports_failure_without_creating_it(self):
        self.db.unlink()
        with patch.object(maintenance, "_quick_smoke", return_value=(True, "ok")):
            result = maintenance.collect_diagnostics()
        self.assertFalse(result.ok)
        self.assertFalse(self.db.exists())
        with closing(sqlite3.connect(self.db)):
            pass
        with patch.object(maintenance, "_quick_smoke", return_value=(True, "ok")):
            result = maintenance.collect_diagnostics()
        self.assertFalse(result.ok)
        self.assertIn("unavailable", result.report.read_text())

    def test_read_connections_close_and_cannot_mutate(self):
        original = sqlite3.connect
        retained = []
        def connect(*args, **kwargs):
            conn = original(*args, **kwargs)
            retained.append(conn)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE forbidden(x)")
            return conn
        with patch.object(maintenance.sqlite3, "connect", side_effect=connect):
            maintenance._read_change_log_counts(str(self.db))
            maintenance._recent_bot_errors(str(self.db))
        self.assertEqual(2, len(retained))
        for conn in retained:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    def test_intake_hold_fails_report_state_and_exit_without_historical_error(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE inbound_source SET session_error='untracked_runtime_session' WHERE id=1")
        with patch.object(maintenance, "_quick_smoke", return_value=(True, "ok")):
            result = maintenance.collect_diagnostics(update_state=True)
            code = maintenance.main([])
        self.assertFalse(result.ok)
        self.assertTrue(result.smoke_ok)
        self.assertFalse(result.inbox_ok)
        self.assertEqual(0, result.recent_error_count)
        self.assertEqual(1, code)
        report = result.report.read_text()
        self.assertIn("Overall: FAIL", report)
        self.assertIn("active source/session hold", report)
        self.assertFalse(json.loads(maintenance.STATE_PATH.read_text())["inbox_ok"])


if __name__ == "__main__":
    unittest.main()
