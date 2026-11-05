"""Real harmless child processes, with synthetic live files and fake Git only."""

from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from scripts import auto_deploy


SOURCE = Path(__file__).resolve().parents[1]
SHA = "b" * 40


def shell_command(*args):
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


class PreflightIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.live = self.base / "synthetic-live"
        self.live.mkdir()
        self.config = auto_deploy.DeployConfig(
            enabled=True, dry_run=False, repo_root=self.live, remote="origin", branch="master",
            github_repo="example/synthetic", required_workflow="tests", require_ci=True,
            poll_seconds=120, command_timeout_seconds=20,
            preflight_command=shell_command(sys.executable, "candidate.py"),
            post_merge_command="post-check", restart_command="restart", exit_after_deploy=True,
            worktree_root=self.live / ".auto_deploy" / "worktrees", status_path=self.live / "status.json",
        )
        self.canary = self.live / "live.sqlite"
        with sqlite3.connect(self.canary) as conn:
            conn.execute("CREATE TABLE canary(value TEXT)")
            conn.execute("INSERT INTO canary VALUES ('unchanged')")
        conn.close()
        self.private = self.live / "private-canary.txt"
        self.private.write_text("Synthetic private canary, never for a test child.", encoding="utf-8")
        self.original = {path: path.read_bytes() for path in (self.canary, self.private)}
        self.parent_env = {
            "HOME": str(self.live), "USERPROFILE": str(self.live),
            "BOT_DB_PATH": str(self.canary), "DB_PATH": str(self.canary),
            "SOUL_PATH": str(self.private), "MEMORY_PATH": str(self.private),
            "GENERATED_DIR": str(self.live), "IMAGE_OUTPUT_DIR": str(self.live),
            "OPENAI_IMAGE_OUTPUT_DIR": str(self.live), "FANTASY_ACCESS_PRIVATE_KEY_PATH": str(self.private),
            "GEMINI_API_KEY": "synthetic-parent-secret", "OPENAI_API_KEY": "synthetic-parent-secret",
            "TAVILY_API_KEY": "synthetic-parent-secret", "ADMIN_PASSWORD": "synthetic-parent-secret",
            "GITHUB_TOKEN": "synthetic-parent-secret", "OWNER_ALERT_WEBHOOK_URL": "synthetic-parent-secret",
            "UNLISTED_PRIVATE_CREDENTIAL": "synthetic-parent-secret", "OWNER_ID": "private-parent-owner",
            "PYTHON": "inherited-python-must-not-run", "PYTHONPATH": str(self.live),
            "PYTHON_DOTENV_DISABLED": "0",
        }
        self.body = "print('synthetic child passed')\n"
        self.calls = []
        self.environments = []

    def runner(self, command, *, cwd, timeout, shell=False, env=None):
        self.calls.append((command, Path(cwd), env))
        if isinstance(command, list) and command[:3] == ["git", "worktree", "add"]:
            worktree = Path(command[-2])
            worktree.mkdir(parents=True)
            (worktree / "candidate.py").write_text(self.body, encoding="utf-8")
            (worktree / "davosbot").mkdir()
            (worktree / "davosbot" / "__init__.py").write_text("", encoding="utf-8")
            shutil.copyfile(SOURCE / "davosbot" / "config.py", worktree / "davosbot" / "config.py")
            # The child's normal config import must not load even this synthetic dotenv.
            (worktree / ".env").write_text("PREFLIGHT_DOTENV_CANARY=synthetic-private\n", encoding="utf-8")
            (worktree / "scripts").mkdir()
            (worktree / "scripts" / "__init__.py").write_text("", encoding="utf-8")
            (worktree / "scripts" / "quality_check.py").write_text(
                "raise RuntimeError('candidate must not supply the watcher isolation policy')\n", encoding="utf-8")
            return auto_deploy.CommandResult(0)
        if isinstance(command, list) and command[:3] == ["git", "worktree", "remove"]:
            # Production cleanup owns the following temporary tree removal.
            return auto_deploy.CommandResult(0)
        self.environments.append(dict(env))
        return auto_deploy.run_command(command, cwd=cwd, timeout=timeout, shell=shell, env=env)

    def run_preflight(self, **changes):
        with patch.dict(os.environ, self.parent_env):
            before = dict(os.environ)
            result = auto_deploy._run_preflight(replace(self.config, **changes), SHA, self.runner)
            self.assertEqual(before, dict(os.environ), "Watcher environment was changed")
        return result

    def assert_clean(self):
        self.assertFalse(auto_deploy._safe_worktree_path(self.config, SHA).exists())
        self.assertEqual([], list(self.config.worktree_root.glob("*.cleanup-unverified.json")))
        for env in self.environments:
            self.assertFalse(Path(env["HOME"]).exists())
        for path, content in self.original.items():
            self.assertEqual(content, path.read_bytes())

    def test_real_candidate_uses_synthetic_state_without_inherited_credentials_or_dotenv(self):
        self.body = '''import os, pathlib, sqlite3, sys
from davosbot import config
state = pathlib.Path(os.environ["HOME"]).resolve()
assert not state.is_relative_to(pathlib.Path.cwd().resolve())
assert pathlib.Path(os.environ["PYTHON"]).resolve() == pathlib.Path(sys.executable).resolve()
assert "PYTHONPATH" not in os.environ
assert "PREFLIGHT_DOTENV_CANARY" not in os.environ
assert "UNLISTED_PRIVATE_CREDENTIAL" not in os.environ
assert "GITHUB_TOKEN" not in os.environ
for key in ("GEMINI_API_KEY", "OPENAI_API_KEY", "TAVILY_API_KEY", "ADMIN_PASSWORD", "OWNER_ALERT_WEBHOOK_URL"):
    assert os.environ[key] == ""
for key in ("BOT_DB_PATH", "DB_PATH", "SOUL_PATH", "MEMORY_PATH", "GENERATED_DIR", "IMAGE_OUTPUT_DIR",
            "OPENAI_IMAGE_OUTPUT_DIR", "FANTASY_ACCESS_PRIVATE_KEY_PATH"):
    assert pathlib.Path(os.environ[key]).resolve().is_relative_to(state)
assert config.OWNER_ID == "+15550000001"
assert "Synthetic" in pathlib.Path(config.SOUL_PATH).read_text()
assert pathlib.Path(config.MEMORY_PATH).read_text() == ""
with sqlite3.connect(config.BOT_DB_PATH) as conn:
    conn.execute("CREATE TABLE test_write(value TEXT)")
    conn.execute("INSERT INTO test_write VALUES ('synthetic')")
conn.close()
pathlib.Path(config.SOUL_PATH).write_text("Synthetic changed only in preflight")
pathlib.Path(config.MEMORY_PATH).write_text("Synthetic changed only in preflight")
pathlib.Path(config.GENERATED_DIR).mkdir()
(pathlib.Path(config.GENERATED_DIR) / "test-output.txt").write_text("synthetic")
print("synthetic isolation verified")
'''
        result = self.run_preflight()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("synthetic isolation verified", result.stdout.strip())
        self.assertEqual(self.config.preflight_command, self.calls[1][0])
        self.assertIsNone(self.calls[0][2])  # Git retains normal watcher environment.
        self.assertIsNone(self.calls[-1][2])
        self.assert_clean()

    def test_custom_command_failure_is_retained_and_state_is_removed(self):
        self.body = "import sys\nprint('synthetic stdout')\nprint('synthetic stderr', file=sys.stderr)\nraise SystemExit(9)\n"
        result = self.run_preflight()
        self.assertEqual(9, result.returncode)
        self.assertEqual("synthetic stdout", result.stdout.strip())
        self.assertEqual("synthetic stderr", result.stderr.strip())
        self.assert_clean()

    def test_subsequent_runtime_command_still_inherits_original_environment(self):
        self.assertEqual(0, self.run_preflight().returncode)
        code = ("import os; assert os.environ['GITHUB_TOKEN'] == 'synthetic-parent-secret'; "
                "assert os.environ['PYTHON'] == 'inherited-python-must-not-run'; print('runtime env retained')")
        with patch.dict(os.environ, self.parent_env):
            result = auto_deploy.run_command([sys.executable, "-c", code], cwd=self.live, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("runtime env retained", result.stdout.strip())
        self.assert_clean()

    def test_state_inside_live_checkout_is_rejected_before_shell_runs(self):
        with patch.object(auto_deploy.tempfile, "gettempdir", return_value=str(self.live)), \
             self.assertRaisesRegex(RuntimeError, "outside the live checkout"):
            self.run_preflight()
        self.assertEqual([], self.environments)
        self.assert_clean()

    def test_setup_error_removes_candidate_without_running_it(self):
        with patch.object(auto_deploy, "_isolated_test_environment", side_effect=OSError("synthetic setup failure")), \
             self.assertRaises(OSError):
            self.run_preflight()
        self.assertEqual([], self.environments)
        self.assert_clean()

    def test_marker_write_failure_prevents_candidate_execution(self):
        with patch.object(auto_deploy.os, "open", side_effect=OSError("synthetic disk failure")), \
             self.assertRaises(OSError):
            self.run_preflight()
        self.assertEqual([], self.calls)
        self.assert_clean()

    def test_concurrent_marker_claim_cannot_remove_another_held_worktree(self):
        worktree = auto_deploy._safe_worktree_path(self.config, SHA)
        marker = worktree.with_name(worktree.name + ".cleanup-unverified.json")
        original_open = os.open
        def concurrent_claim(path, flags, mode=0o777, **kwargs):
            if Path(path) == marker:
                worktree.mkdir(parents=True)
                (worktree / "held-canary").write_text("must remain")
                marker.write_text("unverified prior worker")
            return original_open(path, flags, mode, **kwargs)
        with patch.object(auto_deploy.os, "open", side_effect=concurrent_claim), \
             self.assertRaises(FileExistsError):
            self.run_preflight()
        self.assertEqual([], self.calls)
        self.assertEqual("unverified prior worker", marker.read_text())
        self.assertEqual("must remain", (worktree / "held-canary").read_text())

    def test_timeout_terminates_descendants_before_state_cleanup(self):
        # Repeated heartbeat proves no execution after return, without assuming
        # Windows taskkill starts instantly at the timeout boundary.
        heartbeat = self.base / "synthetic-heartbeat"
        child = ("import pathlib,time\np=pathlib.Path(" + repr(str(heartbeat)) + ")\n"
                 "while True:\n p.write_text(str(time.monotonic_ns()))\n time.sleep(.03)\n")
        self.body = ("import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c'," + repr(child) + "])\n"
                     "time.sleep(60)\n")
        result = self.run_preflight(command_timeout_seconds=2)
        self.assertEqual(124, result.returncode)
        self.assertTrue(heartbeat.exists(), "Descendant did not start; timeout test did not exercise cleanup")
        before = heartbeat.read_bytes()
        time.sleep(.35)
        self.assertEqual(before, heartbeat.read_bytes())
        self.assert_clean()

    @unittest.skipIf(os.name == "nt", "Normal Windows preflights require foreground commands; no Job Object ownership")
    def test_posix_normal_and_failed_exit_stop_background_children_with_closed_pipes(self):
        for exit_code in (0, 9):
            with self.subTest(exit_code=exit_code):
                heartbeat = self.base / ("background-heartbeat-" + str(exit_code))
                child = ("import pathlib,time\np=pathlib.Path(" + repr(str(heartbeat)) + ")\n"
                         "while True:\n p.write_text(str(time.monotonic_ns()))\n time.sleep(.03)\n")
                self.body = ("import pathlib,subprocess,sys,time\n"
                             "subprocess.Popen([sys.executable,'-c'," + repr(child) + "], "
                             "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                             "p=pathlib.Path(" + repr(str(heartbeat)) + ")\n"
                             "while not p.exists(): time.sleep(.01)\n"
                             "raise SystemExit(" + str(exit_code) + ")\n")
                result = self.run_preflight()
                self.assertEqual(exit_code, result.returncode, result.stderr)
                before = heartbeat.read_bytes()
                time.sleep(.35)
                self.assertEqual(before, heartbeat.read_bytes())
                self.assert_clean()

    def test_windows_taskkill_failure_is_not_reported_as_verified_cleanup(self):
        process = Mock(pid=123, poll=Mock(return_value=None))
        with patch.object(auto_deploy.os, "name", "nt"), \
             patch.object(auto_deploy.subprocess, "run", return_value=Mock(returncode=1)), \
             self.assertRaises(auto_deploy.PreflightCleanupError):
            auto_deploy._stop_isolated_processes(process, abnormal=True)
        process.kill.assert_called_once()
        process.communicate.assert_called_once_with(timeout=10)

    def test_unexpected_communicate_errors_always_request_process_cleanup(self):
        errors = (OSError("synthetic pipe failure"), UnicodeDecodeError("utf8", b"\xff", 0, 1, "synthetic"))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                process = Mock()
                process.communicate.side_effect = error
                with patch.object(auto_deploy.subprocess, "Popen", return_value=process), \
                     patch.object(auto_deploy, "_stop_isolated_processes") as stop, \
                     self.assertRaises(type(error)):
                    auto_deploy._run_isolated_command("synthetic", cwd=self.base, timeout=1, shell=True, env={})
                stop.assert_called_once_with(process, abnormal=True)

    @unittest.skipIf(os.name == "nt", "Background child ownership is POSIX-only; Windows cleanup errors are mocked")
    def test_invalid_output_with_background_child_fails_and_stops_child(self):
        heartbeat = self.base / "invalid-output-heartbeat"
        child = ("import pathlib,time\np=pathlib.Path(" + repr(str(heartbeat)) + ")\n"
                 "while True:\n p.write_text(str(time.monotonic_ns()))\n time.sleep(.03)\n")
        self.body = ("import os,pathlib,subprocess,sys,time\n"
                     "subprocess.Popen([sys.executable,'-c'," + repr(child) + "], "
                     "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                     "p=pathlib.Path(" + repr(str(heartbeat)) + ")\n"
                     "while not p.exists(): time.sleep(.01)\n"
                     "os.write(1,bytes([255]))\n")
        with self.assertRaises(auto_deploy.PreflightCleanupError):
            self.run_preflight()
        state = Path(self.environments[0]["HOME"])
        self.addCleanup(shutil.rmtree, state)
        self.assertTrue(state.exists())
        self.assertTrue(auto_deploy._safe_worktree_path(self.config, SHA).exists())
        before = heartbeat.read_bytes()
        time.sleep(.35)
        self.assertEqual(before, heartbeat.read_bytes())

    def test_cleanup_communicate_timeout_retains_state_and_worktree(self):
        process = Mock(pid=123, poll=Mock(return_value=0))
        process.communicate.side_effect = subprocess.TimeoutExpired("synthetic child", 10)
        def fail_cleanup(*_args, **_kwargs):
            with patch.object(auto_deploy.os, "name", "nt"):
                auto_deploy._stop_isolated_processes(process, abnormal=False)
        with patch.object(auto_deploy, "run_command", side_effect=fail_cleanup), \
             self.assertRaises(auto_deploy.PreflightCleanupError):
            self.run_preflight()
        self.assertTrue(auto_deploy._safe_worktree_path(self.config, SHA).exists())
        state = Path(self.environments[0]["HOME"])
        self.assertTrue(state.exists())
        # Only this synthetic fixture is removed; production cleanup deliberately
        # retains uncertain state for operator inspection.
        self.addCleanup(shutil.rmtree, state)
        marker = self.config.worktree_root / (SHA[:12] + ".cleanup-unverified.json")
        self.assertEqual({"version": 1, "target_sha": SHA, "state_path": str(state)},
                         json.loads(marker.read_text()))
        before = len(self.calls)
        with self.assertRaisesRegex(RuntimeError, "operator review required"):
            self.run_preflight()
        self.assertEqual(before, len(self.calls), "Held SHA reached destructive worktree cleanup")
        other_sha = "c" * 40
        result = auto_deploy._run_preflight(self.config, other_sha, self.runner)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(marker.exists())
        self.assertTrue(state.exists())
        self.assertTrue(auto_deploy._safe_worktree_path(self.config, SHA).exists())
        self.assertFalse(auto_deploy._safe_worktree_path(self.config, other_sha).exists())
        for path, content in self.original.items():
            self.assertEqual(content, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
