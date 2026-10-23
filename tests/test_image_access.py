import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from davosbot import image_access
def _init_db(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE bot_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                sender TEXT,
                event_type TEXT,
                payload TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE tool_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT,
                tool TEXT,
                ts TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


class ImageAccessTests(unittest.TestCase):
    def test_owner_is_uncapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "bot.db")
            _init_db(db_path)
            with patch.object(image_access, "is_owner", lambda sender: True):
                status = image_access.get_image_access_status("+13369700454", db_path=db_path)

        self.assertTrue(status.allowed)
        self.assertIsNone(status.daily_limit)
        self.assertIsNone(status.remaining)

    def test_non_owner_base_limit_counts_scan_and_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "bot.db")
            _init_db(db_path)
            conn = sqlite3.connect(db_path)
            try:
                for _ in range(3):
                    conn.execute(
                        "INSERT INTO tool_usage (sender, tool) VALUES (?, ?)",
                        ("+15550000001", image_access.IMAGE_TOOL_NAMES[0]),
                    )
                for _ in range(2):
                    conn.execute(
                        "INSERT INTO tool_usage (sender, tool) VALUES (?, ?)",
                        ("+15550000001", image_access.IMAGE_TOOL_NAMES[1]),
                    )
                conn.commit()
            finally:
                conn.close()
            with patch.object(image_access, "is_owner", lambda sender: False):
                status = image_access.get_image_access_status("+15550000001", db_path=db_path)

        self.assertFalse(status.allowed)
        self.assertEqual(5, status.daily_limit)
        self.assertEqual(5, status.used_today)

    def test_extend_and_revoke_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "bot.db")
            _init_db(db_path)
            with patch.object(image_access, "is_owner", lambda sender: False):
                image_access.record_image_access_policy("owner", "+15550000001", "extend", 5, db_path=db_path)
                extended = image_access.get_image_access_status("+15550000001", db_path=db_path)
                image_access.record_image_access_policy("owner", "+15550000001", "revoke", db_path=db_path)
                revoked = image_access.get_image_access_status("+15550000001", db_path=db_path)
                image_access.record_image_access_policy("owner", "+15550000001", "allow", db_path=db_path)
                allowed = image_access.get_image_access_status("+15550000001", db_path=db_path)

        self.assertEqual(10, extended.daily_limit)
        self.assertFalse(extended.revoked)
        self.assertTrue(revoked.revoked)
        self.assertFalse(revoked.allowed)
        self.assertFalse(allowed.revoked)
        self.assertEqual(10, allowed.daily_limit)


if __name__ == "__main__":
    unittest.main()
