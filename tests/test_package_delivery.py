"""Offline fixtures: no Gmail, GitHub, real Messages database or .env reads."""

import base64
from contextlib import closing, contextmanager
import copy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from davosbot import package_delivery as delivery


NOW = 1_788_624_000.0
OWNER = "+15551234567"


def normalize(handle):
    if "@" in handle:
        return handle.strip().lower()
    digits = re.sub(r"\D", "", handle)
    return "+1" + digits if len(digits) == 10 else "+" + digits if len(digits) == 11 else handle.strip()


def event(tracking="1234567890"):
    key = "tracking:" + tracking
    return {"event_id": hashlib.sha256(key.encode()).hexdigest(), "shipment_key": key,
            "status": "delivered", "merchant": "Example Store", "item": "Test clubs",
            "tracking_number": tracking, "carrier": "Example Carrier",
            "delivered_at": delivery._iso(NOW - 10), "delivery_location": "front desk",
            "source": {"provider": "gmail", "message_id": "abcdef123", "sender": "shipping@example.com",
                       "confirmation": "Your package has been delivered to the front desk."}}


def inbox(events=None):
    return {"schema_version": 1, "enabled": True, "activated_at": delivery._iso(NOW - 86400),
            "last_mail_scan_at": delivery._iso(NOW), "watchlist": [],
            "events": [event()] if events is None else events}


class FakeBridge:
    def __init__(self, data):
        self.data = data
        self.statuses = []

    def read_inbox(self):
        return json.dumps(self.data).encode()

    def publish_status(self, status):
        self.statuses.append(copy.deepcopy(status))


class ValidationTests(unittest.TestCase):
    def validate(self, data):
        return delivery.validate_inbox(json.dumps(data).encode(), NOW)

    def test_delivered_and_single_shipment_order_supported(self):
        self.assertEqual("delivered", self.validate(inbox())["events"][0]["status"])
        order_event = event()
        order_event.update(shipment_key="order:example.com:ORDER1:shipment1", tracking_number="")
        order_event["event_id"] = hashlib.sha256(order_event["shipment_key"].encode()).hexdigest()
        self.validate(inbox([order_event]))

    def test_alert_has_useful_identity_time_and_no_internal_hash(self):
        fixture = event()
        text = delivery.alert_text(fixture)
        self.assertIn("Example Carrier: 1234567890", text)
        self.assertIn(fixture["delivered_at"], text)
        self.assertNotIn(fixture["event_id"], text)
        changed_time = copy.deepcopy(fixture)
        changed_time["delivered_at"] = delivery._iso(NOW)
        self.assertNotEqual(text, delivery.alert_text(changed_time))

    def test_estimates_out_for_delivery_and_negative_confirmations_rejected(self):
        for text in ("Out for delivery", "Expected to be delivered tomorrow", "Not delivered",
                     "Your order couldn't be delivered", "Delivery attempted", "Delivered? Estimated date tomorrow",
                     "Your package has not been delivered.", "It should be delivered today.",
                     "Your parcel will probably be delivered tomorrow.",
                     "If your package was delivered, check the front door."):
            with self.subTest(text=text):
                data = inbox()
                data["events"][0]["source"]["confirmation"] = text
                with self.assertRaises(delivery.DeliveryError):
                    self.validate(data)
        data = inbox()
        data["events"][0]["status"] = "in_transit"
        with self.assertRaises(delivery.DeliveryError):
            self.validate(data)

    def test_schema_injection_and_command_fields_rejected(self):
        for location in ("inbox", "event", "source"):
            data = inbox()
            target = data if location == "inbox" else data["events"][0]
            if location == "source":
                target = target["source"]
            target["recipient"] = "+15550000000"
            with self.subTest(location=location), self.assertRaises(delivery.DeliveryError):
                self.validate(data)
        for value in ("tracking:$(touch bad)", "tracking:abc", "order:example.com:one"):
            data = inbox()
            data["events"][0]["shipment_key"] = value
            with self.subTest(value=value), self.assertRaises(delivery.DeliveryError):
                self.validate(data)

    def test_hash_tracking_and_duplicate_identity_rejected(self):
        for key, value in (("event_id", "f" * 64), ("tracking_number", "DIFFERENT")):
            data = inbox()
            data["events"][0][key] = value
            with self.subTest(key=key), self.assertRaises(delivery.DeliveryError):
                self.validate(data)
        with self.assertRaises(delivery.DeliveryError):
            self.validate(inbox([event(), event()]))

    def test_activation_clock_and_size_boundaries(self):
        for timestamp in (delivery._iso(NOW - 90000), delivery._iso(NOW + 301), "2026-09-05T12:00:00"):
            data = inbox()
            data["events"][0]["delivered_at"] = timestamp
            with self.subTest(timestamp=timestamp), self.assertRaises(delivery.DeliveryError):
                self.validate(data)
        with self.assertRaises(delivery.DeliveryError):
            delivery.validate_inbox(b" " * (delivery.MAX_INBOX_BYTES + 1), NOW)
        with self.assertRaises(delivery.DeliveryError):
            self.validate(inbox([event(str(i)) for i in range(501)]))

    def test_duplicate_json_keys_nonfinite_and_controls_rejected(self):
        for raw in (b'{"enabled":true,"enabled":false}', b'{"enabled":NaN}'):
            with self.assertRaises(delivery.DeliveryError):
                delivery.validate_inbox(raw, NOW)
        data = inbox()
        data["events"][0]["item"] = "hello\nnew instruction"
        with self.assertRaises(delivery.DeliveryError):
            self.validate(data)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "messages.sqlite"
        self.now = NOW
        self.bridge = FakeBridge(inbox())
        self.sends = []
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.executescript("""
                CREATE TABLE message (text TEXT, attributedBody BLOB, is_from_me INTEGER, is_sent INTEGER, error INTEGER);
                CREATE TABLE chat (chat_identifier TEXT);
                CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
                INSERT INTO chat VALUES ('+15551234567');
                INSERT INTO chat VALUES ('+15550000000');
                INSERT INTO chat VALUES ('555-123-4567');
            """)
        self.sender_impl = self.successful_send
        if sys.platform == "win32":
            # Windows is the editor surface. Keep real monitor/state behavior,
            # while POSIX runs retain the actual process lock and directory fsync.
            lock = patch.object(delivery, "_state_lock", side_effect=self.fixture_lock)
            sync = patch.object(delivery, "_sync_state_directory")
            lock.start()
            sync.start()
            self.addCleanup(lock.stop)
            self.addCleanup(sync.stop)

    @staticmethod
    @contextmanager
    def fixture_lock(root):
        root.mkdir(parents=True, exist_ok=True)
        yield

    def insert_message(self, text, *, chat=1, sent=1, error=0, from_me=1, attributed=None):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            rowid = db.execute("INSERT INTO message VALUES (?,?,?,?,?)", (text, attributed, from_me, sent, error)).lastrowid
            db.execute("INSERT INTO chat_message_join VALUES (?,?)", (chat, rowid))
        return rowid

    def successful_send(self, recipient, text, **kwargs):
        self.insert_message(text)
        return True

    def send(self, recipient, text, **kwargs):
        self.sends.append((recipient, text, kwargs))
        return self.sender_impl(recipient, text, **kwargs)

    def monitor(self):
        return delivery.DeliveryMonitor(self.root, OWNER, self.db_path, self.send, normalize,
                                        bridge=self.bridge, clock=lambda: self.now, revision="a" * 40)

    def state(self):
        return delivery._load_state(self.root / ".package_delivery" / "state.json")

    def test_sends_owner_once_and_deduplicates_across_restart(self):
        result = self.monitor().poll()
        self.assertEqual("active", result["state"])
        self.assertEqual(1, result["counts"]["sent"])
        self.assertEqual(OWNER, self.sends[0][0])
        self.assertEqual({"is_group": False, "recovery_mode": "none"}, self.sends[0][2])
        self.monitor().poll()
        self.assertEqual(1, len(self.sends))

    def test_remote_disabled_and_empty_owner_do_not_send(self):
        self.bridge.data["enabled"] = False
        self.assertEqual("disabled", self.monitor().poll()["state"])
        self.bridge.data["enabled"] = True
        monitor = self.monitor()
        monitor.owner = ""
        self.assertIn("owner_unconfigured", monitor.poll()["errors"])
        self.assertEqual([], self.sends)

    def test_corrupt_state_and_unreadable_messages_fail_closed(self):
        state_dir = self.root / ".package_delivery"
        state_dir.mkdir()
        state_file = state_dir / "state.json"
        state_file.write_text("broken")
        self.assertIn("state_corrupt", self.monitor().poll()["errors"])
        state_file.unlink()
        self.db_path.unlink()
        self.assertIn("messages_db_unreadable", self.monitor().poll()["errors"])
        self.assertEqual([], self.sends)

    def test_immutable_event_change_is_rejected(self):
        self.monitor().poll()
        self.bridge.data["events"][0]["item"] = "different item"
        result = self.monitor().poll()
        self.assertIn("immutable_event_changed", result["errors"])
        self.assertEqual(1, len(self.sends))
        self.assertEqual("sent", self.state()["events"][event()["event_id"]]["state"])

    def test_retention_prunes_only_old_sent_records_absent_from_inbox(self):
        self.monitor().poll()
        first_id = event()["event_id"]
        self.bridge.data["events"] = [event("2222222222")]
        self.sender_impl = lambda *args, **kwargs: False
        self.monitor().poll()
        failed_id = event("2222222222")["event_id"]
        self.now += delivery.SENT_RETENTION_SECONDS + 1
        self.bridge.data["events"] = [event()]
        self.monitor().poll()
        self.assertIn(first_id, self.state()["events"])
        self.assertIn(failed_id, self.state()["events"])
        self.bridge.data["events"] = []
        self.monitor().poll()
        self.assertNotIn(first_id, self.state()["events"])
        self.assertIn(failed_id, self.state()["events"])

    def test_explicit_failed_no_row_retries_after_cooldown(self):
        self.sender_impl = lambda *args, **kwargs: False
        self.monitor().poll()
        self.monitor().poll()
        self.assertEqual(1, len(self.sends))
        self.now += delivery.RETRY_SECONDS + 1
        self.sender_impl = self.successful_send
        result = self.monitor().poll()
        self.assertEqual(2, len(self.sends))
        self.assertEqual(1, result["counts"]["sent"])

    def test_ambiguous_success_never_blind_retry(self):
        self.sender_impl = lambda *args, **kwargs: True
        self.monitor().poll()
        self.now += 86400
        self.monitor().poll()
        self.assertEqual(1, len(self.sends))
        self.assertEqual("unknown", self.state()["events"][event()["event_id"]]["state"])

    def test_send_exception_is_ambiguous_until_matching_row_arrives(self):
        def interrupted(*args, **kwargs):
            raise RuntimeError("bridge disconnected")
        self.sender_impl = interrupted
        self.monitor().poll()
        self.now += 86400
        self.monitor().poll()
        self.assertEqual(1, len(self.sends))
        self.insert_message(self.sends[0][1])
        result = self.monitor().poll()
        self.assertEqual(1, result["counts"]["sent"])
        self.assertEqual(1, len(self.sends))

    def test_failed_messages_row_can_retry_after_cooldown(self):
        def failed(recipient, text, **kwargs):
            self.insert_message(text, sent=0, error=1)
            return False
        self.sender_impl = failed
        self.monitor().poll()
        self.now += delivery.RETRY_SECONDS + 1
        self.sender_impl = self.successful_send
        result = self.monitor().poll()
        self.assertEqual(2, len(self.sends))
        self.assertEqual(1, result["counts"]["sent"])

    def test_permanent_failure_stops_after_three_attempts_but_reconciles(self):
        self.sender_impl = lambda *args, **kwargs: False
        for _ in range(delivery.MAX_SEND_ATTEMPTS + 2):
            result = self.monitor().poll()
            self.now += delivery.RETRY_SECONDS + 1
        self.assertEqual(delivery.MAX_SEND_ATTEMPTS, len(self.sends))
        self.assertIn("send_attempts_exhausted", result["errors"])
        self.assertEqual("review", self.state()["events"][event()["event_id"]]["state"])
        self.insert_message(self.sends[-1][1])
        result = self.monitor().poll()
        self.assertEqual(1, result["counts"]["sent"])
        self.assertEqual(delivery.MAX_SEND_ATTEMPTS, len(self.sends))

    def test_pending_row_suppresses_retry_then_reconciles_sent(self):
        def pending(recipient, text, **kwargs):
            self.insert_message(text, sent=0)
            return False
        self.sender_impl = pending
        self.monitor().poll()
        self.now += 86400
        self.monitor().poll()
        self.assertEqual(1, len(self.sends))
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE message SET is_sent=1")
        result = self.monitor().poll()
        self.assertEqual(1, result["counts"]["sent"])
        self.assertEqual(1, len(self.sends))

    def test_unchanged_pending_does_not_publish_every_poll(self):
        def pending(recipient, text, **kwargs):
            self.insert_message(text, sent=0)
            return False
        self.sender_impl = pending
        monitor = self.monitor()
        monitor.poll()
        self.now += delivery.POLL_SECONDS
        monitor.poll()
        self.assertEqual(1, len(self.bridge.statuses))

    def test_crash_after_send_is_reconciled_on_restart(self):
        original_save = delivery._save_state
        calls = []
        def fail_after_send(path, state):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("simulated crash")
            original_save(path, state)
        with patch.object(delivery, "_save_state", fail_after_send):
            self.monitor().poll()
        self.assertEqual("attempting", self.state()["events"][event()["event_id"]]["state"])
        self.monitor().poll()
        self.assertEqual(1, len(self.sends))
        self.assertEqual("sent", self.state()["events"][event()["event_id"]]["state"])

    def test_exact_body_owner_direction_and_send_metadata(self):
        text = delivery.alert_text(event())
        self.insert_message(text + " not identical")
        self.insert_message(text, chat=2)
        self.insert_message(text, from_me=0)
        self.assertEqual("absent", delivery.messages_status(self.db_path, OWNER, text, normalize)[0])
        self.insert_message(text, sent=1, error=1)
        self.assertEqual("failed", delivery.messages_status(self.db_path, OWNER, text, normalize)[0])
        self.insert_message(text, sent=0)
        self.assertEqual("pending", delivery.messages_status(self.db_path, OWNER, text, normalize)[0])
        self.insert_message(text, chat=3)
        self.assertEqual("sent", delivery.messages_status(self.db_path, OWNER, text, normalize)[0])

    def test_attributed_body_lookup_after_attempt(self):
        text = delivery.alert_text(event())
        self.insert_message(None, attributed=b"archive")
        with patch.object(delivery, "decode_attributed_body", return_value=text):
            self.assertEqual("sent", delivery.messages_status(self.db_path, OWNER, text, normalize, 0)[0])
        with patch.object(delivery, "decode_attributed_body", return_value=None):
            self.assertEqual("unknown", delivery.messages_status(self.db_path, OWNER, text, normalize, 0)[0])

    def test_messages_status_closes_read_connection_on_return_and_error(self):
        real_connect = sqlite3.connect
        connections = []

        def track_connection(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connections.append(connection)
            return connection

        def invalid_handle(_handle):
            raise ValueError("synthetic invalid handle")

        for owner, normalizer in ((OWNER, normalize), ("+15559999999", normalize), (OWNER, invalid_handle)):
            with self.subTest(owner=owner, invalid=normalizer is invalid_handle):
                with patch.object(delivery.sqlite3, "connect", side_effect=track_connection):
                    if normalizer is invalid_handle:
                        with self.assertRaisesRegex(delivery.DeliveryError, "messages_db_unreadable"):
                            delivery.messages_status(self.db_path, owner, "absent", normalizer)
                    else:
                        self.assertEqual("absent", delivery.messages_status(self.db_path, owner, "absent", normalizer)[0])
                with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                    connections[-1].execute("SELECT 1")

    @unittest.skipIf(sys.platform == "win32", "Real flock requires POSIX; monitor behavior is tested separately")
    def test_concurrent_monitor_lock_does_not_send_or_overwrite_heartbeat(self):
        with delivery._state_lock(self.root / ".package_delivery"):
            result = self.monitor().poll()
        self.assertIn("monitor_busy", result["errors"])
        self.assertEqual([], self.sends)
        self.assertEqual([], self.bridge.statuses)

    def test_heartbeat_initial_privacy_and_interval(self):
        self.bridge.data["events"] = []
        monitor = self.monitor()
        monitor.poll()
        monitor.poll()
        self.assertEqual(1, len(self.bridge.statuses))
        self.now += delivery.HEARTBEAT_SECONDS + 1
        monitor.poll()
        self.assertEqual(2, len(self.bridge.statuses))
        self.bridge.data["events"] = [event()]
        monitor.poll()
        status = self.bridge.statuses[-1]
        self.assertEqual({"schema_version", "runtime_revision", "checked_at", "last_successful_poll", "state", "errors", "counts", "events"}, set(status))
        serialized = json.dumps(status)
        for sensitive in (OWNER, "Example Store", "Test clubs", "front desk", "shipping@example.com", "1234567890", "abcdef123"):
            self.assertNotIn(sensitive, serialized)
        self.assertEqual({"event_id", "state", "sent_at"}, set(status["events"][0]))

    def test_source_stale_visible_without_inventing_delivery(self):
        self.bridge.data["events"] = []
        self.bridge.data["last_mail_scan_at"] = delivery._iso(NOW - 10801)
        self.assertEqual("source_stale", self.monitor().poll()["state"])
        self.assertEqual([], self.sends)


class GitHubTests(unittest.TestCase):
    def test_private_repository_gate(self):
        bridge = delivery.GitHubBridge(".")
        with patch.object(bridge, "_call", return_value=json.dumps({"private": False, "full_name": delivery.REPOSITORY}).encode()) as call:
            with self.assertRaisesRegex(delivery.DeliveryError, "repository_not_private"):
                bridge.read_inbox()
        self.assertEqual(1, call.call_count)

    def test_status_put_is_fixed_path_branch_and_optimistic_sha(self):
        bridge = delivery.GitHubBridge(".")
        replies = [json.dumps({"private": True, "full_name": delivery.REPOSITORY}).encode(),
                   json.dumps({"sha": "b" * 40}).encode(), b"{}"]
        with patch.object(bridge, "_call", side_effect=replies) as call:
            bridge.publish_status({"schema_version": 1})
        args, payload = call.call_args.args
        self.assertEqual([f"repos/{delivery.REPOSITORY}/contents/{delivery.STATUS_PATH}", "--method", "PUT"], args)
        self.assertEqual(delivery.DATA_BRANCH, payload["branch"])
        self.assertEqual("b" * 40, payload["sha"])
        self.assertEqual({"schema_version": 1}, json.loads(base64.b64decode(payload["content"])))

    @unittest.skipIf(sys.platform == "win32", "Real shebang execution and selector pipes require POSIX")
    def test_subprocess_has_no_shell_and_bounds_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fake-gh"
            fake.write_text(f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
            fake.chmod(0o700)
            bridge = delivery.GitHubBridge(root)
            bridge.executable = str(fake)
            dangerous = "x; touch SHOULD_NOT_EXIST $(touch ALSO_NOT_EXISTS)"
            result = json.loads(bridge._call([dangerous]))
            self.assertEqual(["api", "--hostname", "github.com", dangerous], result)
            self.assertFalse((root / "SHOULD_NOT_EXIST").exists())
            fake.write_text(f"#!{sys.executable}\nprint('x' * {delivery.MAX_OUTPUT_BYTES + 1})\n")
            with self.assertRaisesRegex(delivery.DeliveryError, "gh_output_too_large"):
                bridge._call([])

    def test_non_mac_start_does_not_import_runtime(self):
        with patch.object(delivery.sys, "platform", "linux"):
            self.assertIsNone(delivery.start_package_delivery_monitor())


if __name__ == "__main__":
    unittest.main()
