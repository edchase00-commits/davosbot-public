"""Owner-only receipt recovery with synthetic journals and mocked publication."""

from contextlib import ExitStack, contextmanager
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from davosbot import work_actions as actions, work_bridge as bridge
from test_work_bridge import FakeTransport, NOW, OWNER, comment, request


class WorkReceiptPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.journal = self.root / ".work_bridge" / "state.json"
        self.journal.parent.mkdir()
        self.request_id = str(uuid.uuid4())
        self.comment_id = 5_354_900_000
        self.args = {"request_id": self.request_id, "request_comment_id": self.comment_id}
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("davosbot.config.PROJECT_ROOT", self.root))
        stack.enter_context(patch("davosbot.config.OWNER_ID", OWNER))
        stack.enter_context(patch("davosbot.permissions.OWNER_ID", OWNER))

    def record(self, *, outcome="completed", status="ok", published=5_354_999_000,
               confirmation=None, created=None, phase="finished"):
        created = time.time() - 2 * bridge.REQUEST_TTL if created is None else created
        response = {
            "schema_version": 1, "kind": "davos_result", "request_id": self.request_id,
            "request_comment_id": self.comment_id, "state": outcome,
            "result": {"status": status, "result": "Synthetic original result", "evidence": confirmation or {}},
            "runtime_revision": "a" * 40, "completed_at": bridge._iso(created + 1),
        }
        record = {
            "comment_id": self.comment_id, "body_sha256": "b" * 64, "created_at": created,
            "started_at": created, "phase": phase, "response": response if phase == "finished" else None,
            "published_comment_id": published if phase == "finished" else None, "publication_attempt_at": 0,
        }
        state = {"schema_version": 1, "scanned_at": created, "records": {self.request_id: record}}
        self.journal.write_bytes(bridge._json_bytes(state))
        return state

    def lookup(self, args=None, owner=OWNER):
        return actions.execute_action("requests.receipt", self.args if args is None else args, owner=owner)

    def test_owner_lookup_preserves_recorded_outcome_and_publication_separately(self):
        for outcome, status, published in (
            ("completed", "ok", 5_354_999_000),
            ("completed", "accepted", None),
            ("completed", "native_confirmation_required", None),
            ("failed", "error", 5_354_999_000),
            ("ambiguous", "error", None),
            ("rejected", "error", None),
        ):
            with self.subTest(outcome=outcome, status=status):
                self.record(outcome=outcome, status=status, published=published)
                before = self.journal.read_bytes()
                evidence = self.lookup()["evidence"]
                self.assertEqual("recorded", evidence["receipt_state"])
                self.assertEqual(outcome, evidence["request_state"])
                self.assertEqual(status, evidence["action_status"])
                self.assertEqual("published" if published else "pending", evidence["publication_state"])
                self.assertEqual(published, evidence["published_comment_id"])
                self.assertEqual("a" * 40, evidence["runtime_revision"])
                self.assertIsNotNone(datetime.fromisoformat(evidence["observed_at"]).tzinfo)
                self.assertEqual(before, self.journal.read_bytes())

    def test_nonowner_and_invalid_identifiers_never_read_the_journal(self):
        with patch.object(bridge, "_load") as load:
            for actor in ("+15550000002", "", False):
                self.assertEqual("owner_required", self.lookup(owner=actor)["evidence"]["code"])
            invalid = [
                {**self.args, "request_id": str(uuid.uuid1())},
                {**self.args, "request_id": self.request_id.upper()},
                {**self.args, "request_id": "../../state.json"},
                {**self.args, "request_comment_id": True},
                {**self.args, "request_comment_id": "5354900000"},
                {**self.args, "request_comment_id": 0},
                {**self.args, "request_comment_id": 2**53 + 1},
                {**self.args, "path": "/another/journal.json"},
                {**self.args, "owner": OWNER},
            ]
            for args in invalid:
                with self.subTest(args=args):
                    self.assertEqual("error", self.lookup(args)["status"])
            load.assert_not_called()

    def test_retained_historical_receipt_does_not_expire_with_original_request_ttl(self):
        self.record(created=time.time() - 3 * 86400)
        evidence = self.lookup()["evidence"]
        self.assertEqual("recorded", evidence["receipt_state"])
        self.assertLess(datetime.fromisoformat(evidence["completed_at"]).timestamp(), time.time() - bridge.REQUEST_TTL)

    def test_symlinked_journal_directory_is_unknown_without_loading(self):
        self.record()
        with patch.object(Path, "is_symlink", autospec=True,
                          side_effect=lambda path: path == self.journal.parent), patch.object(bridge, "_load") as load:
            response = self.lookup()
        load.assert_not_called()
        self.assertEqual("unknown", response["evidence"]["receipt_state"])
        self.assertIn("do not repeat", response["result"].lower())
        self.assertIn("observed_at", response["evidence"])

    def test_missing_pruned_mismatched_corrupt_and_started_receipts_remain_unknown(self):
        responses = [self.lookup()]
        self.record()
        responses.append(self.lookup({**self.args, "request_comment_id": self.comment_id + 1}))
        responses.append(self.lookup({**self.args, "request_id": str(uuid.uuid4())}))
        self.record(phase="started")
        responses.append(self.lookup())
        self.journal.write_text("broken", encoding="utf-8")
        responses.append(self.lookup())
        self.journal.write_bytes(bridge._json_bytes({"schema_version": 1, "scanned_at": 0, "records": {}}))
        responses.append(self.lookup())
        for response in responses:
            self.assertEqual("unknown", response["evidence"]["receipt_state"])
            self.assertNotIn("request_state", response["evidence"])
            self.assertIn("do not repeat", response["result"].lower())
            self.assertIn("observed_at", response["evidence"])

    def test_only_confirmation_metadata_is_returned_without_text_args_paths_or_secrets(self):
        state = self.record(confirmation={
            "message_state": "sent", "delivery_confirmed": False, "accepted_by_sender": True,
            "ambiguous": False, "review_only": True, "private_path": "/synthetic/private/report.txt",
            "token": "synthetic-secret-value", "args": {"message": "private original text"},
        })
        state["records"][self.request_id]["response"]["result"]["result"] = "private original text" * 1000
        self.journal.write_bytes(bridge._json_bytes(state))
        response = self.lookup()
        saved = response["evidence"]["saved_confirmation"]
        self.assertEqual("sent", saved["message_state"])
        self.assertIs(saved["delivery_confirmed"], False)
        serialized = json.dumps(response)
        self.assertLess(len(serialized), 1500)
        for private in ("private original text", "synthetic-secret-value", "/synthetic/private", "body_sha256", "started_at"):
            self.assertNotIn(private, serialized)

    def test_capability_schema_advertises_read_only_exact_lookup(self):
        spec = actions.action_catalogue()["requests.receipt"]
        self.assertFalse(spec["mutates"])
        self.assertEqual({"request_id", "request_comment_id"}, set(spec["required"]))
        self.assertEqual(set(spec["required"]), set(spec["fields"]))

    @staticmethod
    @contextmanager
    def fixture_lock(root):
        root.mkdir(parents=True, exist_ok=True)
        yield

    def test_lost_publication_receipt_lookup_and_retry_never_repeat_original_action(self):
        original = request(request_id=self.request_id, action="images.generate", args={"prompt": "Synthetic scene"})
        transport = FakeTransport([comment(original, comment_id=self.comment_id)])
        transport.publish_error = "before"
        invoked = []

        def execute(action, args, *, owner):
            invoked.append(action)
            if action == "images.generate":
                return {"status": "accepted", "result": "Synthetic queued image", "evidence": {"delivery_confirmed": False}}
            return actions.execute_action(action, args, owner=owner)

        with ExitStack() as stack:
            if sys.platform == "win32":
                stack.enter_context(patch.object(bridge, "_lock", side_effect=self.fixture_lock))
                stack.enter_context(patch.object(bridge, "_sync_state_directory"))
            worker = bridge.WorkBridge(self.root, OWNER, transport=transport, validate_action=actions.validate_action,
                                       execute_action=execute, clock=lambda: NOW, revision="a" * 40)
            worker.poll()
            query = request(action="requests.receipt", args=self.args)
            transport.rows.append(comment(query, comment_id=self.comment_id + 1))
            worker.poll()
            local = bridge._load(self.journal)
            evidence = local["records"][query["request_id"]]["response"]["result"]["evidence"]
            self.assertEqual("completed", evidence["request_state"])
            self.assertEqual("accepted", evidence["action_status"])
            self.assertEqual("pending", evidence["publication_state"])
            self.assertIs(evidence["saved_confirmation"]["delivery_confirmed"], False)
            transport.publish_error = None
            worker.clock = lambda: NOW + bridge.RESULT_RETRY_SECONDS + 1
            worker.poll()
            self.assertEqual(["images.generate", "requests.receipt"], invoked)
            self.assertEqual(2, len([row for row in transport.rows if '"kind":"davos_result"' in row["body"]]))


if __name__ == "__main__":
    unittest.main()
