"""Real bridge journal and cleanup adapter, with synthetic native operations."""

import copy
from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from davosbot import work_actions as actions
from davosbot import work_actions_extra as extra
from davosbot import work_bridge as bridge
from test_work_bridge import FakeTransport, NOW, comment, request


OWNER = "+15550000001"
OTHER = "+15550000002"
ACK = {"acknowledge_backlog": True}
ACCEPTED = {
    "status": "accepted",
    "result": "Starting the fixed cleanup runner. Repairs are not verified.",
    "evidence": {"launch_state": "launch_requested", "scope": "safe_backlog",
                 "repairs_verified": False},
}


class WorkCleanupBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = NOW
        self.req = request(action="cleanup.start", args=copy.deepcopy(ACK))
        self.transport = FakeTransport([comment(self.req)])
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

        config = ModuleType("davosbot.config")
        config.OWNER_ID = OWNER
        config.normalize_handle = lambda handle: handle
        permissions = ModuleType("davosbot.permissions")
        permissions.is_owner = lambda handle: handle == OWNER
        permissions.redact_secret = lambda text: text
        self.permissions = permissions
        self.stack.enter_context(patch.dict(sys.modules, {
            "davosbot.config": config, "davosbot.permissions": permissions,
        }))
        self.launch = Mock(side_effect=self.launch_after_durable_start)
        self.status = Mock(return_value="Codex safe cleanup status: idle.")
        self.modules = {
            "config": config, "permissions": permissions,
            "commands": SimpleNamespace(_confirmed_safe_cleanup_result=self.launch,
                                        _cmd_cleanup_status=self.status),
        }
        self.lookup = self.stack.enter_context(patch.object(
            extra, "_module", side_effect=lambda name: self.modules[name]))
        if sys.platform == "win32":
            # Real journal serialization/reopening is portable. The production
            # flock and directory fsync still run unchanged on the Mini.
            self.stack.enter_context(patch.object(bridge, "_lock", self.fixture_lock))
            self.stack.enter_context(patch.object(bridge, "_sync_state_directory"))

    @staticmethod
    @contextmanager
    def fixture_lock(root):
        root.mkdir(parents=True, exist_ok=True)
        yield

    def worker(self, owner=OWNER):
        return bridge.WorkBridge(
            self.root, owner, transport=self.transport,
            validate_action=actions.validate_action, execute_action=actions.execute_action,
            clock=lambda: self.now, revision="a" * 40,
        )

    def state(self):
        return bridge._load(self.root / ".work_bridge" / "state.json")

    def record(self):
        return self.state()["records"][self.req["request_id"]]

    def launch_after_durable_start(self, owner):
        self.assertEqual(OWNER, owner)
        record = self.record()  # Reopen the real file at the native launch boundary.
        self.assertEqual("started", record["phase"])
        self.assertEqual(self.transport.rows[0]["id"], record["comment_id"])
        self.assertIsNone(record["response"])
        self.assertIsNone(record["published_comment_id"])
        self.assertTrue((self.root / ".work_bridge" / "initialized").is_file())
        return copy.deepcopy(ACCEPTED)

    def test_start_is_durable_before_launch_and_acceptance_before_publication(self):
        original_publish = self.transport.publish

        def publish_saved_result(result):
            record = self.record()
            self.assertEqual("finished", record["phase"])
            self.assertEqual(result, record["response"])
            self.assertEqual("accepted", result["result"]["status"])
            self.assertEqual(ACCEPTED["evidence"], result["result"]["evidence"])
            return original_publish(result)

        self.transport.publish = publish_saved_result
        self.assertEqual("active", self.worker().poll()["state"])
        self.launch.assert_called_once_with(OWNER)
        response = self.record()["response"]
        self.assertEqual("completed", response["state"])  # Adapter completed only.
        self.assertEqual("accepted", response["result"]["status"])
        self.assertFalse(response["result"]["evidence"]["repairs_verified"])

    def test_restart_and_new_comment_reusing_request_id_never_launch_again(self):
        self.worker().poll()
        self.worker().poll()  # A new worker instance simulates a process restart.
        self.transport.rows.append(comment(self.req, comment_id=5_355_000_001))
        changed = copy.deepcopy(self.req)
        changed["args"] = {"acknowledge_backlog": False}
        self.transport.rows.append(comment(changed, comment_id=5_355_000_002))
        self.worker().poll()
        self.launch.assert_called_once_with(OWNER)
        self.assertEqual(1, len(self.transport.publish_calls))

    def test_failed_start_journal_prevents_native_launch(self):
        with patch.object(bridge, "_save", side_effect=OSError("synthetic disk full")):
            self.assertEqual("error", self.worker().poll()["state"])
        self.launch.assert_not_called()
        self.assertEqual([], self.transport.publish_calls)

    def test_crash_after_launch_is_ambiguous_after_restart_without_rerun(self):
        original_save = bridge._save

        def crash_before_finished_receipt(path, state):
            record = state["records"].get(self.req["request_id"], {})
            if record.get("phase") == "finished":
                raise OSError("synthetic crash after launch")
            original_save(path, state)

        with patch.object(bridge, "_save", side_effect=crash_before_finished_receipt):
            self.assertEqual("error", self.worker().poll()["state"])
        self.assertEqual("started", self.record()["phase"])
        self.assertEqual([], self.transport.publish_calls)
        self.worker().poll()
        self.worker().poll()
        self.launch.assert_called_once_with(OWNER)
        response = self.record()["response"]
        self.assertEqual("ambiguous", response["state"])
        self.assertEqual({"error": "execution_interrupted"}, response["result"])

    def test_missing_publication_retries_saved_acceptance_only(self):
        self.transport.publish_error = "before"
        self.worker().poll()
        saved = self.record()["response"]
        self.worker().poll()
        self.assertEqual(1, len(self.transport.publish_calls))
        self.now += bridge.RESULT_RETRY_SECONDS + 1
        self.transport.publish_error = None
        self.worker().poll()
        self.launch.assert_called_once_with(OWNER)
        self.assertEqual([saved, saved], self.transport.publish_calls)
        self.assertIsNotNone(self.record()["published_comment_id"])

    def test_lost_publication_reply_reconciles_without_relaunch_or_duplicate(self):
        self.transport.publish_error = "after"
        self.worker().poll()
        self.assertIsNone(self.record()["published_comment_id"])
        self.worker().poll()
        self.launch.assert_called_once_with(OWNER)
        self.assertEqual(1, len(self.transport.publish_calls))
        self.assertIsNotNone(self.record()["published_comment_id"])

    def test_unauthorized_transport_or_native_owner_cannot_launch(self):
        self.transport.rows[0]["user"]["id"] = 111
        self.worker().poll()
        self.assertEqual([], self.transport.auth_calls)
        self.assertEqual([], self.transport.publish_calls)
        self.launch.assert_not_called()

        self.transport.rows[0]["user"]["id"] = bridge.GITHUB_OWNER_ID
        self.worker(owner=OTHER).poll()
        self.launch.assert_not_called()
        self.assertEqual("failed", self.record()["response"]["state"])
        self.assertEqual("owner_required", self.record()["response"]["result"]["evidence"]["code"])

    def test_native_permission_denial_cannot_launch_even_for_configured_owner(self):
        self.permissions.is_owner = lambda handle: False
        self.worker().poll()
        self.launch.assert_not_called()
        self.assertEqual("failed", self.record()["response"]["state"])

    def test_acknowledgement_is_exact_boolean_and_no_remote_execution_options(self):
        invalid = [{}, *({"acknowledge_backlog": ack} for ack in (False, 1, "true", None))]
        invalid += [{**ACK, key: "untrusted"} for key in (
            "command", "path", "sender", "owner", "password", "request_id",
        )]
        for index, args in enumerate(invalid):
            with self.subTest(args=args):
                req = request(action="cleanup.start", args=args)
                self.transport.rows = [comment(req, comment_id=5_355_000_010 + index)]
                self.worker().poll()
                record = self.state()["records"][req["request_id"]]
                self.assertEqual("rejected", record["response"]["state"])
        self.launch.assert_not_called()
        self.lookup.assert_not_called()  # Rejected before runtime adapter imports.

    def test_native_typed_refusal_is_failed_and_saved_without_retries(self):
        denied = {"status": "error", "result": "Native deployment gate denied cleanup.",
                  "evidence": {"launch_state": "denied", "scope": "safe_backlog",
                               "repairs_verified": False}}
        self.launch.side_effect = None
        self.launch.return_value = denied
        self.worker().poll()
        self.worker().poll()
        self.launch.assert_called_once_with(OWNER)
        self.assertEqual("failed", self.record()["response"]["state"])
        self.assertEqual(denied, self.record()["response"]["result"])

    def test_uncertain_native_exception_is_not_retried(self):
        def uncertain(owner):
            self.launch_after_durable_start(owner)
            raise RuntimeError("synthetic lost launch response")

        self.launch.side_effect = uncertain
        self.worker().poll()
        self.worker().poll()
        self.launch.assert_called_once_with(OWNER)
        response = self.record()["response"]
        self.assertEqual("ambiguous", response["state"])
        self.assertTrue(response["result"]["evidence"]["ambiguous"])

    def test_status_is_global_readback_not_start_or_request_specific_completion(self):
        self.req = request(action="cleanup.status", args={})
        self.transport.rows = [comment(self.req)]
        self.worker().poll()
        self.launch.assert_not_called()
        self.status.assert_called_once_with(OWNER)
        result = self.record()["response"]["result"]
        self.assertEqual("ok", result["status"])
        self.assertIn("Codex safe cleanup status: idle.", result["result"])
        self.assertEqual({"scope": "global_cleanup_queue", "repairs_verified": False,
                          "request_specific": False}, result["evidence"])
        for args in ({"request_id": self.req["request_id"]}, ACK):
            with self.subTest(args=args), self.assertRaises(ValueError):
                actions.validate_action("cleanup.status", args)
        self.launch.assert_not_called()

    def test_exact_cleanup_capabilities_are_discoverable_without_runtime_helpers(self):
        schemas = {}
        for action in ("cleanup.start", "cleanup.status"):
            reply = actions.execute_action("capabilities", {"action": action}, owner=OWNER)
            self.assertEqual("ok", reply["status"])
            self.assertEqual({action}, set(reply["evidence"]["actions"]))
            schemas[action] = reply["evidence"]["actions"][action]
            json.dumps(reply, allow_nan=False)
        start = schemas["cleanup.start"]
        self.assertTrue(start["mutates"])
        self.assertEqual({"acknowledge_backlog"}, set(start["fields"]))
        self.assertEqual({"type": "boolean", "required": True, "enum": [True]},
                         start["fields"]["acknowledge_backlog"])
        self.assertEqual("safe_backlog", start["scope"])
        self.assertEqual("cleanup.status", start["status_action"])
        self.assertEqual("requests.receipt", start["receipt_action"])
        self.assertFalse(start["automatic_retry"])
        self.assertFalse(schemas["cleanup.status"]["mutates"])
        self.assertEqual({}, schemas["cleanup.status"]["fields"])
        self.assertEqual("global_cleanup_queue", schemas["cleanup.status"]["scope"])
        self.lookup.assert_not_called()

    def test_paginated_catalogue_reaches_cleanup_actions_with_bounded_complete_pages(self):
        expected = sorted(actions.action_catalogue())
        found, offset = [], 0
        for _ in range(len(expected) + 1):
            reply = actions.execute_action("capabilities", {"offset": offset, "limit": 3}, owner=OWNER)
            self.assertEqual("ok", reply["status"])
            evidence = reply["evidence"]
            page = list(evidence["actions"])
            self.assertTrue(0 < len(page) <= 3)
            self.assertEqual(len(expected), evidence["total"])
            self.assertEqual(expected[offset:offset + 3], page)
            self.assertLess(len(json.dumps(reply).encode("utf-8")), bridge.MAX_RESULT_BYTES)
            found.extend(page)
            following = evidence["next_offset"]
            if following is None:
                break
            self.assertEqual(offset + len(page), following)
            offset = following
        else:
            self.fail("Catalogue pagination did not terminate")
        self.assertEqual(expected, found)
        self.assertIn("cleanup.start", found)
        self.assertIn("cleanup.status", found)
        self.lookup.assert_not_called()

    def test_changes_intake_logs_only_and_never_requests_cleanup(self):
        logger = Mock(return_value="Logged guarded Codex handoff #7 for review.")
        self.modules["change_request_tools"] = SimpleNamespace(_log_change_request=logger)
        args = {"request": "Fix the synthetic fixture typo", "reason": "Owner requested review"}
        reply = actions.execute_action("changes.intake", args, owner=OWNER)
        self.assertEqual("ok", reply["status"])
        logger.assert_called_once_with(args["request"], args["reason"])
        self.launch.assert_not_called()
        self.status.assert_not_called()
        self.assertNotIn("commands", [call.args[0] for call in self.lookup.call_args_list])


if __name__ == "__main__":
    unittest.main()
