import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from davosbot import commands


class ConfirmedCleanupAutorunTests(unittest.TestCase):
    def test_confirmed_cleanup_starts_shared_runner_with_notify(self):
        rows = [(1, "image scan fix", "", "2026-06-02 00:00:00")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "scripts" / "nightly_safe_cleanup_codex.sh"
            script.parent.mkdir(parents=True)
            script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

            with (
                patch.object(commands, "PROJECT_ROOT", root),
                patch.object(commands, "_fetch_change_log_rows", return_value=rows),
                patch.object(commands, "check_action_permission", return_value=None),
                patch.object(commands, "_can_autorun_cleanup_here", return_value=True),
                patch.object(commands.subprocess, "Popen") as popen,
            ):
                reply = commands._cmd_confirmed_safe_cleanup("+15550000001")

        self.assertIn("Starting Codex safe cleanup", reply)
        popen.assert_called_once()
        args = popen.call_args.args[0]
        self.assertEqual(["/bin/bash", str(script), "--confirmed", "--notify"], args)

    def test_confirmed_cleanup_falls_back_to_prompt_off_mini(self):
        rows = [(1, "docs cleanup", "", "2026-06-02 00:00:00")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "scripts" / "nightly_safe_cleanup_codex.sh"
            script.parent.mkdir(parents=True)
            script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

            with (
                patch.object(commands, "PROJECT_ROOT", root),
                patch.object(commands, "_fetch_change_log_rows", return_value=rows),
                patch.object(commands, "check_action_permission", return_value=None),
                patch.object(commands, "_can_autorun_cleanup_here", return_value=False),
            ):
                reply = commands._cmd_confirmed_safe_cleanup("+15550000001")

        self.assertIn("Auto-run cleanup is Mini-only", reply)
        self.assertIn("Copy/paste this into Codex", reply)

    def test_stale_lock_allows_the_supervised_runner_to_recover(self):
        rows = [(1, "docs cleanup", "", "2026-06-02 00:00:00")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "scripts" / "nightly_safe_cleanup_codex.sh"
            script.parent.mkdir()
            script.write_text("#!/bin/bash\n")
            (root / ".auto_deploy" / "codex_cleanup.lock").mkdir(parents=True)
            with (
                patch.object(commands, "PROJECT_ROOT", root),
                patch.object(commands, "_fetch_change_log_rows", return_value=rows),
                patch.object(commands, "check_action_permission", return_value=None),
                patch.object(commands, "_can_autorun_cleanup_here", return_value=True),
                patch.object(commands, "cleanup_lock_state", return_value="stale"),
                patch.object(commands.subprocess, "Popen") as popen,
            ):
                reply = commands._cmd_confirmed_safe_cleanup("+15550000001")
        self.assertIn("Starting Codex safe cleanup", reply)
        popen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
