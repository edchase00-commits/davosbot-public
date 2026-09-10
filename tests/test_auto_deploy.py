import importlib.util
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "auto_deploy.py"
SPEC = importlib.util.spec_from_file_location("auto_deploy", MODULE_PATH)
auto_deploy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = auto_deploy
SPEC.loader.exec_module(auto_deploy)


def _config(**overrides):
    root = Path(overrides.pop("repo_root", Path.cwd()))
    return auto_deploy.DeployConfig(
        enabled=overrides.pop("enabled", True),
        dry_run=overrides.pop("dry_run", False),
        repo_root=root,
        remote=overrides.pop("remote", "origin"),
        branch=overrides.pop("branch", "master"),
        github_repo=overrides.pop("github_repo", "example/davosbot"),
        required_workflow=overrides.pop("required_workflow", "tests"),
        require_ci=overrides.pop("require_ci", True),
        poll_seconds=overrides.pop("poll_seconds", 120),
        command_timeout_seconds=overrides.pop("command_timeout_seconds", 30),
        preflight_command=overrides.pop("preflight_command", "true"),
        post_merge_command=overrides.pop("post_merge_command", "true"),
        restart_command=overrides.pop("restart_command", "true"),
        exit_after_deploy=overrides.pop("exit_after_deploy", True),
        worktree_root=overrides.pop("worktree_root", root / ".auto_deploy" / "worktrees"),
        status_path=overrides.pop("status_path", root / ".auto_deploy" / "status.json"),
    )


class AutoDeployTests(unittest.TestCase):
    def test_project_root_is_importable_for_alerts(self):
        self.assertIn(str(auto_deploy.PROJECT_ROOT), sys.path)

    def test_remote_matches_private_repo_forms(self):
        self.assertTrue(
            auto_deploy._remote_matches_repo(
                "git@github.com:example/davosbot.git",
                "example/davosbot",
            )
        )
        self.assertTrue(
            auto_deploy._remote_matches_repo(
                "https://github.com/example/davosbot.git",
                "example/davosbot",
            )
        )
        self.assertFalse(
            auto_deploy._remote_matches_repo(
                "git@github.com:someone-else/davosbot.git",
                "example/davosbot",
            )
        )

    def test_ci_runs_require_green_matching_workflow(self):
        sha = "a" * 40
        runs = [
            {"head_sha": sha, "name": "tests", "status": "completed", "conclusion": "success"},
            {"head_sha": "b" * 40, "name": "tests", "status": "completed", "conclusion": "failure"},
        ]
        status = auto_deploy._ci_runs_status(runs, sha=sha, required_workflow="tests")
        self.assertTrue(status.ok)

    def test_ci_runs_block_pending_or_failed(self):
        sha = "a" * 40
        pending = [{"head_sha": sha, "name": "tests", "status": "in_progress", "conclusion": None}]
        failed = [{"head_sha": sha, "name": "tests", "status": "completed", "conclusion": "failure"}]
        self.assertFalse(auto_deploy._ci_runs_status(pending, sha=sha, required_workflow="tests").ok)
        self.assertFalse(auto_deploy._ci_runs_status(failed, sha=sha, required_workflow="tests").ok)

    def test_gh_executable_uses_common_homebrew_fallback(self):
        with TemporaryDirectory() as tmp:
            fake_gh = Path(tmp) / "gh"
            fake_gh.write_text("#!/bin/sh\n", encoding="utf-8")
            with patch.dict(auto_deploy.os.environ, {"AUTO_DEPLOY_GH_PATH": ""}, clear=False), \
                 patch.object(auto_deploy.shutil, "which", return_value=None), \
                 patch.object(auto_deploy, "_GH_FALLBACK_PATHS", (str(fake_gh),)):
                self.assertEqual(str(fake_gh), auto_deploy._gh_executable())

    def test_main_exits_after_deploy_for_pm2_supervisor_restart(self):
        config = _config(exit_after_deploy=True)
        outcome = auto_deploy.DeployOutcome("deployed", "deployed abc1234", "old", "new")

        with patch.object(auto_deploy.sys, "argv", ["auto_deploy.py"]), \
             patch.object(auto_deploy, "load_config", return_value=config), \
             patch.object(auto_deploy, "deploy_once", return_value=outcome), \
             patch.object(auto_deploy, "_write_status"), \
             patch.object(auto_deploy.time, "sleep") as sleep:
            result = auto_deploy.main()

        self.assertEqual(0, result)
        sleep.assert_not_called()

    def test_ci_status_uses_resolved_gh_path(self):
        sha = "a" * 40
        calls = []

        def runner(command, *, cwd, timeout, shell=False):
            calls.append(command)
            return auto_deploy.CommandResult(
                0,
                '[{"headSha":"%s","name":"tests","status":"completed","conclusion":"success"}]' % sha,
                "",
            )

        with patch.dict(auto_deploy.os.environ, {"AUTO_DEPLOY_GH_PATH": "/opt/homebrew/bin/gh"}, clear=False):
            status = auto_deploy._ci_status_via_gh(_config(), sha, runner=runner)

        self.assertTrue(status.ok)
        self.assertEqual("/opt/homebrew/bin/gh", calls[0][0])

    def test_disabled_never_runs_git(self):
        config = _config(enabled=False)

        def runner(*args, **kwargs):
            raise AssertionError("runner should not be called when disabled")

        outcome = auto_deploy.deploy_once(config, runner=runner)
        self.assertEqual(outcome.state, "disabled")

    def test_dirty_live_checkout_blocks_before_fetch(self):
        with TemporaryDirectory() as tmp:
            config = _config(repo_root=Path(tmp))
            calls = []

            def runner(command, *, cwd, timeout, shell=False):
                calls.append(command)
                if command == ["git", "rev-parse", "--is-inside-work-tree"]:
                    return auto_deploy.CommandResult(0, "true\n", "")
                if command == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
                    return auto_deploy.CommandResult(0, "master\n", "")
                if command == ["git", "remote", "get-url", "origin"]:
                    return auto_deploy.CommandResult(0, "git@github.com:example/davosbot.git\n", "")
                if command == ["git", "status", "--porcelain"]:
                    return auto_deploy.CommandResult(0, " M davosbot/main.py\n", "")
                raise AssertionError(f"unexpected command: {command}")

            alerts = []
            outcome = auto_deploy.deploy_once(
                config,
                runner=runner,
                alert=lambda event, message, metadata=None: alerts.append((event, message)) or True,
            )

            self.assertEqual(outcome.state, "blocked")
            self.assertIn(["git", "status", "--porcelain"], calls)
            self.assertNotIn(["git", "fetch", "--prune", "origin", "master"], calls)
            self.assertEqual(alerts[0][0], "auto_deploy_blocked")

    def test_ci_waiting_does_not_alert_by_default(self):
        config = _config()
        local_sha = "a" * 40
        remote_sha = "b" * 40

        def runner(command, *, cwd, timeout, shell=False):
            if command == ["git", "rev-parse", "--is-inside-work-tree"]:
                return auto_deploy.CommandResult(0, "true\n", "")
            if command == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
                return auto_deploy.CommandResult(0, "master\n", "")
            if command == ["git", "remote", "get-url", "origin"]:
                return auto_deploy.CommandResult(0, "git@github.com:example/davosbot.git\n", "")
            if command == ["git", "status", "--porcelain"]:
                return auto_deploy.CommandResult(0, "", "")
            if command == ["git", "fetch", "--prune", "origin", "master"]:
                return auto_deploy.CommandResult(0, "", "")
            if command == ["git", "rev-parse", "HEAD"]:
                return auto_deploy.CommandResult(0, local_sha + "\n", "")
            if command == ["git", "rev-parse", "origin/master"]:
                return auto_deploy.CommandResult(0, remote_sha + "\n", "")
            if command == ["git", "merge-base", "--is-ancestor", "HEAD", "origin/master"]:
                return auto_deploy.CommandResult(0, "", "")
            raise AssertionError(f"unexpected command: {command}")

        alerts = []
        with patch.dict(auto_deploy.os.environ, {"AUTO_DEPLOY_WAIT_ALERT_SECONDS": "0"}, clear=False):
            auto_deploy._WAITING_SINCE.clear()
            outcome = auto_deploy.deploy_once(
                config,
                runner=runner,
                ci_checker=lambda *_args: auto_deploy.CiStatus(False, "CI still running for bbbbbbb"),
                alert=lambda event, message, metadata=None: alerts.append((event, message, metadata)) or True,
            )

        self.assertEqual("waiting", outcome.state)
        self.assertEqual([], alerts)

    def test_ci_waiting_alert_can_be_enabled_after_threshold(self):
        remote_sha = "b" * 40

        with patch.dict(auto_deploy.os.environ, {"AUTO_DEPLOY_WAIT_ALERT_SECONDS": "60"}, clear=False), \
             patch.object(auto_deploy.time, "monotonic", side_effect=[100.0, 159.0, 161.0]):
            auto_deploy._WAITING_SINCE.clear()
            self.assertFalse(auto_deploy._should_alert_waiting(remote_sha, "CI still running"))
            self.assertFalse(auto_deploy._should_alert_waiting(remote_sha, "CI still running"))
            self.assertTrue(auto_deploy._should_alert_waiting(remote_sha, "CI still running"))


class DeployRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = _config(
            repo_root=Path(self.tmp.name), post_merge_command="post-check", restart_command="restart",
        )
        self.head = "a" * 40
        self.remote = "b" * 40
        self.calls = []
        self.dirty = ""
        self.branch = "master"
        self.remote_url = "git@github.com:example/davosbot.git"
        self.ancestor_ok = True
        self.post_codes = []
        self.restart_codes = []
        self.interrupt_after_merge = False
        self.change_head_after_merge = False
        patcher = patch.object(auto_deploy, "_run_preflight", return_value=auto_deploy.CommandResult(0))
        self.preflight = patcher.start()
        self.addCleanup(patcher.stop)
        self.ci = Mock(return_value=auto_deploy.CiStatus(True, "CI green"))
        self.alerts = Mock(return_value=True)

    def runner(self, command, *, cwd, timeout, shell=False):
        self.calls.append(command)
        if command == "post-check":
            return auto_deploy.CommandResult(self.post_codes.pop(0) if self.post_codes else 0, stderr="post failed")
        if command == "restart":
            return auto_deploy.CommandResult(self.restart_codes.pop(0) if self.restart_codes else 0, stderr="restart failed")
        if command == ["git", "rev-parse", "--is-inside-work-tree"]:
            return auto_deploy.CommandResult(0, "true")
        if command == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return auto_deploy.CommandResult(0, self.branch)
        if command == ["git", "remote", "get-url", "origin"]:
            return auto_deploy.CommandResult(0, self.remote_url)
        if command == ["git", "status", "--porcelain"]:
            return auto_deploy.CommandResult(0, self.dirty)
        if command == ["git", "fetch", "--prune", "origin", "master"]:
            return auto_deploy.CommandResult(0)
        if command == ["git", "rev-parse", "HEAD"]:
            return auto_deploy.CommandResult(0, self.head)
        if command == ["git", "rev-parse", "origin/master"]:
            return auto_deploy.CommandResult(0, self.remote)
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            return auto_deploy.CommandResult(0 if self.ancestor_ok else 1)
        if command[:3] == ["git", "merge", "--ff-only"]:
            self.head = "d" * 40 if self.change_head_after_merge else command[3]
            if self.interrupt_after_merge:
                self.interrupt_after_merge = False
                raise RuntimeError("watcher interrupted after FF")
            return auto_deploy.CommandResult(0)
        raise AssertionError(f"unexpected command: {command}")

    def deploy(self, config=None):
        return auto_deploy.deploy_once(
            config or self.config, runner=self.runner, ci_checker=self.ci, alert=self.alerts,
        )

    def pending(self):
        return json.loads(auto_deploy._pending_path(self.config).read_text(encoding="utf-8"))

    def merges(self):
        return [command for command in self.calls if isinstance(command, list) and command[:3] == ["git", "merge", "--ff-only"]]

    def test_restart_failure_is_retried_after_reload_at_same_sha(self):
        self.restart_codes = [1, 0]
        first = self.deploy()
        self.assertEqual(first.state, "failed")
        self.assertEqual(self.pending()["phase"], "restart")
        auto_deploy._write_status(self.config, first)
        # A fresh config plus the persisted checkpoint simulates the next
        # watcher process. No in-memory flag is used to authorize recovery.
        second = self.deploy(replace(self.config))
        self.assertEqual(second.state, "deployed")
        self.assertEqual((second.local_sha, second.remote_sha), ("a" * 40, "b" * 40))
        self.assertEqual(self.calls.count("restart"), 2)
        self.assertEqual(len(self.merges()), 1)
        self.preflight.assert_called_once()
        self.assertEqual([call.args[1] for call in self.ci.call_args_list], ["b" * 40, "b" * 40])
        self.assertFalse(auto_deploy._pending_path(self.config).exists())
        self.assertEqual(self.deploy().state, "up_to_date")

    def test_repeated_postcheck_failure_remains_failed_without_restart(self):
        self.post_codes = [1, 1]
        for _ in range(2):
            outcome = self.deploy()
            auto_deploy._write_status(self.config, outcome)
            self.assertEqual(outcome.state, "failed")
            self.assertIn("post-merge validation failed", outcome.detail)
            self.assertEqual(self.pending()["phase"], "post_merge")
        self.assertEqual(self.calls.count("restart"), 0)
        self.assertEqual(len(self.merges()), 1)
        self.preflight.assert_called_once()
        self.assertEqual(json.loads(self.config.status_path.read_text())["state"], "failed")

    def test_interrupt_after_merge_does_not_repeat_preflight_or_ff(self):
        self.interrupt_after_merge = True
        self.assertEqual(self.deploy().state, "failed")
        self.assertEqual(self.pending()["phase"], "prepared")
        self.assertEqual(self.deploy().state, "deployed")
        self.assertEqual(len(self.merges()), 1)
        self.preflight.assert_called_once()

    def test_newer_remote_does_not_replace_pending_verified_target(self):
        self.restart_codes = [1]
        self.deploy()
        self.remote = "c" * 40
        outcome = self.deploy()
        self.assertEqual(outcome.state, "deployed")
        self.assertEqual(outcome.remote_sha, "b" * 40)
        self.assertEqual(self.head, "b" * 40)
        self.assertEqual(self.ci.call_args.args[1], "b" * 40)
        self.assertEqual(len(self.merges()), 1)

    def test_retry_requires_clean_checkout_and_current_ci(self):
        self.restart_codes = [1]
        self.deploy()
        self.dirty = " M main.py"
        self.assertEqual(self.deploy().state, "blocked")
        self.dirty = ""
        self.ci.return_value = auto_deploy.CiStatus(False, "CI not green")
        self.assertEqual(self.deploy().state, "waiting")
        self.assertEqual(self.calls.count("restart"), 1)
        self.assertEqual(self.pending()["phase"], "restart")

    def test_unrelated_live_head_blocks_recovery(self):
        self.restart_codes = [1]
        self.deploy()
        self.head = "d" * 40
        outcome = self.deploy()
        self.assertEqual(outcome.state, "blocked")
        self.assertIn("unverified SHA", outcome.detail)
        self.assertEqual(self.calls.count("restart"), 1)

    def test_remote_rewrite_branch_or_repo_mismatch_blocks_recovery(self):
        self.restart_codes = [1]
        self.deploy()
        for field, value in (("ancestor_ok", False), ("branch", "other"), ("remote_url", "git@github.com:other/repo.git")):
            original = getattr(self, field)
            with self.subTest(field=field):
                setattr(self, field, value)
                self.assertEqual(self.deploy().state, "blocked")
                setattr(self, field, original)
        self.assertEqual(self.calls.count("restart"), 1)

    def test_changed_preflight_policy_requires_review(self):
        self.restart_codes = [1]
        self.deploy()
        outcome = self.deploy(replace(self.config, preflight_command="new stricter validation"))
        self.assertEqual(outcome.state, "failed")
        self.assertIn("operator review required", outcome.detail)
        self.assertEqual(self.calls.count("restart"), 1)

    def test_dry_run_preserves_checkpoint_without_retrying(self):
        self.restart_codes = [1]
        self.deploy()
        checkpoint = auto_deploy._pending_path(self.config).read_bytes()
        self.assertEqual(self.deploy(replace(self.config, dry_run=True)).state, "dry_run")
        self.assertEqual(auto_deploy._pending_path(self.config).read_bytes(), checkpoint)
        self.assertEqual(self.calls.count("restart"), 1)

    def test_malformed_checkpoint_cannot_be_misreported_up_to_date(self):
        self.head = self.remote
        path = auto_deploy._pending_path(self.config)
        path.parent.mkdir(parents=True)
        path.write_text("{broken")
        self.assertEqual(self.deploy().state, "failed")
        self.preflight.assert_not_called()
        self.assertNotIn("restart", self.calls)

    def test_checkpoint_failure_prevents_live_fast_forward(self):
        with patch.object(auto_deploy, "_save_pending", side_effect=OSError("disk full")):
            self.assertEqual(self.deploy().state, "failed")
        self.assertEqual(self.merges(), [])
        self.assertNotIn("restart", self.calls)

    def test_merge_pins_preflighted_sha_when_remote_ref_moves(self):
        def advance_remote(*_args):
            self.remote = "c" * 40
            return auto_deploy.CommandResult(0)
        self.preflight.side_effect = advance_remote
        self.assertEqual(self.deploy().state, "deployed")
        self.assertEqual(self.merges(), [["git", "merge", "--ff-only", "b" * 40]])
        self.assertEqual(self.head, "b" * 40)

    def test_changed_head_after_merge_is_not_restarted(self):
        self.change_head_after_merge = True
        self.assertEqual(self.deploy().state, "blocked")
        self.assertNotIn("restart", self.calls)
        self.assertEqual(self.pending()["target_sha"], "b" * 40)


if __name__ == "__main__":
    unittest.main()
