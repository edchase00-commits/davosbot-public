import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from davosbot import reminder_tools, tools


class _FakeConnection:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return SimpleNamespace(lastrowid=42)


class ReminderRoutingTests(unittest.TestCase):
    def test_set_reminder_requires_originating_chat_id(self):
        reply = tools._set_reminder("check this", "2099-01-01 00:00:00", originating_chat_id="")
        self.assertIn("Couldn't determine where to send", reply)

    def test_execute_tool_ignores_llm_supplied_chat_id_for_reminders(self):
        fake = _FakeConnection()
        due = (
            datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        ).strftime("%Y-%m-%d %H:%M:%S")
        args = {
            "message": "bring the charger",
            "due_ts": due,
            "chat_id": "llm-supplied-wrong-chat",
        }

        with patch.object(reminder_tools, "connect_bot_db", lambda _path: fake), patch.object(
            reminder_tools, "_humanize_due", lambda _due: "soon"
        ):
            reply = tools.execute_tool(
                "set_reminder",
                args,
                sender="+15550000001",
                originating_chat_id="actual-origin-chat",
            )

        self.assertIn("Got it", reply)
        self.assertEqual(1, len(fake.calls))
        _sql, params = fake.calls[0]
        self.assertEqual(
            ("actual-origin-chat", "bring the charger", due, "actual-origin-chat"),
            params,
        )
        self.assertNotIn("llm-supplied-wrong-chat", str(params))

    def test_execute_tool_ignores_llm_supplied_chat_id_for_list_reminders(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE reminders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT,
                        message TEXT,
                        due_ts TEXT,
                        sent INTEGER DEFAULT 0,
                        origin_chat_id TEXT,
                        send_attempts INTEGER DEFAULT 0
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO reminders (chat_id, message, due_ts, sent, origin_chat_id, send_attempts) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        ("actual-origin-chat", "correct chat", "2099-01-01 10:00:00", 0, "actual-origin-chat", 0),
                        ("llm-supplied-wrong-chat", "wrong chat", "2099-01-01 11:00:00", 0, "llm-supplied-wrong-chat", 0),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            with patch.object(reminder_tools, "BOT_DB_PATH", db_path), patch.object(
                reminder_tools, "_humanize_due", lambda due: due
            ):
                reply = tools.execute_tool(
                    "list_reminders",
                    {"chat_id": "llm-supplied-wrong-chat"},
                    sender="+15550000001",
                    originating_chat_id="actual-origin-chat",
                )

        self.assertIn("correct chat", reply)
        self.assertNotIn("wrong chat", reply)

    def test_execute_tool_ignores_llm_supplied_chat_id_for_cancel_reminder(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE reminders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT,
                        message TEXT,
                        due_ts TEXT,
                        sent INTEGER DEFAULT 0,
                        origin_chat_id TEXT,
                        send_attempts INTEGER DEFAULT 0
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO reminders (chat_id, message, due_ts, sent, origin_chat_id, send_attempts) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        ("actual-origin-chat", "correct chat", "2099-01-01 10:00:00", 0, "actual-origin-chat", 0),
                        ("llm-supplied-wrong-chat", "wrong chat", "2099-01-01 11:00:00", 0, "llm-supplied-wrong-chat", 0),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            with patch.object(reminder_tools, "BOT_DB_PATH", db_path), patch.object(
                reminder_tools, "_humanize_due", lambda due: due
            ):
                reply = tools.execute_tool(
                    "cancel_reminder",
                    {"position": 1, "chat_id": "llm-supplied-wrong-chat"},
                    sender="+15550000001",
                    originating_chat_id="actual-origin-chat",
                )

            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute("SELECT message FROM reminders ORDER BY id").fetchall()
            finally:
                conn.close()

        self.assertIn("Cancelled: correct chat", reply)
        self.assertEqual([("wrong chat",)], rows)

    def test_set_reminder_normalizes_iso_and_rounds_near_past_forward(self):
        fake = _FakeConnection()
        now = datetime(2026, 6, 8, 19, 0, 0)

        with patch.object(reminder_tools, "_utcnow_naive", lambda: now), patch.object(
            reminder_tools, "connect_bot_db", lambda _path: fake
        ), patch.object(reminder_tools, "_humanize_due", lambda due: due):
            reply = reminder_tools._set_reminder(
                "stretch",
                "2026-06-08T18:59:30Z",
                originating_chat_id="actual-origin-chat",
            )

        self.assertIn("2026-06-08 19:00:30", reply)
        _sql, params = fake.calls[0]
        self.assertEqual(
            ("actual-origin-chat", "stretch", "2026-06-08 19:00:30", "actual-origin-chat"),
            params,
        )

    def test_list_reminders_returns_all_pending_rows_for_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE reminders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT,
                        message TEXT,
                        due_ts TEXT,
                        sent INTEGER DEFAULT 0,
                        origin_chat_id TEXT,
                        send_attempts INTEGER DEFAULT 0
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO reminders (chat_id, message, due_ts, sent, origin_chat_id, send_attempts) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        ("owner", "pick up wine", "2099-01-01 10:00:00", 0, "owner", 0),
                        ("owner", "call Cole", "2099-01-01 11:00:00", 0, "owner", 0),
                        ("other", "not yours", "2099-01-01 12:00:00", 0, "other", 0),
                        ("owner", "already sent", "2099-01-01 13:00:00", 1, "owner", 0),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            with patch.object(reminder_tools, "BOT_DB_PATH", db_path), patch.object(
                reminder_tools, "_humanize_due", lambda due: due
            ):
                reply = tools._list_reminders("owner")

        self.assertIn("Pending reminders:", reply)
        self.assertIn("1. 2099-01-01 10:00:00 \u2014 pick up wine", reply)
        self.assertIn("2. 2099-01-01 11:00:00 \u2014 call Cole", reply)
        self.assertNotIn("not yours", reply)
        self.assertNotIn("already sent", reply)

    def test_list_and_cancel_include_legacy_hidden_failed_reminders(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE reminders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT,
                        message TEXT,
                        due_ts TEXT,
                        sent INTEGER DEFAULT 0,
                        origin_chat_id TEXT,
                        send_attempts INTEGER DEFAULT 0
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO reminders (chat_id, message, due_ts, sent, origin_chat_id, send_attempts) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        ("owner", "normal pending", "2099-01-01 10:00:00", 0, "owner", 0),
                        ("owner", "hidden failed", "2099-01-01 11:00:00", 1, "owner", 4),
                        ("owner", "sent after retry", "2099-01-01 12:00:00", 1, "owner", 1),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            with patch.object(reminder_tools, "BOT_DB_PATH", db_path), patch.object(
                reminder_tools, "_humanize_due", lambda due: due
            ):
                reply = tools._list_reminders("owner")
                cancel = tools._cancel_reminder(2, originating_chat_id="owner")

        self.assertIn("normal pending", reply)
        self.assertIn("hidden failed", reply)
        self.assertIn("delivery failed before this fix", reply)
        self.assertNotIn("sent after retry", reply)
        self.assertIn("Cancelled: hidden failed", cancel)

    def test_pending_retry_rows_do_not_show_delivery_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE reminders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT,
                        message TEXT,
                        due_ts TEXT,
                        sent INTEGER DEFAULT 0,
                        origin_chat_id TEXT,
                        send_attempts INTEGER DEFAULT 0
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO reminders (chat_id, message, due_ts, sent, origin_chat_id, send_attempts) VALUES (?, ?, ?, ?, ?, ?)",
                    ("owner", "still retrying", "2099-01-01 10:00:00", 0, "owner", 9),
                )
                conn.commit()
            finally:
                conn.close()

            with patch.object(reminder_tools, "BOT_DB_PATH", db_path), patch.object(
                reminder_tools, "_humanize_due", lambda due: due
            ):
                reply = tools._list_reminders("owner")

        self.assertIn("still retrying", reply)
        self.assertNotIn("delivery retry", reply)
        self.assertNotIn("delivery failed", reply)

    def test_humanize_due_labels_pacific_time(self):
        reply = reminder_tools._humanize_due("2099-01-01 17:00:00")

        self.assertIn("9:00 am PT", reply)
        self.assertNotIn("UTC", reply)

    def test_cancel_multiple_reminders_by_visible_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE reminders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT,
                        message TEXT,
                        due_ts TEXT,
                        sent INTEGER DEFAULT 0,
                        origin_chat_id TEXT,
                        send_attempts INTEGER DEFAULT 0
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO reminders (chat_id, message, due_ts, sent, origin_chat_id, send_attempts) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        ("owner", "first thing", "2099-01-01 10:00:00", 0, "owner", 0),
                        ("owner", "second thing", "2099-01-01 11:00:00", 0, "owner", 0),
                        ("owner", "third thing", "2099-01-01 12:00:00", 0, "owner", 0),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            with patch.object(reminder_tools, "BOT_DB_PATH", db_path), patch.object(
                reminder_tools, "_humanize_due", lambda due: due
            ):
                cancel = tools._cancel_reminders([1, 2], originating_chat_id="owner")
                remaining = tools._list_reminders("owner")

        self.assertIn("Cancelled reminders:", cancel)
        self.assertIn("first thing", cancel)
        self.assertIn("second thing", cancel)
        self.assertNotIn("third thing", cancel)
        self.assertIn("third thing", remaining)
        self.assertNotIn("first thing", remaining)
        self.assertNotIn("second thing", remaining)


if __name__ == "__main__":
    unittest.main()
