import tempfile
import unittest
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GitHookTests(unittest.TestCase):
    def test_commit_msg_hook_documents_conventional_commits(self):
        hook = ROOT / ".githooks" / "commit-msg"

        text = hook.read_text(encoding="utf-8")

        self.assertTrue(text.startswith("#!/bin/sh"))
        self.assertIn("Conventional commit required", text)
        self.assertIn("feat|fix|docs", text)
        self.assertIn("ops", text)

    def test_install_script_enables_all_repo_hooks(self):
        script = (ROOT / "scripts" / "install_git_hooks.sh").read_text(encoding="utf-8")

        self.assertIn("git config core.hooksPath .githooks", script)
        self.assertIn(".githooks/pre-commit", script)
        self.assertIn(".githooks/pre-push", script)
        self.assertIn(".githooks/post-merge", script)
        self.assertIn(".githooks/commit-msg", script)

    def test_mini_cron_installer_manages_davos_crons(self):
        script = (ROOT / "scripts" / "install_mini_crons.sh").read_text(encoding="utf-8")

        self.assertIn("BEGIN davosbot-managed-crons", script)
        self.assertIn("cleanup_monitor_dm.py", script)
        self.assertIn('DAVOSBOT_CLEANUP_MONITOR_ENABLED:-false', script)
        self.assertIn("$cleanup_monitor_line", script)
        self.assertIn("Cleanup monitor DM: disabled", script)
        self.assertIn("maintenance_diagnostics.py --update-state", script)
        self.assertIn("quality_sweep.py --mode light --fix", script)
        self.assertIn("quality_sweep.py --mode full --fix", script)
        self.assertIn("nightly_safe_cleanup_codex.sh", script)
        self.assertIn("crontab \"$tmp_next\"", script)

    def test_mini_cron_installer_prunes_legacy_empty_commit_backup(self):
        script = (ROOT / "scripts" / "install_mini_crons.sh").read_text(encoding="utf-8")

        self.assertIn("git add MEMORY.md", script)
        self.assertIn("auto: memory backup", script)
        self.assertIn("--allow-empty", script)

    def test_pre_commit_runs_repo_cleanliness_guard(self):
        hook = ROOT / ".githooks" / "pre-commit"

        text = hook.read_text(encoding="utf-8")

        self.assertTrue(text.startswith("#!/bin/sh"))
        self.assertIn("scripts/check_repo_cleanliness.py --staged", text)

    def test_pre_push_runs_repo_cleanliness_guard(self):
        hook = ROOT / ".githooks" / "pre-push"

        text = hook.read_text(encoding="utf-8")

        self.assertTrue(text.startswith("#!/bin/sh"))
        self.assertIn("scripts/check_repo_cleanliness.py --all", text)

    def test_publish_public_snapshot_validates_before_push(self):
        script_path = ROOT / "scripts" / "publish_public_snapshot.ps1"
        if not script_path.exists():
            self.skipTest("private publish script is not included in public snapshots")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("export_public_snapshot.ps1", script)
        self.assertIn("unittest discover -s tests", script)
        self.assertIn("private-marker scan", script)
        self.assertIn("scripts/clean_public_snapshot.py", script)
        self.assertIn("scripts/scan_public_snapshot.py", script)
        self.assertIn("[switch]$DryRun", script)
        self.assertIn("-DryRun", script)
        self.assertIn("git push --force origin main", script)

    def test_ci_supports_private_and_public_snapshot_modes(self):
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

        self.assertIn("Test-Path -LiteralPath $exportScript", workflow)
        self.assertIn("scripts/clean_public_snapshot.py", workflow)
        self.assertIn("scripts/scan_public_snapshot.py", workflow)
        self.assertIn("publish-public-snapshot", workflow)
        self.assertIn("github.event_name == 'workflow_dispatch'", workflow)
        self.assertIn("PUBLIC_SNAPSHOT_TOKEN", workflow)
        self.assertIn("publish_public_snapshot.ps1", workflow)

    def test_public_export_removes_private_publish_entrypoints(self):
        script_path = ROOT / "scripts" / "export_public_snapshot.ps1"
        if not script_path.exists():
            self.skipTest("private export script is not included in public snapshots")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("scripts/export_public_snapshot.ps1", script)
        self.assertIn("scripts/publish_public_snapshot.ps1", script)
        self.assertIn("To refresh the shareable public snapshot", script)
        self.assertIn("$allowlistEntries", script)
        self.assertIn("[switch]$DryRun", script)
        self.assertIn("Public snapshot dry run", script)
        self.assertIn(".githooks", script)
        self.assertIn("docs/public", script)

    def test_commit_msg_hook_accepts_and_rejects_expected_subjects(self):
        shell = shutil.which("sh")
        if shell is None:
            self.skipTest("sh is not available on this Windows host")
        hook = ROOT / ".githooks" / "commit-msg"
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            msg = Path(tmp) / "COMMIT_EDITMSG"
            msg.write_text("fix: make logs easier to fetch\n", encoding="utf-8")
            ok = subprocess.run([shell, str(hook), str(msg)], capture_output=True, text=True)
            self.assertEqual(0, ok.returncode, ok.stderr)

            msg.write_text("make logs easier to fetch\n", encoding="utf-8")
            bad = subprocess.run([shell, str(hook), str(msg)], capture_output=True, text=True)
            self.assertNotEqual(0, bad.returncode)
            self.assertIn("Conventional commit required", bad.stderr)


if __name__ == "__main__":
    unittest.main()
