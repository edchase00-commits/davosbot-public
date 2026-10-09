import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from davosbot import main, memory


class ReminderSendFailureTests(unittest.TestCase):
    def _make_db(self) -> tuple[tempfile.TemporaryDirectory, str]:
        tmp = tempfile.TemporaryDirectory()
        db_path = str(Path(tmp.name) / "davosbot.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    message TEXT NOT NULL,
                    due_ts TEXT NOT NULL,
                    sent INTEGER DEFAULT 0,
                    origin_chat_id TEXT DEFAULT '',
                    send_attempts INTEGER DEFAULT 0,
                    last_attempt_ts TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
        return tmp, db_path

    def test_due_reminders_includes_exhausted_unsent_rows_after_cooldown(self):
        tmp, db_path = self._make_db()
        self.addCleanup(tmp.cleanup)
        conn = sqlite3.connect(db_path)
        try:
            conn.executemany(
                """
                INSERT INTO reminders (chat_id, message, due_ts, sent, origin_chat_id, send_attempts)
                VALUES (?, ?, datetime('now', '-1 minute'), 0, ?, ?)
                """,
                [
                    ("owner", "retry me", "owner", 4),
                    ("owner", "exhausted", "owner", 5),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        with patch.object(memory, "BOT_DB_PATH", db_path):
            due = memory.get_due_reminders()

        self.assertEqual(["retry me", "exhausted"], [row["message"] for row in due])

    def test_check_reminders_keeps_retrying_after_old_cap(self):
        bumped = []
        marked_sent = []

        with (
            patch.object(
                main,
                "get_due_reminders",
                return_value=[
                    {
                        "id": 7,
                        "chat_id": "owner",
                        "origin_chat_id": "owner",
                        "message": "send me",
                        "send_attempts": 5,
                    }
                ],
            ),
            patch.object(main, "send_message", return_value=False),
            patch.object(main, "_bump_reminder_attempts", lambda rid, attempts: bumped.append((rid, attempts))),
            patch.object(main, "mark_reminder_sent", lambda rid: marked_sent.append(rid)),
        ):
            main._check_reminders()

        self.assertEqual([(7, 6)], bumped)
        self.assertEqual([], marked_sent)

    def test_check_reminders_marks_missing_target_failed_not_sent(self):
        bumped = []
        marked_sent = []

        with (
            patch.object(
                main,
                "get_due_reminders",
                return_value=[{"id": 8, "chat_id": "", "origin_chat_id": "", "message": "lost", "send_attempts": 0}],
            ),
            patch.object(main, "_bump_reminder_attempts", lambda rid, attempts: bumped.append((rid, attempts))),
            patch.object(main, "mark_reminder_sent", lambda rid: marked_sent.append(rid)),
        ):
            main._check_reminders()

        self.assertEqual([(8, 1)], bumped)
        self.assertEqual([], marked_sent)


if __name__ == "__main__":
    unittest.main()
