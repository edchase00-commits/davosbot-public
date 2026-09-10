"""Owner-only progress snapshots using the real native context lookup."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from davosbot import work_actions as actions, work_actions_extra as extra
from test_openai_image_routing import _load_main_function


OWNER = "+15550000001"
OTHER = "+15550000002"
NOW = 1_788_566_400.0


class WorkImageStatusPermissionTests(unittest.TestCase):
    def setUp(self):
        self.active = {}
        helper = _load_main_function("_active_image_jobs_for_context", {
            "_ACTIVE_IMAGE_JOBS": self.active, "_IMAGE_JOB_LOCK": threading.RLock(),
        }, dependencies=("_image_context_key",))
        self.context_key = helper.__globals__["_image_context_key"]
        self.helper = Mock(wraps=helper)
        self.main = SimpleNamespace(_active_image_jobs_for_context=self.helper)
        self.modules = {
            "config": SimpleNamespace(OWNER_ID=OWNER),
            "permissions": SimpleNamespace(is_owner=lambda actor: actor == OWNER),
            "main": self.main,
        }
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.lookup = stack.enter_context(patch.object(extra, "_module", side_effect=lambda name: self.modules[name]))
        stack.enter_context(patch.object(extra.time, "time", return_value=NOW))
        stack.enter_context(patch("davosbot.config.OWNER_ID", OWNER))
        stack.enter_context(patch("davosbot.permissions.OWNER_ID", OWNER))

    def job(self, sender=OWNER, recipient=OWNER, group=False, route=""):
        record = {
            "sender": sender, "recipient": recipient, "is_group": group, "route_key": route,
            "job_id": "1788566358000-1234", "provider": "gemini", "started_ts": NOW - 42,
            "prompt": "synthetic private prompt", "image_path": "/synthetic/private/reference.png",
        }
        self.active[self.context_key(sender, recipient, group, route)] = record
        return record

    def status(self, args=None, owner=OWNER):
        return actions.execute_action("images.status", {} if args is None else args, owner=owner)

    def test_real_native_lookup_excludes_other_senders_groups_and_routes(self):
        self.job(sender=OTHER, recipient=OTHER)["job_id"] = "1788566358000-2222"
        self.job(recipient="a" * 32, group=True)["job_id"] = "1788566358000-3333"
        self.job(route="nano_banana")["job_id"] = "1788566358000-4444"
        owner_job = self.job()
        before = deepcopy(self.active)
        evidence = self.status()["evidence"]
        self.helper.assert_called_once_with(OWNER, OWNER, is_group=False)
        self.assertEqual(1, evidence["active_job_count"])
        self.assertEqual([{
            "job_id": owner_job["job_id"], "provider": "gemini", "elapsed_seconds": 42,
            "timeout_remaining_seconds": None,
        }], evidence["jobs"])
        self.assertEqual("unknown", evidence["delivery_state"])
        self.assertEqual(NOW, datetime.fromisoformat(evidence["observed_at"]).timestamp())
        self.assertEqual(before, self.active)

    def test_returned_job_must_still_match_owner_dm_metadata(self):
        for field, value in (("sender", OTHER), ("recipient", OTHER), ("is_group", True),
                             ("is_group", 0), ("route_key", "nano_banana")):
            with self.subTest(field=field):
                record = self.job()
                record[field] = value  # Native DM index alone does not encode recipient.
                evidence = self.status()["evidence"]
                self.assertEqual(0, evidence["active_job_count"])
                self.assertEqual([], evidence["jobs"])
                self.assertEqual("unknown", evidence["delivery_state"])

    def test_nonowner_and_caller_context_are_rejected_before_native_lookup(self):
        for owner in (OTHER, "", False):
            self.assertEqual("owner_required", self.status(owner=owner)["evidence"]["code"])
        for field in ("sender", "owner", "recipient", "chat_id", "route_key", "path", "job_id"):
            with self.subTest(field=field):
                self.assertEqual("error", self.status({field: "synthetic"})["status"])
        self.helper.assert_not_called()
        self.lookup.assert_not_called()

    def test_native_permission_denial_prevents_queue_access(self):
        self.modules["permissions"].is_owner = lambda actor: False
        response = self.status()
        self.assertEqual("error", response["status"])
        self.helper.assert_not_called()

    def test_empty_or_removed_job_never_proves_delivery(self):
        for phase in ("never_observed", "active", "finished_or_restarted"):
            if phase == "active":
                self.job()
            else:
                self.active.clear()
            response = self.status()
            self.assertEqual("unknown", response["evidence"]["delivery_state"])
            self.assertNotIn("delivery_confirmed", response["evidence"])
            self.assertIn("does not establish", response["result"])
            if phase != "active":
                self.assertEqual([], response["evidence"]["jobs"])
                self.assertIn("No active image job was observed", response["result"])

    def test_missing_invalid_or_future_times_are_unknown_not_zero(self):
        for started in (None, "42", True, float("nan"), float("inf"), -1, 0, NOW + 1, 10**400):
            with self.subTest(started_type=type(started).__name__):
                self.job()["started_ts"] = started
                job = self.status()["evidence"]["jobs"][0]
                self.assertIsNone(job["elapsed_seconds"])
                self.assertIsNone(job["timeout_remaining_seconds"])
        record = self.job()
        record.pop("started_ts")
        record["deadline"] = NOW + 300  # There is no native job deadline contract.
        record["estimate_seconds"] = 120
        job = self.status()["evidence"]["jobs"][0]
        self.assertIsNone(job["elapsed_seconds"])
        self.assertIsNone(job["timeout_remaining_seconds"])

    def test_metadata_is_bounded_and_no_files_provider_or_send_helpers_are_used(self):
        record = self.job()
        record["job_id"] = "synthetic private ID" * 100
        record["provider"] = ["synthetic private provider"]
        record["prompt"] *= 1000
        with patch.object(Path, "open", side_effect=AssertionError("unexpected file access")) as opened:
            response = self.status()
        opened.assert_not_called()
        self.assertEqual(["config", "permissions", "main"], [call.args[0] for call in self.lookup.call_args_list])
        evidence = response["evidence"]
        self.assertIsNone(evidence["jobs"][0]["job_id"])
        self.assertEqual("unknown", evidence["jobs"][0]["provider"])
        serialized = json.dumps(response, allow_nan=False)
        self.assertLess(len(serialized), 1200)
        for private in ("synthetic private", "/synthetic/private", OWNER, "prompt", "image_path", "recipient"):
            self.assertNotIn(private, serialized)

    def test_capability_schema_requires_no_args_and_remains_read_only(self):
        spec = actions.action_catalogue()["images.status"]
        self.assertFalse(spec["mutates"])
        self.assertEqual({}, spec["fields"])
        self.lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
