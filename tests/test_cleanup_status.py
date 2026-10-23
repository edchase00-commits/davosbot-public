import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from davosbot import commands


class CleanupStatusTests(unittest.TestCase):
    @staticmethod
    def _normalized(text: str) -> str:
        return text.replace("\\", "/")

    def test_cleanup_status_reports_running_lock_and_current_log(self):
        rows = [
            (2, "cron list UX for all chats", "", "2026-06-16 18:00:00"),
            (1, "docs cleanup", "", "2026-06-16 17:00:00"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_dir = root / ".auto_deploy" / "codex_cleanup.lock"
            log_file = root / ".auto_deploy" / "codex_cleanup_logs" / "confirmed_safe_cleanup_20260616_190000.log"
            lock_dir.mkdir(parents=True)
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("still running\n", encoding="utf-8")

            with (
                patch.object(commands, "PROJECT_ROOT", root),
                patch.object(commands, "_fetch_change_log_rows", return_value=rows),
                patch.object(commands, "check_action_permission", return_value=None),
                patch.object(commands, "cleanup_lock_state", return_value="running"),
            ):
                reply = commands._cmd_cleanup_status("+15550000001")

        self.assertIn("Codex safe cleanup status: running.", reply)
        self.assertIn("Change log: GREEN 1 | YELLOW 1 | RED 0.", reply)
        self.assertIn("Lock age:", reply)
        self.assertIn(
            "Current run log: .auto_deploy/codex_cleanup_logs/confirmed_safe_cleanup_20260616_190000.log",
            self._normalized(reply),
        )

    def test_cleanup_status_reports_idle_and_restart_hint(self):
        rows = [(1, "docs cleanup", "", "2026-06-16 17:00:00")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_file = root / ".auto_deploy" / "codex_cleanup_logs" / "nightly_safe_cleanup_20260616_030000.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("finished\n", encoding="utf-8")

            with (
                patch.object(commands, "PROJECT_ROOT", root),
                patch.object(commands, "_fetch_change_log_rows", return_value=rows),
                patch.object(commands, "check_action_permission", return_value=None),
            ):
                reply = commands._cmd_cleanup_status("+15550000001")

        self.assertIn("Codex safe cleanup status: idle.", reply)
        self.assertIn(
            "Last run log: .auto_deploy/codex_cleanup_logs/nightly_safe_cleanup_20260616_030000.log",
            self._normalized(reply),
        )
        self.assertIn("Text `yes fix` to start now, or `ship safe cleanup` for the board.", reply)

    def test_confirmed_cleanup_returns_status_when_lock_exists(self):
        rows = [(1, "docs cleanup", "", "2026-06-16 17:00:00")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_dir = root / ".auto_deploy" / "codex_cleanup.lock"
            log_file = root / ".auto_deploy" / "codex_cleanup_logs" / "confirmed_safe_cleanup_20260616_190000.log"
            lock_dir.mkdir(parents=True)
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("still running\n", encoding="utf-8")

            with (
                patch.object(commands, "PROJECT_ROOT", root),
                patch.object(commands, "_fetch_change_log_rows", return_value=rows),
                patch.object(commands, "check_action_permission", return_value=None),
                patch.object(commands, "cleanup_lock_state", return_value="running"),
            ):
                reply = commands._cmd_confirmed_safe_cleanup("+15550000001")

        self.assertIn("Codex safe cleanup status: running.", reply)
        self.assertIn("Current run log:", reply)

    def test_stale_lock_exposes_recovery_and_last_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / ".auto_deploy"
            state_dir.mkdir()
            (state_dir / "cleanup_status.json").write_text('{"state": "timed_out"}')
            with patch.object(commands, "cleanup_lock_state", return_value="stale"):
                reply = "\n".join(commands._cleanup_runner_status_lines(
                    [(1, "docs cleanup", "", "2026-06-16")], project_root=root,
                ))
        self.assertIn("status: stale", reply)
        self.assertIn("Last run outcome: timed out", reply)
        self.assertIn("recover its stale lock", reply)
        self.assertIn("Text `yes fix`", reply)

    def test_unknown_lock_does_not_offer_an_unverified_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(commands, "cleanup_lock_state", return_value="unknown"):
                reply = "\n".join(commands._cleanup_runner_status_lines(
                    [(1, "docs cleanup", "", "2026-06-16")], project_root=Path(tmp),
                ))
        self.assertIn("ownership could not be verified", reply)
        self.assertNotIn("Text `yes fix`", reply)

    def test_handle_command_routes_cleanup_status_phrasing(self):
        with (
            patch.object(commands, "is_owner", return_value=True),
            patch.object(commands, "_cmd_cleanup_status", return_value="status ok"),
        ):
            reply = commands.handle_command("+15550000001", "What's the status of Codex safe cleanup?")

        self.assertEqual("status ok", reply)

    def test_handle_command_blocks_cleanup_status_for_non_owner(self):
        with patch.object(commands, "is_owner", return_value=False):
            reply = commands.handle_command("+15550000002", "cleanup status")

        self.assertEqual("That cleanup status is owner-only.", reply)


if __name__ == "__main__":
    unittest.main()
