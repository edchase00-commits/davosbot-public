import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import davosbot.commands as commands
import davosbot.main as main


class ScreenshotLogIntentTests(unittest.TestCase):
    def _run_screenshot_log(self, text, scan_message):
        sent = []
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            db_path = root / "davosbot.db"
            conn = sqlite3.connect(db_path)
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
                conn.commit()
            finally:
                conn.close()

            with patch.object(main, "BOT_DB_PATH", str(db_path)), \
                 patch.object(commands, "BOT_DB_PATH", str(db_path)), \
                 patch.object(commands, "PROJECT_ROOT", root), \
                 patch.object(commands, "check_action_permission", lambda sender, action: None), \
                 patch.object(main, "is_owner", lambda sender: True), \
                 patch.object(main, "choose_scan_provider", lambda: "gemini"), \
                 patch.object(main, "estimate_scan_time", lambda provider=None: "about 10 seconds"), \
                 patch.object(main, "send_message", lambda recipient, text, is_group=False: sent.append((recipient, text, is_group)) or True), \
                 patch.object(main, "scan_image", lambda image_path, prompt: SimpleNamespace(ok=True, message=scan_message, provider="gemini")):
                reply = main._handle_screenshot_issue_log(
                    "+15550000001",
                    text,
                    "local.png",
                    recipient="+15550000001",
                )

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute("SELECT request, reason FROM change_log").fetchone()
            finally:
                conn.close()

        return reply, row, sent

    def test_analyze_this_and_log_scans_and_writes_change_log(self):
        reply, row, sent = self._run_screenshot_log(
            "analyze this and log",
            "The screenshot shows the log did not update after a huge image scan.",
        )

        self.assertIn("Self-repair logged #1", reply)
        self.assertTrue(any("reading screenshot" in item[1] for item in sent))
        self.assertIn("[SELF-REPAIR YELLOW]", row[0])
        self.assertIn("log did not update", row[0])
        self.assertIn("source=screenshot_issue_image_scan", row[1])
        self.assertIn("image_scan_result=", row[1])
        self.assertIn("recent_bot_logs=", row[1])
        self.assertIn("relevant_db_rows=", row[1])
        self.assertIn("safe_auto_fix_pipeline=Codex only", row[1])

    def test_analyze_this_image_and_log_the_issue_scans_and_writes_change_log(self):
        reply, row, sent = self._run_screenshot_log(
            "Analyze this image and log the issue",
            "The screenshot shows the issue was captured through the image repair intake.",
        )

        self.assertIn("Self-repair logged #1", reply)
        self.assertTrue(any("reading screenshot" in item[1] for item in sent))
        self.assertIn("[SELF-REPAIR YELLOW]", row[0])
        self.assertIn("image repair intake", row[0])
        self.assertIn("source=screenshot_issue_image_scan", row[1])

    def test_analyze_this_and_log_without_image_asks_for_image(self):
        with patch.object(main, "is_owner", lambda sender: True):
            reply = main._handle_screenshot_issue_log(
                "+15550000001",
                "analyze this and log",
                None,
                recipient="+15550000001",
            )

        self.assertIn("I need the screenshot/image or exact failing text", reply)

    def test_analyze_this_image_and_log_the_issue_without_image_asks_for_image(self):
        with patch.object(main, "is_owner", lambda sender: True):
            reply = main._handle_screenshot_issue_log(
                "+15550000001",
                "Analyze this image and log the issue",
                None,
                recipient="+15550000001",
            )

        self.assertIn("I need the screenshot/image or exact failing text", reply)

    def test_normal_image_thoughts_do_not_become_screenshot_issue_log(self):
        with patch.object(main, "is_owner", lambda sender: True):
            reply = main._handle_screenshot_issue_log(
                "+15550000001",
                "analyze this image what do you think",
                "local.png",
                recipient="+15550000001",
            )

        self.assertIsNone(reply)

    def test_misspelled_analyze_this_and_log_scans_and_writes_change_log(self):
        reply, row, _sent = self._run_screenshot_log(
            "anaylze this and log",
            "Typo intent still reached image logging.",
        )

        self.assertIn("Self-repair logged #1", reply)
        self.assertIn("Typo intent still reached image logging", row[0])

    def test_misspelled_analyze_this_and_log_without_image_asks_for_image(self):
        with patch.object(main, "is_owner", lambda sender: True):
            reply = main._handle_screenshot_issue_log(
                "+15550000001",
                "anaylze this and log",
                None,
                recipient="+15550000001",
            )

        self.assertIn("I need the screenshot/image or exact failing text", reply)

    def test_log_screenshot_issue_owner_dm_scans_before_generic_log_priority(self):
        sent = []
        command_calls = []
        saved = []
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            db_path = root / "davosbot.db"
            conn = sqlite3.connect(db_path)
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
                conn.commit()
            finally:
                conn.close()

            with patch.object(main, "BOT_DB_PATH", str(db_path)), \
                 patch.object(commands, "BOT_DB_PATH", str(db_path)), \
                 patch.object(commands, "PROJECT_ROOT", root), \
                 patch.object(commands, "check_action_permission", lambda sender, action: None), \
                 patch.object(main, "_image_buffer", {}), \
                 patch.object(main, "_text_buffer", {}), \
                 patch.object(main, "is_owner", lambda sender: True), \
                 patch.object(main, "choose_scan_provider", lambda: "gemini"), \
                 patch.object(main, "estimate_scan_time", lambda provider=None: "about 10 seconds"), \
                 patch.object(main, "send_message", lambda recipient, text, is_group=False: sent.append(text) or True), \
                 patch.object(main, "save_turn", lambda *args, **kwargs: saved.append(args)), \
                 patch.object(main, "scan_image", lambda image_path, prompt: SimpleNamespace(ok=True, message="Sports cron screenshot shows the log route was intercepted.", provider="gemini")), \
                 patch.object(main, "handle_command", lambda sender, text: command_calls.append((sender, text)) or "PLAIN LOG"):
                main.handle_dm("+15550000001", "log screenshot issue", image_path="local.png")

            conn = sqlite3.connect(db_path)
            try:
                request, reason = conn.execute("SELECT request, reason FROM change_log").fetchone()
            finally:
                conn.close()

        self.assertFalse(command_calls)
        self.assertTrue(any("Self-repair logged #1" in msg for msg in sent))
        self.assertIn("Sports cron screenshot", request)
        self.assertIn("source=screenshot_issue_image_scan", reason)


if __name__ == "__main__":
    unittest.main()
