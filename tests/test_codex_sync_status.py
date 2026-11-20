import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import codex_sync_status


class CodexSyncStatusTests(unittest.TestCase):
    def test_workspace_kind_identifies_mini_work_checkout(self):
        self.assertEqual(
            "mini-codex-work",
            codex_sync_status.workspace_kind(Path("/Users/<mac-user>/codex-work/davosbot")),
        )

    def test_format_status_includes_safe_loop_and_runtime_boundary(self):
        status = {
            "workspace": "mini-codex-work",
            "root": "/Users/<mac-user>/codex-work/davosbot",
            "git": {
                "dirty_lines": [],
                "branch": "master",
                "head": "abc1234 test",
                "upstream": "origin/master",
                "local_sha": "abc123456",
                "origin_master_sha": "abc123456",
            },
            "auto_deploy": {"state": "up_to_date", "local_sha": "abc123456", "remote_sha": "abc123456"},
        }

        text = codex_sync_status.format_status(status)

        self.assertIn("Mini phone Codex edits /Users/<mac-user>/codex-work/davosbot", text)
        self.assertIn("production /Users/<you>/projects/davosbot is runtime/read-only", text)
        self.assertIn("local matches origin/master: yes", text)
        self.assertIn("Run this sync check at session start", text)

    def test_auto_deploy_info_handles_missing_status_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_prod = Path(tmp) / "prod"
            with patch.object(codex_sync_status, "MINI_PROD_ROOT", missing_prod):
                self.assertEqual({}, codex_sync_status.auto_deploy_info())


if __name__ == "__main__":
    unittest.main()
