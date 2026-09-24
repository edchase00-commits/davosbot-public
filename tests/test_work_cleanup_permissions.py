"""The Work repair launcher retains the native owner and fixed-runner boundary."""

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from davosbot import commands, work_actions_extra as extra


OWNER = "+15550000001"


class CleanupLaunchResultTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.script = self.root / "scripts" / "nightly_safe_cleanup_codex.sh"
        self.script.parent.mkdir()
        self.script.write_text("#!/bin/bash\n", encoding="utf-8")
        self.stack.enter_context(patch.object(commands, "PROJECT_ROOT", self.root))
        self.rows = self.stack.enter_context(patch.object(commands, "_fetch_change_log_rows",
            return_value=[(1, "fix help wording", "", "2026-09-05")]))
        self.permission = self.stack.enter_context(patch.object(commands, "check_action_permission", return_value=None))
        self.lock = self.stack.enter_context(patch.object(commands, "cleanup_lock_state", return_value="idle"))
        self.mini = self.stack.enter_context(patch.object(commands, "_can_autorun_cleanup_here", return_value=True))
        self.spawn = self.stack.enter_context(patch.object(commands.subprocess, "Popen"))

    def test_acceptance_uses_fixed_native_runner_and_does_not_claim_completion(self):
        result = commands._confirmed_safe_cleanup_result(OWNER)
        self.permission.assert_called_once_with(OWNER, "deploy")
        self.spawn.assert_called_once_with(
            ["/bin/bash", str(self.script), "--confirmed", "--notify"],
            cwd=str(self.root), stdout=commands.subprocess.DEVNULL,
            stderr=commands.subprocess.DEVNULL, start_new_session=True)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["evidence"], {
            "launch_state": "launch_requested", "scope": "safe_backlog", "repairs_verified": False})
        self.assertIn("Starting Codex safe cleanup", result["result"])

    def test_native_deploy_denial_precedes_backlog_and_process_access(self):
        self.permission.return_value = "Permission denied."
        result = commands._confirmed_safe_cleanup_result(OWNER)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["evidence"]["launch_state"], "denied")
        self.rows.assert_not_called()
        self.lock.assert_not_called()
        self.spawn.assert_not_called()

    def test_empty_board_never_launches_or_claims_a_fix(self):
        self.rows.return_value = []
        result = commands._confirmed_safe_cleanup_result(OWNER)
        self.assertEqual(result["evidence"]["launch_state"], "empty")
        self.assertIs(result["evidence"]["repairs_verified"], False)
        self.spawn.assert_not_called()

    def test_busy_or_unverifiable_lock_never_launches_another_worker(self):
        for state, expected, status in (("running", "already_running", "ok"),
                                        ("unknown", "lock_unknown", "error")):
            with self.subTest(state=state):
                self.lock.return_value = state
                result = commands._confirmed_safe_cleanup_result(OWNER)
                self.assertEqual(result["evidence"]["launch_state"], expected)
                self.assertEqual(result["status"], status)
        self.spawn.assert_not_called()

    def test_missing_runner_and_wrong_platform_are_errors_before_spawn(self):
        self.mini.return_value = False
        result = commands._confirmed_safe_cleanup_result(OWNER)
        self.assertEqual(result["evidence"]["launch_state"], "mini_required")
        self.assertEqual(result["status"], "error")
        self.script.unlink()
        result = commands._confirmed_safe_cleanup_result(OWNER)
        self.assertEqual(result["evidence"]["launch_state"], "runner_missing")
        self.assertEqual(result["status"], "error")
        self.spawn.assert_not_called()

    def test_spawn_failure_is_not_retried_and_does_not_expose_exception_text(self):
        self.spawn.side_effect = OSError("synthetic-private-detail")
        with patch.object(commands.logger, "warning") as warning:
            result = commands._confirmed_safe_cleanup_result(OWNER)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["evidence"]["launch_state"], "launch_failed")
        self.assertNotIn("synthetic-private-detail", str(result))
        self.assertNotIn("synthetic-private-detail", str(warning.call_args))
        self.spawn.assert_called_once()

    def test_native_imessage_wrapper_still_returns_native_text(self):
        with patch.object(commands, "_confirmed_safe_cleanup_result",
                          return_value={"status": "accepted", "result": "Native reply."}) as helper:
            self.assertEqual(commands._cmd_confirmed_safe_cleanup(OWNER), "Native reply.")
        helper.assert_called_once_with(OWNER)


class CleanupAdapterPermissionTests(unittest.TestCase):
    def setUp(self):
        self.launch = Mock(return_value={"status": "accepted", "result": "Launch requested.",
            "evidence": {"launch_state": "launch_requested", "repairs_verified": False}})
        self.native = SimpleNamespace(_confirmed_safe_cleanup_result=self.launch,
                                     _cmd_cleanup_status=Mock(return_value="Codex safe cleanup status: idle.\nLast run outcome: finished."))
        self.modules = {"config": SimpleNamespace(OWNER_ID=OWNER),
                        "permissions": SimpleNamespace(is_owner=lambda actor: actor == OWNER),
                        "commands": self.native}
        self.lookup = patch.object(extra, "_module", side_effect=lambda name: self.modules[name])
        self.lookup.start()
        self.addCleanup(self.lookup.stop)

    def test_explicit_true_ack_and_no_caller_controlled_inputs(self):
        for args in ({}, {"acknowledge_backlog": False}, {"acknowledge_backlog": 1},
                     {"acknowledge_backlog": "true"}, {"acknowledge_backlog": None}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                extra.execute_extra_action("cleanup.start", args, OWNER)
        for key in ("owner", "sender", "command", "path", "model", "timeout", "change_id", "confirmed"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                extra.execute_extra_action("cleanup.start", {"acknowledge_backlog": True, key: "untrusted"}, OWNER)
        self.launch.assert_not_called()

    def test_nonowner_and_native_owner_denial_precede_launch(self):
        for owner in ("+15550000002", "", None):
            with self.subTest(owner=owner), self.assertRaisesRegex(ValueError, "owner_required"):
                extra.execute_extra_action("cleanup.start", {"acknowledge_backlog": True}, owner)
        self.modules["permissions"].is_owner = lambda actor: False
        with self.assertRaisesRegex(ValueError, "owner_required"):
            extra.execute_extra_action("cleanup.start", {"acknowledge_backlog": True}, OWNER)
        self.launch.assert_not_called()

    def test_adapter_preserves_all_native_outcomes_and_local_owner(self):
        for status in ("error", "accepted", "ok"):
            with self.subTest(status=status):
                expected = {"status": status, "result": "Native response.", "evidence": {"repairs_verified": False}}
                self.launch.return_value = expected
                actual = extra.execute_extra_action("cleanup.start", {"acknowledge_backlog": True}, OWNER)
                self.assertEqual(actual["status"], expected["status"])
                self.assertEqual(actual["evidence"], expected["evidence"])
                if status == "accepted":
                    self.assertIn("launch requested", actual["result"])
                    self.assertIn("no fix is verified yet", actual["result"])
                else:
                    self.assertEqual(actual["result"], expected["result"])
                self.launch.assert_called_with(OWNER)

    def test_global_idle_finished_status_never_becomes_request_completion(self):
        result = extra.execute_extra_action("cleanup.status", {}, OWNER)
        self.assertEqual(result["status"], "ok")
        self.assertIn("not proof that a particular repair completed", result["result"])
        self.assertEqual(result["evidence"], {"scope": "global_cleanup_queue",
                                             "repairs_verified": False, "request_specific": False})
        self.native._cmd_cleanup_status.assert_called_once_with(OWNER)
        self.launch.assert_not_called()

    def test_status_rejects_request_ids_and_never_implies_run_correlation(self):
        with self.assertRaises(ValueError):
            extra.execute_extra_action("cleanup.status", {"request_id": "untrusted"}, OWNER)
        self.native._cmd_cleanup_status.assert_not_called()

    def test_historical_process_failure_is_not_status_lookup_failure(self):
        self.native._cmd_cleanup_status.return_value = "Codex safe cleanup status: idle.\nLast run outcome: failed."
        result = extra.execute_extra_action("cleanup.status", {}, OWNER)
        self.assertEqual(result["status"], "ok")
        self.assertIs(result["evidence"]["repairs_verified"], False)

    def test_status_native_permission_denial_is_still_an_error(self):
        self.native._cmd_cleanup_status.return_value = "Permission denied."
        result = extra.execute_extra_action("cleanup.status", {}, OWNER)
        self.assertEqual(result["status"], "error")
        self.launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
