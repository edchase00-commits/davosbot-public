import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from davosbot import cleanup_runner as runner


class CleanupLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lock = self.root / ".auto_deploy" / "codex_cleanup.lock"

    def owner(self, processes):
        self.lock.mkdir(parents=True, exist_ok=True)
        (self.lock / "owner.json").write_text(json.dumps({"processes": processes}))

    def test_missing_lock_is_idle(self):
        self.assertEqual(runner.cleanup_lock_state(self.root), "idle")

    def test_active_child_keeps_lock_running_after_supervisor_dies(self):
        self.owner([{"pid": 101, "started": "parent"}, {"pid": 102, "started": "child"}])
        with patch.object(runner, "_process_start", side_effect=["", "child"]):
            self.assertEqual(runner.cleanup_lock_state(self.root), "running")

    def test_dead_or_reused_pid_is_stale(self):
        self.owner([{"pid": 101, "started": "original"}])
        for actual in ("", "different process"):
            with self.subTest(actual=actual), patch.object(runner, "_process_start", return_value=actual):
                self.assertEqual(runner.cleanup_lock_state(self.root), "stale")

    def test_unverifiable_process_fails_closed(self):
        self.owner([{"pid": 101, "started": "original"}])
        with patch.object(runner, "_process_start", return_value=None):
            self.assertEqual(runner.cleanup_lock_state(self.root), "unknown")

    def test_legacy_lock_requires_process_inventory(self):
        self.lock.mkdir(parents=True)
        for active, expected in ((True, "running"), (False, "stale"), (None, "unknown")):
            with self.subTest(active=active), patch.object(runner, "_legacy_runner_active", return_value=active):
                self.assertEqual(runner.cleanup_lock_state(self.root), expected)

    def test_malformed_metadata_is_not_deleted(self):
        self.owner([])
        self.assertEqual(runner.cleanup_lock_state(self.root), "unknown")
        self.assertTrue((self.lock / "owner.json").exists())

    def test_legacy_check_ignores_own_cron_ancestors_but_detects_other_runner(self):
        own = "100 90 python cleanup_runner.py\n90 1 /bin/sh -c bash nightly_safe_cleanup_codex.sh\n"
        with (
            patch.object(runner.os, "name", "posix"),
            patch.object(runner.os, "getpid", return_value=100),
            patch.object(runner.subprocess, "run", return_value=Mock(returncode=0, stdout=own)) as ps,
        ):
            self.assertFalse(runner._legacy_runner_active())
            ps.return_value.stdout += "200 1 bash nightly_safe_cleanup_codex.sh\n"
            self.assertTrue(runner._legacy_runner_active())

    def test_supervisor_recovers_stale_metadata_and_releases_lock(self):
        self.owner([{"pid": 101, "started": "old"}])
        guard = types.SimpleNamespace(flock=Mock(), LOCK_EX=1, LOCK_NB=2)
        process = Mock(pid=202)
        process.wait.return_value = 0
        with (
            patch.dict(sys.modules, {"fcntl": guard}),
            patch.object(runner, "cleanup_lock_state", return_value="stale"),
            patch.object(runner, "_process_start", return_value="current"),
            patch.object(runner.subprocess, "Popen", return_value=process) as popen,
        ):
            code = runner.supervise(self.root, ["bash", "runner.sh"], 60)
        self.assertEqual(code, 0)
        self.assertFalse(self.lock.exists())
        self.assertEqual(json.loads((self.root / ".auto_deploy" / "cleanup_status.json").read_text())["state"], "finished")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(len(popen.call_args.kwargs["pass_fds"]), 1)
        self.assertEqual(popen.call_args.kwargs["env"]["DAVOSBOT_CLEANUP_SUPERVISED"], "1")

    def test_busy_os_guard_never_starts_another_process(self):
        guard = types.SimpleNamespace(flock=Mock(side_effect=BlockingIOError), LOCK_EX=1, LOCK_NB=2)
        with patch.dict(sys.modules, {"fcntl": guard}), patch.object(runner.subprocess, "Popen") as popen:
            self.assertEqual(runner.supervise(self.root, ["bash", "runner.sh"], 60), 75)
        popen.assert_not_called()

    def test_failed_launch_releases_lock_and_records_failure(self):
        guard = types.SimpleNamespace(flock=Mock(), LOCK_EX=1, LOCK_NB=2)
        with (
            patch.dict(sys.modules, {"fcntl": guard}),
            patch.object(runner, "_process_start", return_value="current"),
            patch.object(runner.subprocess, "Popen", side_effect=OSError("failed")),
        ):
            with self.assertRaises(OSError):
                runner.supervise(self.root, ["missing"], 60)
        self.assertFalse(self.lock.exists())
        self.assertEqual(json.loads((self.root / ".auto_deploy" / "cleanup_status.json").read_text())["state"], "failed")

    def test_metadata_failure_after_spawn_stops_child_before_unlock(self):
        guard = types.SimpleNamespace(flock=Mock(), LOCK_EX=1, LOCK_NB=2)
        process = Mock(pid=202)
        with (
            patch.dict(sys.modules, {"fcntl": guard}),
            patch.object(runner, "_process_start", return_value="current"),
            patch.object(runner.subprocess, "Popen", return_value=process),
            patch.object(runner, "_write_status", side_effect=[None, OSError("failed"), None]),
            patch.object(runner, "_stop_process_group") as stop,
        ):
            with self.assertRaises(OSError):
                runner.supervise(self.root, ["bash", "runner.sh"], 60)
        stop.assert_called_once_with(process)
        self.assertFalse(self.lock.exists())


class CleanupTimeoutTests(unittest.TestCase):
    def test_timeout_stops_child_and_returns_timeout_result(self):
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("cleanup", 60)
        with patch.object(runner, "_stop_process_group") as stop:
            self.assertEqual(runner.wait_for_cleanup(process, 60), (124, "timed_out"))
        stop.assert_called_once_with(process)

    def test_real_child_cannot_outlive_timeout(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            self.assertEqual(runner.wait_for_cleanup(process, 0.05), (124, "timed_out"))
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


class CleanupScriptPolicyTests(unittest.TestCase):
    def test_runner_uses_isolated_branch_and_integrator(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "nightly_safe_cleanup_codex.sh").read_text(encoding="utf-8")
        self.assertIn('git worktree add -b "$RUN_BRANCH" "$RUN_DIR" origin/master', source)
        self.assertIn('RUN_BRANCH="codex/cleanup-$RUN_ID"', source)
        self.assertIn('git push -u origin "$DAVOSBOT_CLEANUP_RUN_BRANCH"', source)
        self.assertIn("GitHub fast integrator", source)
        self.assertIn("CODEX_CLEANUP_TIMEOUT_SECONDS:-7200", source)
        self.assertIn('git merge-base --is-ancestor HEAD origin/master', source)
        self.assertNotIn("git checkout master", source)
        self.assertNotIn("push origin master", source)
        self.assertNotIn("git worktree remove --force", source)


if __name__ == "__main__":
    unittest.main()
