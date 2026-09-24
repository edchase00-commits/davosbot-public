import sqlite3
import tempfile
import unittest
import os
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from davosbot import db


class DbMigrationTests(unittest.TestCase):
    def test_run_migration_allows_fresh_database_without_backup_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "fresh.db"
            backups_dir = Path(tmp) / "backups"

            with (
                patch.object(db, "BOT_DB_PATH", str(db_path)),
                patch.object(db, "_DB_PATH", db_path),
                patch.object(db, "_BACKUPS_DIR", backups_dir),
            ):
                db.run_migration(
                    "CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY)",
                    "fresh sample table",
                )

            self.assertTrue(db_path.exists())
            self.assertEqual([], list(backups_dir.glob("davosbot_*.db")))

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sample'"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(("sample",), row)

    def test_run_migration_skips_existing_table_without_backup_or_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "existing.db"
            backups_dir = Path(tmp) / "backups"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
                conn.execute(
                    """
                    CREATE TABLE bot_log (
                        id INTEGER PRIMARY KEY,
                        sender TEXT,
                        event_type TEXT,
                        payload TEXT
                    )
                    """
                )

            with (
                patch.object(db, "BOT_DB_PATH", str(db_path)),
                patch.object(db, "_DB_PATH", db_path),
                patch.object(db, "_BACKUPS_DIR", backups_dir),
                patch.object(db, "backup_database", side_effect=AssertionError("backup should not run")),
            ):
                db.run_migration(
                    "CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY)",
                    "sample table",
                )

            with closing(sqlite3.connect(db_path)) as conn:
                migration_rows = conn.execute(
                    "SELECT COUNT(*) FROM bot_log WHERE event_type = 'migration'"
                ).fetchone()[0]
            self.assertEqual(0, migration_rows)

    def test_run_migration_skips_existing_index_without_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "existing.db"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("CREATE TABLE sample (value TEXT)")
                conn.execute("CREATE INDEX idx_sample_value ON sample(value)")

            with (
                patch.object(db, "BOT_DB_PATH", str(db_path)),
                patch.object(db, "backup_database", side_effect=AssertionError("backup should not run")),
            ):
                db.run_migration(
                    "CREATE INDEX IF NOT EXISTS idx_sample_value ON sample(value)",
                    "sample value index",
                )

    def test_run_migration_keeps_backup_and_log_for_new_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "existing.db"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE bot_log (
                        id INTEGER PRIMARY KEY,
                        sender TEXT,
                        event_type TEXT,
                        payload TEXT
                    )
                    """
                )

            with (
                patch.object(db, "BOT_DB_PATH", str(db_path)),
                patch.object(db, "backup_database", return_value="backup.db") as backup,
            ):
                db.run_migration(
                    "CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY)",
                    "sample table",
                )

            backup.assert_called_once_with()
            with closing(sqlite3.connect(db_path)) as conn:
                sample = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sample'"
                ).fetchone()
                migration_rows = conn.execute(
                    "SELECT COUNT(*) FROM bot_log WHERE event_type = 'migration'"
                ).fetchone()[0]
            self.assertEqual(("sample",), sample)
            self.assertEqual(1, migration_rows)

    def test_cleanup_old_backups_keeps_recent_and_minimum(self):
        with tempfile.TemporaryDirectory() as tmp:
            backups_dir = Path(tmp) / "backups"
            backups_dir.mkdir()
            now = datetime.now()

            for idx in range(3):
                path = backups_dir / f"davosbot_recent_{idx}.db"
                path.write_text("recent", encoding="utf-8")
                mtime = (now - timedelta(days=idx)).timestamp()
                os.utime(path, (mtime, mtime))

            for idx in range(7):
                path = backups_dir / f"davosbot_old_{idx}.db"
                path.write_text("old", encoding="utf-8")
                mtime = (now - timedelta(days=40 + idx)).timestamp()
                os.utime(path, (mtime, mtime))

            with patch.object(db, "_BACKUPS_DIR", backups_dir):
                db.cleanup_old_backups()

            remaining = {path.name for path in backups_dir.glob("davosbot_*.db")}

        self.assertEqual(
            {
                "davosbot_recent_0.db",
                "davosbot_recent_1.db",
                "davosbot_recent_2.db",
                "davosbot_old_0.db",
                "davosbot_old_1.db",
            },
            remaining,
        )


if __name__ == "__main__":
    unittest.main()
