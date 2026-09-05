import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import export_change_log


class ExportChangeLogTests(unittest.TestCase):
    def _make_db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                """
                CREATE TABLE change_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request TEXT NOT NULL,
                    reason TEXT,
                    created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "INSERT INTO change_log (request, reason, created_ts) VALUES (?, ?, ?)",
                ("image gen failed with token=abc123", "needs image key", "2026-05-16 12:00:00"),
            )
            conn.execute(
                "INSERT INTO change_log (request, reason, created_ts) VALUES (?, ?, ?)",
                ("docs cleanup", "", "2026-05-16 12:01:00"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_stdout_board_redacts_and_groups_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "davosbot.db"
            self._make_db(db_path)

            board = export_change_log.format_board(export_change_log.fetch_rows(db_path))

        self.assertIn("Triage board", board)
        self.assertIn("RED - no phone shipping", board)
        self.assertIn("GREEN - safe", board)
        self.assertNotIn("abc123", board)
        self.assertIn("token=[redacted]", board)

    def test_explicit_bracketed_risk_prefix_wins(self):
        self.assertEqual(
            "red",
            export_change_log.classify_change_request("[SELF-REPAIR RED] analyze this cron issue"),
        )
        self.assertEqual(
            "yellow",
            export_change_log.classify_change_request("[GROUP-ERROR YELLOW] summarize this issue"),
        )
        self.assertEqual(
            "green",
            export_change_log.classify_change_request("[DOCS GREEN] cleanup wording only"),
        )

    def test_empty_board_shows_logging_guidance(self):
        board = export_change_log.format_board([])

        self.assertIn("Change log is empty.", board)
        self.assertIn("Use `log [thing]`", board)
        self.assertIn("`analyze this and log`", board)
        self.assertIn("`ship safe cleanup`", board)

    def test_write_snapshot_creates_stable_and_timestamped_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "exports" / "private"
            stable, snapshot = export_change_log.write_snapshot("board text", output_dir)

            self.assertEqual(output_dir / "change_log_board.md", stable)
            self.assertTrue(stable.exists())
            self.assertTrue(snapshot.exists())
            self.assertIn("board text", stable.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
