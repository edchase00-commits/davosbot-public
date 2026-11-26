"""Synthetic transport and adapter tests: no credentials or runtime side effects."""
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from davosbot import work_bridge as bridge

NOW = 1_788_624_000.0
OWNER = "+15551234567"


def request(**changes):
    result = {"schema_version": 1, "kind": "davos_request", "request_id": str(uuid.uuid4()),
              "action": "diagnostics.status", "args": {}}
    result.update(changes)
    return result


def comment(body=None, *, comment_id=5_354_900_000, created=NOW - 5):
    return {"id": comment_id, "node_id": "IC_kwDOExampleNode", "body": json.dumps(request() if body is None else body),
            "user": {"id": bridge.GITHUB_OWNER_ID, "type": "User"},
            "issue_url": bridge.ISSUE_URL,
            "url": f"https://api.github.com/repos/{bridge.REPOSITORY}/issues/comments/{comment_id}",
            "created_at": bridge._iso(created), "updated_at": bridge._iso(created)}


def graph_node(row):
    return {"__typename": "IssueComment", "fullDatabaseId": str(row["id"]), "body": row["body"],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"], "lastEditedAt": None,
            "includesCreatedEdit": False, "createdViaEmail": False, "editor": None,
            "author": {"__typename": "User", "databaseId": bridge.GITHUB_OWNER_ID},
            "issue": {"number": bridge.ISSUE_NUMBER, "fullDatabaseId": str(bridge.ISSUE_ID),
                      "repository": {"databaseId": bridge.REPOSITORY_ID, "nameWithOwner": bridge.REPOSITORY,
                                     "isPrivate": True, "owner": {"__typename": "User", "databaseId": bridge.GITHUB_OWNER_ID}}}}


def graph_page(rows, has_next=False):
    repo = copy.deepcopy(graph_node(comment())["issue"]["repository"])
    repo["issue"] = {"number": bridge.ISSUE_NUMBER, "fullDatabaseId": str(bridge.ISSUE_ID),
                     "comments": {"edges": [{"cursor": str(row["id"]),
                         "node": {"id": row["node_id"], "fullDatabaseId": str(row["id"]), "body": row["body"],
                                  "createdAt": row["created_at"], "updatedAt": row["updated_at"],
                                  "author": {"__typename": row["user"]["type"], "databaseId": row["user"]["id"]}}}
                         for row in rows],
                         "pageInfo": {"endCursor": str(rows[-1]["id"]) if rows else None, "hasNextPage": has_next}}}
    return {"data": {"repository": repo}}


class FakeTransport:
    def __init__(self, rows):
        self.rows = rows
        self.open = True
        self.auth_error = None
        self.scan_error = None
        self.publish_error = None
        self.publish_calls = []
        self.auth_calls = []
        self.scan_since = []

    def assert_channel(self):
        return self.open

    def comment_pages(self, cursor):
        self.scan_since.append(cursor)
        if self.scan_error:
            raise self.scan_error
        rows = [copy.deepcopy(row) for row in sorted(self.rows, key=lambda r: r["id"])
                if cursor is None or row["id"] > int(cursor)]
        for row in rows:
            row["scan_cursor"] = str(row["id"])
        if not rows:
            yield [], True
        for offset in range(0, min(len(rows), bridge.MAX_PAGES * bridge.PAGE_SIZE), bridge.PAGE_SIZE):
            end = offset + bridge.PAGE_SIZE
            yield rows[offset:end], end >= len(rows)

    def authenticate(self, row, *, receipt=False):
        self.auth_calls.append(row["id"])
        if self.auth_error:
            raise self.auth_error

    def publish(self, result):
        self.publish_calls.append(copy.deepcopy(result))
        if self.publish_error == "before":
            raise bridge.BridgeError("github_unavailable")
        row = comment(result, comment_id=5_354_999_000 + len(self.publish_calls), created=NOW)
        row["body"] = bridge._json_bytes(result).decode()
        self.rows.append(row)
        if self.publish_error == "after":
            raise bridge.BridgeError("publication_unconfirmed")
        return row["id"]


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.row = comment()
        self.transport = bridge.GitHubTransport(".")

    def authenticate(self, node=None, row=None):
        with patch.object(self.transport, "_call", return_value={"data": {"node": node or graph_node(self.row)}}):
            self.transport.authenticate(row or self.row)

    def test_valid_bigint_ids_and_exact_authorized_metadata(self):
        self.authenticate()
        self.assertIn("fullDatabaseId", bridge.COMMENT_QUERY)
        self.assertGreater(bridge.ISSUE_ID, 2**31)

    def test_rest_forgery_and_wrong_issue_rejected_before_graphql(self):
        for key, value in (("issue_url", "https://api.github.com/repos/other/repo/issues/64"),
                           ("url", "https://attacker.example/comment"),
                           ("user", {"id": 123, "type": "User"}),
                           ("user", {"id": bridge.GITHUB_OWNER_ID, "type": "Bot"})):
            row = copy.deepcopy(self.row)
            row[key] = value
            with self.subTest(key=key), patch.object(self.transport, "_call") as call:
                with self.assertRaises(bridge.RequestRejected):
                    self.transport.authenticate(row)
                call.assert_not_called()

    def test_same_second_edit_and_missing_edit_metadata_fail_closed(self):
        for key, value in (("lastEditedAt", self.row["created_at"]), ("editor", {"__typename": "User"}),
                           ("includesCreatedEdit", True), ("createdViaEmail", True)):
            node = graph_node(self.row)
            node[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(bridge.RequestRejected, "edited_or_email"):
                self.authenticate(node)
        for key in ("lastEditedAt", "editor", "includesCreatedEdit", "createdViaEmail"):
            node = graph_node(self.row)
            del node[key]
            with self.subTest(missing=key), self.assertRaises(bridge.RequestRejected):
                self.authenticate(node)

    def test_graphql_body_author_repo_issue_and_id_mismatches(self):
        for mutation in (
            lambda node: node.update(body="changed"),
            lambda node: node.update(fullDatabaseId="1"),
            lambda node: node["author"].update(databaseId=123),
            lambda node: node["issue"].update(fullDatabaseId="64"),
            lambda node: node["issue"]["repository"].update(isPrivate=False),
            lambda node: node["issue"]["repository"].update(databaseId=1),
            lambda node: node["issue"]["repository"]["owner"].update(databaseId=1),
        ):
            node = graph_node(self.row)
            mutation(node)
            with self.assertRaises(bridge.RequestRejected):
                self.authenticate(node)

    def test_graphql_errors_are_transient_not_authorization(self):
        with patch.object(self.transport, "_call", return_value={"errors": [{"message": "temporary"}]}):
            with self.assertRaisesRegex(bridge.BridgeError, "comment_auth_unavailable"):
                self.transport.authenticate(self.row)

    def test_private_repository_and_pinned_issue_checks(self):
        repo = {"private": True, "id": bridge.REPOSITORY_ID, "full_name": bridge.REPOSITORY,
                "owner": {"id": bridge.GITHUB_OWNER_ID}}
        actor = {"id": bridge.GITHUB_OWNER_ID, "login": bridge.REPOSITORY.split("/", 1)[0], "type": "User"}
        issue = {"id": bridge.ISSUE_ID, "number": bridge.ISSUE_NUMBER, "url": bridge.ISSUE_URL, "state": "open"}
        with patch.object(self.transport, "_call", side_effect=[repo, actor, issue]):
            self.assertTrue(self.transport.assert_channel())
        issue["state"] = "closed"
        with patch.object(self.transport, "_call", side_effect=[repo, actor, issue]):
            self.assertFalse(self.transport.assert_channel())
        repo["private"] = False
        with patch.object(self.transport, "_call", return_value=repo) as call:
            with self.assertRaises(bridge.BridgeError):
                self.transport.assert_channel()
            self.assertEqual(1, call.call_count)

    def test_wrong_runtime_github_identity_cannot_post(self):
        repo = {"private": True, "id": bridge.REPOSITORY_ID, "full_name": bridge.REPOSITORY,
                "owner": {"id": bridge.GITHUB_OWNER_ID}}
        for actor in ({"id": 111, "login": "other-user", "type": "User"},
                      {"id": bridge.GITHUB_OWNER_ID, "login": "<windows-user>se00-commits", "type": "Bot"}):
            with self.subTest(actor=actor), patch.object(self.transport, "_call", side_effect=[repo, actor]) as call:
                with self.assertRaisesRegex(bridge.BridgeError, "runtime_github_owner_mismatch"):
                    self.transport.publish({"kind": "davos_result"})
                self.assertEqual(2, call.call_count)
                self.assertTrue(all("POST" not in invocation.args[0] for invocation in call.call_args_list))

    def test_cursor_pagination_has_no_full_page_boundary_error(self):
        first = [comment(comment_id=i + 1) for i in range(bridge.PAGE_SIZE)]
        with patch.object(self.transport, "_call", side_effect=[graph_page(first, True), graph_page([], False)]) as call:
            pages = list(self.transport.comment_pages(None))
        self.assertEqual(bridge.PAGE_SIZE, len(pages[0][0]))
        self.assertTrue(pages[1][1])
        self.assertEqual(str(bridge.PAGE_SIZE), call.call_args.args[1]["variables"]["after"])
        with patch.object(self.transport, "_call", side_effect=[graph_page(first, True), bridge.BridgeError("github_unavailable")]):
            pages = self.transport.comment_pages(None)
            self.assertEqual(bridge.PAGE_SIZE, len(next(pages)[0]))
            with self.assertRaises(bridge.BridgeError):
                next(pages)

    def test_receipt_edits_cannot_relax_request_authentication(self):
        node = graph_node(self.row)
        node.update(lastEditedAt=self.row["created_at"], includesCreatedEdit=True, editor={"__typename": "User"})
        with patch.object(self.transport, "_call", return_value={"data": {"node": node}}):
            with self.assertRaises(bridge.RequestRejected):
                self.transport.authenticate(self.row)
            self.transport.authenticate(self.row, receipt=True)
        node["author"]["databaseId"] = 1
        with patch.object(self.transport, "_call", return_value={"data": {"node": node}}):
            with self.assertRaises(bridge.RequestRejected):
                self.transport.authenticate(self.row, receipt=True)


class WorkBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = NOW
        self.req = request()
        self.transport = FakeTransport([comment(self.req)])
        self.calls = []
        self.action_result = {"status": "ok", "result": {"ready": True}}
        if sys.platform == "win32":
            # Windows validates the portable worker logic; production locking
            # and directory durability remain exercised unchanged on POSIX.
            lock = patch.object(bridge, "_lock", side_effect=self.fixture_lock)
            sync = patch.object(bridge, "_sync_state_directory")
            lock.start()
            sync.start()
            self.addCleanup(lock.stop)
            self.addCleanup(sync.stop)

    @staticmethod
    @contextmanager
    def fixture_lock(root):
        root.mkdir(parents=True, exist_ok=True)
        yield

    def validate(self, action, args):
        if action != "diagnostics.status" or args:
            raise ValueError("unsupported_action_or_arguments")

    def execute(self, action, args, *, owner):
        self.calls.append((action, args, owner))
        return copy.deepcopy(self.action_result)

    def worker(self):
        return bridge.WorkBridge(self.root, OWNER, transport=self.transport, validate_action=self.validate,
                                 execute_action=self.execute, clock=lambda: self.now, revision="a" * 40)

    def state(self):
        return bridge._load(self.root / ".work_bridge" / "state.json")

    def test_once_owner_binding_and_restart_replay(self):
        self.assertEqual("active", self.worker().poll()["state"])
        self.worker().poll()
        self.assertEqual([("diagnostics.status", {}, OWNER)], self.calls)
        result = self.transport.publish_calls[0]
        self.assertEqual("completed", result["state"])
        self.assertEqual(self.req["request_id"], result["request_id"])
        self.assertEqual(self.transport.rows[0]["id"], result["request_comment_id"])
        self.assertEqual("a" * 40, result["runtime_revision"])
        self.assertEqual(1, len(self.transport.publish_calls))

    def test_new_comment_reusing_request_id_cannot_rerun(self):
        self.worker().poll()
        self.transport.rows.append(comment(self.req, comment_id=5_355_000_001))
        altered = copy.deepcopy(self.req)
        altered["args"] = {"changed": True}
        self.transport.rows.append(comment(altered, comment_id=5_355_000_002))
        self.worker().poll()
        self.assertEqual(1, len(self.calls))

    def test_forged_author_and_edited_requests_do_not_execute(self):
        self.transport.rows[0]["user"]["id"] = 111
        self.worker().poll()
        self.assertEqual([], self.calls)
        self.assertEqual([], self.transport.auth_calls)
        self.transport.rows[0]["user"]["id"] = bridge.GITHUB_OWNER_ID
        self.transport.auth_error = bridge.RequestRejected("edited_or_email_request")
        self.worker().poll()
        self.assertEqual([], self.calls)
        self.assertEqual([], self.transport.publish_calls)

    def test_ttl_expired_and_future_requests_are_rejected(self):
        for offset in (-bridge.REQUEST_TTL - 1, 31):
            row = comment(request(), comment_id=5_355_000_000 + abs(offset), created=self.now + offset)
            self.transport.rows = [row]
            self.worker().poll()
            self.assertEqual("rejected", self.transport.publish_calls[-1]["state"])
        self.assertEqual([], self.calls)

    def test_unknown_action_and_remote_owner_arguments_cannot_dispatch(self):
        self.transport.rows = [comment(request(action="shell", args={"command": "touch x"})),
                               comment(request(args={"owner": "+15550000000"}), comment_id=5_355_000_000)]
        self.worker().poll()
        self.assertEqual([], self.calls)
        self.assertTrue(all(row["state"] == "rejected" for row in self.transport.publish_calls))

    def test_crash_after_action_never_reexecutes_and_returns_ambiguous(self):
        original = bridge._save
        count = []
        def simulated_crash(path, state):
            count.append(1)
            record = state["records"].get(self.req["request_id"], {})
            if record.get("phase") == "finished":
                raise OSError("crashed after action")
            original(path, state)
        with patch.object(bridge, "_save", simulated_crash):
            self.assertEqual("error", self.worker().poll()["state"])
        self.assertEqual("started", self.state()["records"][self.req["request_id"]]["phase"])
        self.worker().poll()
        self.assertEqual(1, len(self.calls))
        self.assertEqual("ambiguous", self.transport.publish_calls[-1]["state"])

    def test_failed_started_journal_prevents_action(self):
        with patch.object(bridge, "_save", side_effect=OSError("disk full")):
            self.worker().poll()
        self.assertEqual([], self.calls)

    def test_publication_failure_stays_reconciliation_only(self):
        self.transport.publish_error = "before"
        self.worker().poll()
        self.worker().poll()
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(self.transport.publish_calls))
        self.now += bridge.RESULT_RETRY_SECONDS + 1
        self.transport.publish_error = None
        self.worker().poll()
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(self.transport.publish_calls))
        self.assertIsNone(self.state()["records"][self.req["request_id"]]["published_comment_id"])

    def test_lost_publication_response_reconciles_remote_receipt_without_duplicate(self):
        self.transport.publish_error = "after"
        self.worker().poll()
        self.worker().poll()
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(self.transport.publish_calls))
        self.assertIsNotNone(self.state()["records"][self.req["request_id"]]["published_comment_id"])

    def test_page_or_auth_failure_does_not_advance_scan_cursor(self):
        self.worker().poll()
        previous = self.state()["scanned_at"]
        self.now += 10
        self.transport.scan_error = bridge.BridgeError("github_unavailable")
        self.worker().poll()
        self.assertEqual(previous, self.state()["scanned_at"])
        self.transport.scan_error = None
        self.transport.rows.append(comment(request(), comment_id=5_355_000_004))
        self.transport.auth_error = bridge.BridgeError("comment_auth_unavailable")
        self.worker().poll()
        self.assertEqual(previous, self.state()["scanned_at"])
        self.assertEqual(1, len(self.calls))

    def test_closed_issue_pauses_actions_and_never_reposts_uncertain_result(self):
        self.transport.publish_error = "before"
        self.worker().poll()
        self.transport.open = False
        self.transport.publish_error = None
        self.now += bridge.RESULT_RETRY_SECONDS + 1
        self.transport.rows.append(comment(request(), comment_id=5_355_000_005))
        self.assertEqual("paused", self.worker().poll()["state"])
        self.assertEqual(1, len(self.calls))
        self.assertIsNone(self.state()["records"][self.req["request_id"]]["published_comment_id"])
        self.assertEqual(1, len(self.transport.publish_calls))

    @unittest.skipIf(sys.platform == "win32", "Real flock requires POSIX; worker behavior is tested separately")
    def test_concurrency_fails_closed(self):
        with bridge._lock(self.root / ".work_bridge"):
            self.assertEqual("bridge_busy", self.worker().poll()["error"])
        self.assertEqual([], self.calls)

    def test_corrupt_and_deleted_state_fail_closed(self):
        self.worker().poll()
        state_path = self.root / ".work_bridge" / "state.json"
        state_path.write_text("broken")
        self.assertEqual("state_corrupt", self.worker().poll()["error"])
        state_path.unlink()
        self.assertEqual("state_missing", self.worker().poll()["error"])
        self.assertEqual(1, len(self.calls))

    def test_adapter_exception_and_ambiguous_evidence_do_not_retry(self):
        def failed(*args, **kwargs):
            self.calls.append("attempted")
            raise RuntimeError("private exception detail")
        worker = self.worker()
        worker.execute_action = failed
        worker.poll()
        worker.poll()
        self.assertEqual(["attempted"], self.calls)
        self.assertEqual("ambiguous", self.transport.publish_calls[-1]["state"])
        self.assertNotIn("private exception", json.dumps(self.state()))
        self.transport.rows.append(comment(request(), comment_id=5_355_000_006))
        self.action_result = {"status": "error", "evidence": {"ambiguous": True}}
        self.worker().poll()
        self.assertEqual("ambiguous", self.transport.publish_calls[-1]["state"])

    def test_gc_keeps_recent_and_unpublished_idempotency(self):
        self.worker().poll()
        old_id = self.req["request_id"]
        self.transport.rows = []
        self.now += bridge.REQUEST_TTL + 1
        self.worker().poll()
        self.assertIn(old_id, self.state()["records"])
        self.now += bridge.RETENTION_SECONDS
        self.worker().poll()
        self.assertNotIn(old_id, self.state()["records"])

    def test_action_batch_limit_leaves_unprocessed_requests_for_next_poll(self):
        self.transport.rows = [comment(request(), comment_id=5_355_000_100 + i) for i in range(12)]
        self.worker().poll()
        self.assertEqual(bridge.MAX_ACTIONS_PER_POLL, len(self.calls))
        self.worker().poll()
        self.assertEqual(12, len(self.calls))

    def test_result_redaction_precedes_storage_and_publication(self):
        self.action_result = {"status": "ok", "result": {"password": "secret123", "text": "ghp_" + "a" * 30}}
        self.worker().poll()
        serialized = json.dumps(self.state()) + json.dumps(self.transport.publish_calls)
        self.assertNotIn("secret123", serialized)
        self.assertNotIn("ghp_", serialized)
        self.assertIn("[redacted]", serialized)

    def test_1002_comment_backlog_recovers_saved_receipt_without_reexecution(self):
        self.transport.publish_error = "after"
        self.worker().poll()
        saved = copy.deepcopy(self.transport.publish_calls[0])
        # Simulate upgrade from the old journal, with no discovery sidecar.
        (self.root / ".work_bridge" / f"scan-{bridge.ISSUE_ID}.json").unlink()
        self.now += 2 * 86400
        duplicate = self.transport.rows[-1]
        self.transport.rows = [self.transport.rows[0]]
        for i in range(1000):
            row = copy.deepcopy(duplicate)
            row["id"] = 5_355_100_000 + i
            row["url"] = f"https://api.github.com/repos/{bridge.REPOSITORY}/issues/comments/{row['id']}"
            self.transport.rows.append(row)
        fresh = request()
        self.transport.rows.append(comment(fresh, comment_id=5_355_200_000, created=self.now - 1))
        self.transport.publish_error = None
        pending_seen = False
        for _ in range(20):
            result = self.worker().poll()
            self.assertEqual("active", result["state"])
            pending_seen |= result["scan_pending"]
            if not result["scan_pending"]:
                break
        self.assertTrue(pending_seen)
        self.assertFalse(result["scan_pending"])
        self.assertEqual(2, len(self.calls))
        self.assertEqual(2, len(self.transport.publish_calls))
        self.assertEqual(saved, self.state()["records"][self.req["request_id"]]["response"])
        self.assertEqual(5_355_100_000, self.state()["records"][self.req["request_id"]]["published_comment_id"])
        self.assertEqual(1003, len(self.transport.rows))  # Audit duplicates were not deleted.

    def test_exact_full_page_budget_is_success_and_resume_is_bounded(self):
        self.transport.rows = [comment({"kind": "ignored"}, comment_id=i + 1) for i in range(1000)]
        self.transport.rows.append(comment(self.req, comment_id=1001))
        cursors = []
        with patch.object(bridge, "MAX_PAGES", 2), patch.object(bridge, "PAGE_SIZE", 50):
            for _ in range(11):
                result = self.worker().poll()
                cursors.append(int(bridge._load_cursor(self.root / ".work_bridge" / f"scan-{bridge.ISSUE_ID}.json")))
        self.assertTrue(all(b - a <= 100 for a, b in zip([0] + cursors, cursors)))
        self.assertEqual(1, len(self.calls))
        self.assertFalse(result["scan_pending"])

    def test_partial_page_failure_preserves_consumed_checkpoint(self):
        rows = [comment({"kind": "ignored"}, comment_id=i + 1) for i in range(bridge.PAGE_SIZE)]
        def pages(cursor):
            yield [dict(row, scan_cursor=str(row["id"])) for row in rows], False
            raise bridge.BridgeError("github_unavailable")
        with patch.object(self.transport, "comment_pages", side_effect=pages):
            self.assertEqual("error", self.worker().poll()["state"])
        self.assertEqual(str(bridge.PAGE_SIZE), bridge._load_cursor(self.root / ".work_bridge" / f"scan-{bridge.ISSUE_ID}.json"))
        self.assertEqual([], self.calls)

    def test_closing_and_reopening_does_not_skip_a_fresh_request(self):
        self.transport.open = False
        self.assertEqual("paused", self.worker().poll()["state"])
        self.assertEqual([], self.calls)
        self.transport.open = True
        self.assertEqual("active", self.worker().poll()["state"])
        self.assertEqual(1, len(self.calls))

    def test_missing_sidecar_rescans_but_never_replays_saved_action(self):
        self.worker().poll()
        (self.root / ".work_bridge" / f"scan-{bridge.ISSUE_ID}.json").unlink()
        self.worker().poll()
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(self.transport.publish_calls))

    def test_corrupt_sidecar_fails_closed_without_touching_journal(self):
        self.worker().poll()
        journal = (self.root / ".work_bridge" / "state.json").read_bytes()
        (self.root / ".work_bridge" / f"scan-{bridge.ISSUE_ID}.json").write_text("broken")
        self.assertEqual("scan_state_corrupt", self.worker().poll()["error"])
        self.assertEqual(journal, (self.root / ".work_bridge" / "state.json").read_bytes())
        self.assertEqual(1, len(self.calls))

    def test_checkpoint_failure_after_action_cannot_reexecute(self):
        original = bridge._save_payload
        def fail_scan(path, payload):
            if path.name == f"scan-{bridge.ISSUE_ID}.json":
                raise OSError("disk full")
            original(path, payload)
        with patch.object(bridge, "_save_payload", side_effect=fail_scan):
            self.assertEqual("error", self.worker().poll()["state"])
        self.worker().poll()
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(self.transport.publish_calls))

    def test_unverified_receipt_does_not_trigger_another_post(self):
        self.transport.publish_error = "after"
        self.worker().poll()
        self.now += bridge.RESULT_RETRY_SECONDS + 1
        self.transport.auth_error = bridge.RequestRejected("comment_auth_mismatch")
        result = self.worker().poll()
        self.assertEqual("receipt_auth_unconfirmed", result["error"])
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(self.transport.publish_calls))

    def test_old_unjournaled_requests_do_not_generate_new_receipts_on_rescan(self):
        self.transport.rows[0]["created_at"] = bridge._iso(self.now - bridge.RETENTION_SECONDS - 1)
        self.transport.rows[0]["updated_at"] = self.transport.rows[0]["created_at"]
        self.worker().poll()
        self.assertEqual([], self.calls)
        self.assertEqual([], self.transport.publish_calls)

    def test_receipt_reconciliation_requires_exact_saved_bytes(self):
        self.transport.publish_error = "after"
        self.worker().poll()
        receipt = self.transport.rows[-1]
        altered = json.loads(receipt["body"])
        altered["state"] = "failed"
        receipt["body"] = bridge._json_bytes(altered).decode()
        self.transport.publish_error = None
        self.now += bridge.RESULT_RETRY_SECONDS + 1
        self.worker().poll()
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(self.transport.publish_calls))
        self.assertIsNone(self.state()["records"][self.req["request_id"]]["published_comment_id"])
        self.assertEqual("completed", self.transport.publish_calls[-1]["state"])


class CursorTransportTests(unittest.TestCase):
    def setUp(self):
        self.transport = bridge.GitHubTransport(".")
        self.rows = [comment(comment_id=i + 1) for i in range(bridge.PAGE_SIZE)]

    def test_page_repository_identity_and_cursor_shape_fail_closed(self):
        for mutate in (
            lambda r: r["data"]["repository"].update(isPrivate=False),
            lambda r: r["data"]["repository"].update(databaseId=1),
            lambda r: r["data"]["repository"]["owner"].update(databaseId=1),
            lambda r: r["data"]["repository"]["issue"].update(fullDatabaseId="1"),
            lambda r: r["data"]["repository"]["issue"]["comments"]["pageInfo"].update(endCursor="wrong"),
            lambda r: r["data"]["repository"]["issue"]["comments"]["edges"][0].update(cursor="\n"),
        ):
            response = graph_page(self.rows)
            mutate(response)
            with patch.object(self.transport, "_call", return_value=response):
                with self.assertRaises(bridge.BridgeError):
                    list(self.transport.comment_pages(None))

    def test_page_size_bounds_maximum_escaped_comment_bodies(self):
        for character in ("\x01", "\U00010000"):
            for row in self.rows:
                row["body"] = character * 65536
            raw = bridge._json_bytes(graph_page(self.rows))
            self.assertLess(len(raw), 2_097_152)

    def test_repeated_cursor_and_empty_next_page_fail_closed(self):
        response = graph_page(self.rows, True)
        with patch.object(self.transport, "_call", return_value=response):
            pages = self.transport.comment_pages(None)
            next(pages)
            with self.assertRaises(bridge.BridgeError):
                next(pages)
        with patch.object(self.transport, "_call", return_value=graph_page([], True)):
            with self.assertRaises(bridge.BridgeError):
                list(self.transport.comment_pages(None))


class SchemaTests(unittest.TestCase):
    def test_uuid4_exact_schema_duplicate_keys_and_size_limits(self):
        for data in (request(request_id=str(uuid.uuid1())), request(action="x;touch x"), request(extra="value"), request(args=[])):
            with self.assertRaises(bridge.BridgeError):
                bridge.parse_request(json.dumps(data))
        with self.assertRaises(bridge.BridgeError):
            bridge.parse_request('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(bridge.BridgeError):
            bridge.parse_request("x" * (bridge.MAX_REQUEST_BYTES + 1))

    def test_result_cap_nonfinite_and_secret_redaction(self):
        self.assertEqual({"value": "[redacted]"}, bridge.safe_result({"value": "local-secret"}, lambda value: value.replace("local-secret", "[redacted]")))
        for value in ({"large": "x" * bridge.MAX_RESULT_BYTES}, {"number": float("nan")}, {"object": object()}):
            with self.assertRaises(ValueError):
                bridge.safe_result(value)

    def test_real_capability_schemas_fit_without_truncation(self):
        from davosbot.work_actions import action_catalogue
        catalogue = action_catalogue()
        cleaned = bridge.safe_result({"status": "ok", "evidence": {"actions": catalogue}})
        self.assertEqual(set(catalogue), set(cleaned["evidence"]["actions"]))

    def test_non_mac_start_no_runtime_import(self):
        with patch.object(bridge.sys, "platform", "linux"):
            self.assertIsNone(bridge.start_work_bridge())


if __name__ == "__main__":
    unittest.main()
