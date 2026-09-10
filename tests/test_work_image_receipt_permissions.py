"""Authenticated request-to-job receipts, real temporary persistence, mocked sends."""
from contextlib import ExitStack, closing, contextmanager
import ast
import json
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock, patch

from davosbot import work_image_receipts as receipts
from davosbot import work_actions as actions, work_actions_extra as extra
from test_openai_image_routing import _load_main_function

OWNER = "+15550000001"
OTHER = "+15550000002"
COMMENT = 5_354_900_001
JOB = "1788629000000-1234"


def _native_send_file(imessage):
    # Strict suites replace exported send helpers with no-send guards. Execute
    # the real function body only with the lowest service/DB boundaries mocked.
    tree = ast.parse(Path(imessage.__file__).read_text(encoding="utf-8"))
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "send_file")
    namespace = dict(vars(imessage))
    exec(compile(ast.Module(body=[node], type_ignores=[]), imessage.__file__, "exec"), namespace)
    return namespace["send_file"]


class ReceiptFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / ".work_bridge"
        self.request_id = str(uuid.uuid4())
        self.addCleanup(receipts._LIVE.clear)

    def tracker(self):
        with receipts.request_scope(self.request_id, COMMENT, OWNER, self.root, "8af6817"):
            return receipts.ImageTracker(OWNER)

    def lookup(self, **changes):
        return receipts.receipt(changes.get("request_id", self.request_id), changes.get("comment_id", COMMENT),
                                changes.get("owner", OWNER), root=self.root)

    def prepare(self):
        tracker = self.tracker()
        tracker.prepare(JOB, OWNER, OWNER, False, "local")
        return tracker


class ReceiptTests(ReceiptFixture, unittest.TestCase):
    def test_missing_lookup_does_not_create_state_and_is_not_retry_authority(self):
        result = self.lookup()
        self.assertFalse(self.root.exists())
        self.assertEqual("unknown", result["receipt_state"])
        self.assertFalse(result["retry_safe"])
        self.assertFalse(result["send_verified"])

    def test_scope_is_required_owner_bound_thread_local_and_reset(self):
        with self.assertRaisesRegex(ValueError, "authenticated_image_request_required"):
            receipts.ImageTracker(OWNER)
        outcomes = []
        with receipts.request_scope(self.request_id, COMMENT, OWNER, self.root):
            with self.assertRaises(ValueError):
                receipts.ImageTracker(OTHER)
            thread = threading.Thread(target=lambda: outcomes.append(receipts._REQUEST.get()))
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual([None], outcomes)
        self.assertIsNone(receipts._REQUEST.get())

    def test_complete_send_retained_across_restart_without_read_or_delivery_claim(self):
        tracker = self.prepare()
        self.assertEqual("queued", self.lookup()["job_state"])
        tracker.mark("generating")
        tracker.mark("sending", provider="gemini")
        tracker.mark("sent")
        tracker.close()
        with patch.object(receipts, "_PROCESS_ID", str(uuid.uuid4())):
            result = self.lookup()
        self.assertEqual("sent", result["job_state"])
        self.assertTrue(result["send_verified"])
        self.assertEqual("unknown", result["delivery_state"])
        self.assertEqual("gemini", result["provider"])
        self.assertEqual("8af6817", result["runtime_revision"])
        for private in (OWNER, "prompt", "path", "owner_key", "process_id"):
            self.assertNotIn(private, json.dumps(result))

    def test_restart_or_missing_worker_never_leaves_false_running_state(self):
        tracker = self.prepare()
        tracker.mark("generating")
        with patch.object(receipts, "_PROCESS_ID", str(uuid.uuid4())):
            self.assertEqual("unknown", self.lookup()["job_state"])
        self.assertEqual("generating", self.lookup()["job_state"])
        tracker.close()
        result = self.lookup()
        self.assertEqual("execution_interrupted", result["reason"])
        self.assertFalse(result["retry_safe"])

    def test_wrong_identity_cannot_observe_or_rebind_request(self):
        tracker = self.prepare()
        for changes in ({"owner": OTHER}, {"comment_id": COMMENT + 1}, {"request_id": str(uuid.uuid4())}):
            result = self.lookup(**changes)
            self.assertEqual("unknown", result["receipt_state"])
            self.assertNotIn("job_id", result)
        with self.assertRaises(sqlite3.IntegrityError):
            self.tracker().prepare("1788629000001-1234", OWNER, OWNER, False, "local")
        with self.assertRaises(ValueError):
            tracker.prepare(JOB, OWNER, OWNER, False, "local")

    def test_group_and_other_destination_bindings_rejected_before_creating_files(self):
        for sender, recipient, group in ((OTHER, OWNER, False), (OWNER, OTHER, False), (OWNER, OWNER, True)):
            with self.assertRaises(ValueError):
                self.tracker().prepare(JOB, sender, recipient, group, "local")
        self.assertFalse(self.root.exists())

    def test_illegal_or_stale_transitions_cannot_promote_unknown_to_sent(self):
        tracker = self.prepare()
        with self.assertRaises(ValueError):
            tracker.mark("sent")
        tracker.mark("generating")
        tracker.mark("sending")
        tracker.mark("unknown", reason="send_unverified")
        with self.assertRaises(ValueError):
            tracker.mark("sent")
        self.assertFalse(self.lookup()["send_verified"])

    def test_corrupt_and_future_schema_reads_are_unknown(self):
        self.prepare()
        with closing(sqlite3.connect(self.root / "image_receipts.sqlite3")) as conn, conn:
            conn.execute("UPDATE image_receipts SET state='invented'")
        self.assertEqual("unknown", self.lookup()["receipt_state"])
        with closing(sqlite3.connect(self.root / "image_receipts.sqlite3")) as conn, conn:
            conn.execute("PRAGMA user_version=999")
        self.assertEqual("unknown", self.lookup()["receipt_state"])

    def test_store_capacity_refuses_new_job_without_purging_receipts(self):
        with patch.object(receipts, "_MAX_RECORDS", 0), self.assertRaisesRegex(ValueError, "capacity"):
            self.prepare()
        self.assertEqual(set(), receipts._LIVE)

    @unittest.skipUnless(__import__("os").name != "nt", "POSIX filesystem permissions")
    def test_private_modes_and_symlinks_fail_closed(self):
        import os
        self.prepare()
        self.assertEqual(0o700, self.root.stat().st_mode & 0o777)
        path = self.root / "image_receipts.sqlite3"
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        outside = Path(self.temp.name) / "outside.sqlite3"
        path.rename(outside)
        path.symlink_to(outside)
        before = outside.read_bytes()
        self.assertEqual("unknown", self.lookup()["receipt_state"])
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.tracker().prepare("1788629000001-1234", OWNER, OWNER, False, "local")
        self.assertEqual(before, outside.read_bytes())


class NativeJobReceiptTests(ReceiptFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.result = SimpleNamespace(ok=True, path="/synthetic/image.png", api_called=False, provider="local", message="")
        self.generate = Mock(side_effect=lambda prompt: self.result)
        self.send = Mock(return_value=True)
        self.messages = Mock(return_value=True)
        self.active = {}

        class ImmediateThread:
            def __init__(self, target, args, **kwargs):
                self.target, self.args = target, args
            def start(self):
                self.target(*self.args)
            def is_alive(self):
                return False

        self.namespace = {
            "threading": SimpleNamespace(Thread=ImmediateThread), "time": time, "random": random,
            "_IMAGE_JOB_LOCK": threading.RLock(), "_ACTIVE_IMAGE_JOBS": self.active,
            "choose_generation_provider": lambda: "local", "estimate_generation_time": lambda provider: "1 minute",
            "generate_image": self.generate, "generate_local_image": self.generate,
            "generate_gemini_image": self.generate, "generate_openai_image": self.generate,
            "generate_nano_banana_image": self.generate, "send_file": self.send, "send_message": self.messages,
            "is_owner": lambda actor: actor == OWNER, "log_tool_use": Mock(), "logger": Mock(),
            "redact_secret": lambda value: value, "_remember_generated_image": Mock(),
        }
        self.start = _load_main_function("_start_image_generation_job", self.namespace, dependencies=(
            "_image_context_key", "_format_image_job_line", "_finish_image_job",
            "_run_image_generation_job", "_compose_reference_generation_prompt"))

    def start_job(self, tracker=None):
        tracker = tracker or self.tracker()
        response = self.start(OWNER, "Synthetic fox", None, OWNER, tracking=tracker)
        return response, tracker

    def test_actual_runner_saves_sending_before_single_send_then_retains_success(self):
        self.send.side_effect = lambda *args, **kwargs: self.assertEqual("sending", self.lookup()["job_state"]) or True
        _, tracker = self.start_job()
        self.assertEqual("sent", self.lookup()["job_state"])
        self.assertEqual(tracker.job_id, self.lookup()["job_id"])
        self.send.assert_called_once()
        self.assertEqual({}, self.active)

    def test_send_timeout_or_false_is_unknown_and_is_never_retried(self):
        for effect in (False, OSError("synthetic timeout")):
            with self.subTest(effect=type(effect).__name__):
                self.request_id = str(uuid.uuid4())
                self.send.reset_mock()
                self.send.side_effect = effect if isinstance(effect, Exception) else None
                self.send.return_value = False
                self.start_job()
                self.assertEqual("unknown", self.lookup()["job_state"])
                self.send.assert_called_once()
                self.lookup()
                self.send.assert_called_once()

    def test_actual_attachment_timeout_never_relaunches_or_repeats_submission(self):
        from davosbot import imessage
        with patch.object(imessage, "_stage_outbound_attachment", return_value=Path("/synthetic/staged.png")), \
                patch.object(imessage, "_latest_message_rowid", return_value=42), \
                patch.object(imessage, "_run_osascript", side_effect=subprocess.TimeoutExpired("osascript", 1)) as run, \
                patch.object(imessage, "_verify_file_send", return_value=False) as verify, \
                patch.object(imessage, "_hard_relaunch_messages", return_value=True) as relaunch, \
                patch.object(imessage, "_schedule_applescript_recovery_retry") as schedule:
            self.start.__globals__["send_file"] = _native_send_file(imessage)
            self.start_job()
        run.assert_called_once()
        verify.assert_called_once()
        relaunch.assert_not_called()
        schedule.assert_not_called()
        self.assertEqual("unknown", self.lookup()["job_state"])

    def test_actual_timed_out_attachment_can_verify_without_resubmission(self):
        from davosbot import imessage
        with patch.object(imessage, "_stage_outbound_attachment", return_value=Path("/synthetic/staged.png")), \
                patch.object(imessage, "_latest_message_rowid", return_value=42), \
                patch.object(imessage, "_run_osascript", side_effect=subprocess.TimeoutExpired("osascript", 1)) as run, \
                patch.object(imessage, "_verify_file_send", return_value=True) as verify, \
                patch.object(imessage, "_hard_relaunch_messages") as relaunch:
            self.start.__globals__["send_file"] = _native_send_file(imessage)
            self.start_job()
        run.assert_called_once()
        verify.assert_called_once()
        relaunch.assert_not_called()
        self.assertEqual("sent", self.lookup()["job_state"])

    def test_actual_bridge_error_never_relaunches_or_repeats_submission(self):
        from davosbot import imessage
        with patch.object(imessage, "_stage_outbound_attachment", return_value=Path("/synthetic/staged.png")), \
                patch.object(imessage, "_latest_message_rowid", return_value=42), \
                patch.object(imessage, "_run_osascript", return_value=SimpleNamespace(returncode=1, stderr="Application isn't running")) as run, \
                patch.object(imessage, "_looks_like_messages_bridge_failure", return_value=True), \
                patch.object(imessage, "_verify_file_send", return_value=False) as verify, \
                patch.object(imessage, "_hard_relaunch_messages") as relaunch, \
                patch.object(imessage, "_schedule_applescript_recovery_retry") as schedule:
            self.start.__globals__["send_file"] = _native_send_file(imessage)
            self.start_job()
        run.assert_called_once()
        verify.assert_called_once()
        relaunch.assert_not_called()
        schedule.assert_not_called()
        self.assertEqual("unknown", self.lookup()["job_state"])

    def test_generation_failure_is_terminal_without_attachment_send(self):
        self.result.ok = False
        self.start_job()
        self.assertEqual("failed", self.lookup()["job_state"])
        self.send.assert_not_called()

    def test_failed_pre_send_checkpoint_prevents_attachment_action(self):
        tracker = self.tracker()
        real_mark = tracker.mark
        def mark(state, **kwargs):
            if state == "sending":
                raise OSError("synthetic disk failure")
            return real_mark(state, **kwargs)
        tracker.mark = mark
        self.start_job(tracker)
        self.send.assert_not_called()
        self.assertEqual("unknown", self.lookup()["job_state"])

    def test_failed_post_send_checkpoint_is_unknown_not_replayed(self):
        tracker = self.tracker()
        real_mark = tracker.mark
        def mark(state, **kwargs):
            if state in {"sent", "unknown"}:
                raise OSError("synthetic disk failure")
            return real_mark(state, **kwargs)
        tracker.mark = mark
        self.start_job(tracker)
        self.send.assert_called_once()
        self.assertEqual("unknown", self.lookup()["job_state"])
        self.messages.assert_not_called()

    def test_cache_failure_cannot_overwrite_verified_send(self):
        self.start.__globals__["_remember_generated_image"].side_effect = RuntimeError("synthetic cache error")
        self.start_job()
        self.assertEqual("sent", self.lookup()["job_state"])
        self.messages.assert_not_called()

    def test_initial_checkpoint_failure_prevents_generation_and_queue_admission(self):
        tracker = self.tracker()
        tracker.prepare = Mock(side_effect=OSError("synthetic persistence failure"))
        with self.assertRaises(OSError):
            self.start_job(tracker)
        self.generate.assert_not_called()
        self.send.assert_not_called()
        self.assertEqual({}, self.active)

    def test_thread_construction_failure_releases_queue_and_records_failure(self):
        self.start.__globals__["threading"] = SimpleNamespace(Thread=Mock(side_effect=RuntimeError("synthetic start failure")))
        with self.assertRaises(RuntimeError):
            self.start_job()
        self.assertEqual("failed", self.lookup()["job_state"])
        self.assertEqual({}, self.active)
        self.generate.assert_not_called()

    def test_busy_queue_does_not_bind_new_request_to_existing_image(self):
        key = self.start.__globals__["_image_context_key"](OWNER, OWNER, False, "")
        self.active[key] = {"job_id": JOB, "started_ts": time.time()}
        response, tracker = self.start_job()
        self.assertIn("1 active image job", response)
        self.assertIsNone(tracker.job_id)
        self.assertFalse(self.root.exists())
        self.generate.assert_not_called()


class AdapterReceiptPermissionTests(unittest.TestCase):
    def test_lookup_denies_nonowner_and_context_arguments_before_state_access(self):
        with patch("davosbot.config.OWNER_ID", OWNER), patch("davosbot.permissions.OWNER_ID", OWNER), \
                patch.object(receipts, "receipt") as lookup:
            args = {"request_id": str(uuid.uuid4()), "request_comment_id": COMMENT}
            self.assertEqual("error", actions.execute_action("images.receipt", args, owner=OTHER)["status"])
            for key in ("owner", "recipient", "path", "job_id"):
                self.assertEqual("error", actions.execute_action("images.receipt", {**args, key: "bad"}, owner=OWNER)["status"])
            lookup.assert_not_called()

    def test_busy_generation_is_not_an_acceptance_for_another_job(self):
        tracker = SimpleNamespace(job_id=None)
        native = Mock(return_value="1 active image job; elapsed 3s")
        modules = {
            "config": SimpleNamespace(OWNER_ID=OWNER), "permissions": SimpleNamespace(is_owner=lambda actor: actor == OWNER),
            "main": SimpleNamespace(_IMAGE_QUEUE_STATUS_RE=__import__("re").compile(r"(?!)"),
                                    _IMAGE_QUEUE_SEND_RE=__import__("re").compile(r"(?!)"),
                                    _LAST_GENERATED_IMAGE_RE=__import__("re").compile(r"(?!)"),
                                    _handle_openai_image_intent=native),
            "image_conversation": SimpleNamespace(is_image_followup=lambda text: False),
            "openai_images": SimpleNamespace(parse_openai_image_intent=lambda *a, **kw: SimpleNamespace(kind="generate")),
        }
        with patch.object(extra, "_module", side_effect=lambda name: modules[name]), patch.object(receipts, "ImageTracker", return_value=tracker):
            result = extra.execute_extra_action("images.generate", {"prompt": "synthetic fox"}, OWNER)
        self.assertEqual("error", result["status"])
        self.assertEqual("image_queue_busy", result["evidence"]["code"])
        self.assertFalse(result["evidence"]["started"])


class BridgeReceiptIntegrationTests(unittest.TestCase):
    def test_authenticated_identity_survives_publication_failure_and_never_replays_job(self):
        from davosbot import work_bridge as bridge
        from test_work_bridge import FakeTransport, request, comment, NOW
        req = request(action="images.generate", args={"prompt": "Synthetic private prompt must not enter receipt"})
        transport = FakeTransport([comment(req, comment_id=COMMENT)])
        calls = []
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            @contextmanager
            def windows_lock(path):
                path.mkdir(parents=True, exist_ok=True)
                yield
            if sys.platform == "win32":
                stack.enter_context(patch.object(bridge, "_lock", side_effect=windows_lock))
                stack.enter_context(patch.object(bridge, "_sync_state_directory"))
            def execute(action, args, *, owner):
                journal = bridge._load(root / ".work_bridge" / "state.json")
                self.assertEqual("started", journal["records"][req["request_id"]]["phase"])
                tracker = receipts.ImageTracker(owner)
                tracker.prepare(JOB, owner, owner, False, "local")
                tracker.mark("generating")
                tracker.mark("sending")
                calls.append(JOB)  # Synthetic attachment send boundary.
                tracker.mark("sent")
                tracker.close()
                return {"status": "accepted", "evidence": {"job_id": JOB}}
            def worker():
                return bridge.WorkBridge(root, OWNER, transport=transport, validate_action=actions.validate_action,
                                         execute_action=execute, clock=lambda: NOW, revision="a" * 40)
            transport.auth_error = bridge.RequestRejected("edited_or_email_request")
            worker().poll()
            self.assertEqual([], calls)
            self.assertIsNone(receipts._REQUEST.get())
            transport.auth_error = None
            transport.publish_error = "before"
            worker().poll()
            self.assertIsNone(receipts._REQUEST.get())
            result = receipts.receipt(req["request_id"], COMMENT, OWNER, root=root / ".work_bridge")
            self.assertTrue(result["send_verified"])
            transport.publish_error = None
            with patch.object(receipts, "_PROCESS_ID", str(uuid.uuid4())):
                worker().poll()
                retained = receipts.receipt(req["request_id"], COMMENT, OWNER, root=root / ".work_bridge")
            self.assertEqual([JOB], calls)
            self.assertTrue(retained["send_verified"])
            self.assertEqual(req["request_id"], transport.publish_calls[-1]["request_id"])
            self.assertNotIn(req["args"]["prompt"], (root / ".work_bridge" / "image_receipts.sqlite3").read_bytes().decode("latin1"))


if __name__ == "__main__":
    unittest.main()
