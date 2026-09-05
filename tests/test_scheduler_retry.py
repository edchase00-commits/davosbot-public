import ast
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from davosbot import tools
from davosbot.runtime_locks import schedule_locked

ROOT = Path(__file__).resolve().parents[1]
_CRON_NOW_UTC = datetime(2026, 9, 5, 0, 35, 59, 900000, tzinfo=timezone.utc)


class _FrozenCronDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _CRON_NOW_UTC.astimezone(tz) if tz else _CRON_NOW_UTC.replace(tzinfo=None)


class _ClosingConnection:
    def __init__(self, *args, **kwargs):
        self._conn = sqlite3.connect(*args, **kwargs)

    def __enter__(self):
        self._conn.__enter__()
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._conn.__exit__(exc_type, exc, tb)
        finally:
            self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _load_scheduler_helpers(send_message):
    tree = ast.parse((ROOT / "davosbot" / "main.py").read_text(encoding="utf-8"))
    wanted_assigns = {
        "_LAST_SCHED_HEARTBEAT",
        "_SCHEDULED_TASK_MAX_ATTEMPTS",
        "_SCHEDULED_TASK_RETRY_DELAY_SECONDS",
        "_SCHEDULED_ATTEMPT_RE",
        "_LAST_CRON_CHECK",
    }
    wanted_funcs = {
        "_scheduled_task_attempt_count",
        "_scheduled_task_failure_state",
        "_check_scheduled_tasks",
        "_check_cron_jobs",
    }
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id in wanted_assigns for target in node.targets):
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted_funcs:
            nodes.append(node)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "schedule_locked": schedule_locked,
        "BOT_DB_PATH": "",
        "OWNER_ID": "+15550000001",
        "logger": SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None),
        "re": __import__("re"),
        "redact_secret": lambda text: text,
        "send_message": send_message,
        "sqlite3": SimpleNamespace(connect=_ClosingConnection),
        "time": time,
        "__name__": "davosbot.main",
        "__package__": "davosbot",
    }
    exec(compile(module, str(ROOT / "davosbot" / "main.py"), "exec"), namespace)
    return namespace


class SchedulerRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "davosbot.db")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE scheduled_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_type TEXT NOT NULL DEFAULT 'send_imessage',
                    recipient TEXT NOT NULL,
                    message TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    sent_at TEXT,
                    chat_id TEXT,
                    sender TEXT,
                    error TEXT
                )
                """
            )
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
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _scheduled_row(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT status, error, sent_at FROM scheduled_tasks WHERE id = 1").fetchone()
        finally:
            conn.close()

    def _cron_last_run(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT last_run FROM cron_jobs WHERE id = 1").fetchone()[0]
        finally:
            conn.close()

    def test_scheduled_task_send_failure_stays_pending_for_retry(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO scheduled_tasks (recipient, message, scheduled_at) VALUES (?, ?, datetime('now', '-1 minute'))",
                ("+15550000001", "test",),
            )
            conn.commit()
        finally:
            conn.close()
        helpers = _load_scheduler_helpers(lambda *args, **kwargs: False)
        helpers["BOT_DB_PATH"] = self.db_path

        helpers["_check_scheduled_tasks"]()

        status, error, sent_at = self._scheduled_row()
        self.assertEqual("pending", status)
        self.assertIn("attempt 1/5", error)
        self.assertIsNotNone(sent_at)

    def test_scheduled_task_fails_permanently_after_max_attempts(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO scheduled_tasks (recipient, message, scheduled_at, sent_at, error)
                VALUES (?, ?, datetime('now', '-1 minute'), datetime('now', '-2 minutes'), ?)
                """,
                ("+15550000001", "test", "attempt 4/5: send_message returned False"),
            )
            conn.commit()
        finally:
            conn.close()
        helpers = _load_scheduler_helpers(lambda *args, **kwargs: False)
        helpers["BOT_DB_PATH"] = self.db_path

        helpers["_check_scheduled_tasks"]()

        status, error, _sent_at = self._scheduled_row()
        self.assertEqual("failed", status)
        self.assertIn("attempt 5/5", error)

    def test_cron_send_failure_does_not_advance_last_run(self):
        hhmm = _CRON_NOW_UTC.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%H:%M")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO cron_jobs (cron_expression, action_type, action_payload, enabled, created_by) VALUES (?, ?, ?, 1, 'owner')",
                (hhmm, "morning_message", json.dumps({"recipient": "+15550000001"})),
            )
            conn.commit()
        finally:
            conn.close()
        send = Mock(return_value=False)
        helpers = _load_scheduler_helpers(send)
        helpers["BOT_DB_PATH"] = self.db_path
        helpers["time"] = SimpleNamespace(time=lambda: _CRON_NOW_UTC.timestamp())
        with patch("datetime.datetime", _FrozenCronDatetime), patch.object(tools, "_get_inspirational_quote", lambda: "quote"), patch.object(
            tools, "_render_morning_message_body", lambda payload, quote, now_pt: "body"
        ):
            helpers["_check_cron_jobs"]()

        send.assert_called_once_with("+15550000001", "body", is_group=False, recovery_mode="inline")
        self.assertIsNone(self._cron_last_run())

    def test_cron_send_success_advances_last_run(self):
        hhmm = _CRON_NOW_UTC.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%H:%M")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO cron_jobs (cron_expression, action_type, action_payload, enabled, created_by) VALUES (?, ?, ?, 1, 'owner')",
                (hhmm, "morning_message", json.dumps({"recipient": "+15550000001"})),
            )
            conn.commit()
        finally:
            conn.close()
        send = Mock(return_value=True)
        helpers = _load_scheduler_helpers(send)
        helpers["BOT_DB_PATH"] = self.db_path
        helpers["time"] = SimpleNamespace(time=lambda: _CRON_NOW_UTC.timestamp())
        with patch("datetime.datetime", _FrozenCronDatetime), patch.object(tools, "_get_inspirational_quote", lambda: "quote"), patch.object(
            tools, "_render_morning_message_body", lambda payload, quote, now_pt: "body"
        ):
            helpers["_check_cron_jobs"]()

        send.assert_called_once_with("+15550000001", "body", is_group=False, recovery_mode="inline")
        self.assertIsNotNone(self._cron_last_run())


if __name__ == "__main__":
    unittest.main()
