import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import davosbot.tools as tools


class ToolChangeRequestTests(unittest.TestCase):
    def _make_db(self, tmp: str) -> str:
        db_path = str(Path(tmp) / "davosbot.db")
        with closing(sqlite3.connect(db_path)) as conn:
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
        return db_path

    def test_log_change_request_writes_guarded_handoff(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = self._make_db(tmp)
            with patch.object(tools, "BOT_DB_PATH", db_path):
                reply = tools._log_change_request(
                    "build a robust setup flow api_key=sk-supersecret1234567890abcdef",
                    "needs branch, tests, CI, and Mini smoke",
                )
            with closing(sqlite3.connect(db_path)) as conn:
                request, reason = conn.execute("SELECT request, reason FROM change_log").fetchone()

        self.assertIn("Logged guarded Codex handoff #1 [YELLOW]", reply)
        self.assertIn("[TOOL-HANDOFF YELLOW]", request)
        self.assertIn("type=tool_change_request", reason)
        self.assertIn("source=gemini_tool_log_change_request", reason)
        self.assertIn("safe_auto_fix_pipeline=Codex only", reason)
        self.assertIn("[redacted-openai-key]", reason)
        self.assertNotIn("sk-supersecret", reason)

    def test_log_change_request_marks_cron_repairs_red(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = self._make_db(tmp)
            with patch.object(tools, "BOT_DB_PATH", db_path):
                reply = tools._log_change_request("ship this cron fix", "sports recap failed")
            with closing(sqlite3.connect(db_path)) as conn:
                request, reason = conn.execute("SELECT request, reason FROM change_log").fetchone()

        self.assertIn("[RED]", reply)
        self.assertIn("[TOOL-HANDOFF RED]", request)
        self.assertIn("risk=RED", reason)

    def test_tool_description_does_not_call_large_requests_too_big_to_fix(self):
        tool_def = next(item for item in tools.TOOL_DEFINITIONS if item["name"] == "log_change_request")

        description = tool_def["description"].lower()
        self.assertIn("guarded codex handoff", description)
        self.assertNotIn("too big to execute", description)
        self.assertNotIn("too big to fix", description)


if __name__ == "__main__":
    unittest.main()
