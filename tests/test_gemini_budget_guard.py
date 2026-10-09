import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from davosbot import billing, openai_images


def _init_usage_db(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE gemini_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                prompt_tokens INTEGER NOT NULL,
                candidates_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL,
                source TEXT NOT NULL
            )
            """
        )
    finally:
        conn.close()


class GeminiBudgetGuardTests(unittest.TestCase):
    def test_usage_logging_persists_each_source_and_summary_after_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "usage.sqlite")
            _init_usage_db(db_path)
            with patch.object(billing, "BOT_DB_PATH", db_path):
                billing.log_gemini_usage(100, 20, 120, "image_generation")
                billing.log_gemini_usage(200, 30, 230, "image_scan")
                summary = billing.get_gemini_usage_summary("all")
            with closing(sqlite3.connect(db_path)) as conn:
                rows = conn.execute(
                    "SELECT prompt_tokens, candidates_tokens, total_tokens, source FROM gemini_usage ORDER BY id",
                ).fetchall()

        self.assertEqual([(100, 20, 120, "image_generation"), (200, 30, 230, "image_scan")], rows)
        self.assertEqual((300, 50, 350, 2), (
            summary.prompt_tokens, summary.candidates_tokens, summary.total_tokens, summary.calls,
        ))

    def test_failed_usage_commit_rolls_back_new_row_and_keeps_existing_usage(self):
        class FailingCommitConnection(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("synthetic commit failure")

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "usage.sqlite")
            _init_usage_db(db_path)
            failing_sqlite = SimpleNamespace(
                connect=lambda path: sqlite3.connect(path, factory=FailingCommitConnection),
            )
            with patch.object(billing, "BOT_DB_PATH", db_path):
                billing.log_gemini_usage(10, 5, 15, "existing")
                with patch.object(billing, "sqlite3", failing_sqlite), patch.object(billing.logger, "warning") as warning:
                    billing.log_gemini_usage(100, 50, 150, "failed")
            with closing(sqlite3.connect(db_path)) as conn:
                rows = conn.execute("SELECT source, total_tokens FROM gemini_usage ORDER BY id").fetchall()

        warning.assert_called_once()
        self.assertEqual([("existing", 15)], rows)

    def test_disabled_gemini_blocks_calls_before_api(self):
        with patch.object(billing, "GEMINI_ENABLED", False):
            decision = billing.check_gemini_budget("direct")

        self.assertFalse(decision.allowed)
        self.assertIn("GEMINI_ENABLED=false", decision.reason)

    def test_daily_budget_blocks_and_alerts_without_printing_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            _init_usage_db(db_path)
            with patch.object(billing, "BOT_DB_PATH", db_path):
                billing.log_gemini_usage(1_000_000, 1_000_000, 2_000_000, "direct")

            with (
                patch.object(billing, "BOT_DB_PATH", db_path),
                patch.object(billing, "GEMINI_ENABLED", True),
                patch.object(billing, "GEMINI_DAILY_ALERT_USD", 0.10),
                patch.object(billing, "GEMINI_DAILY_BUDGET_USD", 0.50),
                patch.object(billing, "GEMINI_BUDGET_ALERT_COOLDOWN_MINUTES", 0.01),
                patch("davosbot.alerts.send_owner_alert", return_value=True) as alert,
            ):
                billing._LAST_GEMINI_BUDGET_ALERT_AT = 0
                decision = billing.check_gemini_budget("agentic")

        self.assertFalse(decision.allowed)
        self.assertIn("budget reached", decision.reason)
        alert.assert_called_once()
        payload = str(alert.call_args)
        self.assertNotIn("GEMINI_API_KEY", payload)
        self.assertIn("gemini_budget_blocked", payload)

    def test_gemini_image_generation_obeys_budget_guard_before_api(self):
        calls = []

        def fake_post(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("API should not be called")

        with (
            patch.object(openai_images, "GEMINI_API_KEY", "test-key"),
            patch.object(openai_images, "check_gemini_budget", return_value=billing.GeminiBudgetDecision(False, "blocked")),
            patch.object(openai_images.requests, "post", fake_post),
        ):
            result = openai_images.generate_gemini_image("cat")

        self.assertFalse(result.ok)
        self.assertEqual([], calls)
        self.assertIn("spend guard", result.message)


if __name__ == "__main__":
    unittest.main()
