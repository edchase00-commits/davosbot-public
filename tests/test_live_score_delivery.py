"""Offline native-delivery fixtures; never invokes Messages or AppleScript."""

from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from davosbot import live_score_delivery as delivery


OWNER = "+15551234567"
FRIEND_ONE = "+15557654321"
FRIEND_TWO = "+15559876543"
PARTICIPANTS = [OWNER, FRIEND_ONE, FRIEND_TWO]
NOW = 1_795_200_000.0


def config(mode="auto_group", *, participants=None, destinations=None, group_guid=None):
    participants = list(participants or PARTICIPANTS)
    route = {"mode": mode}
    if mode == "direct":
        route["destinations"] = list(destinations or participants)
    else:
        route["group_guid"] = group_guid
    return {
        "schema_version": 1,
        "expected_count": len(participants),
        "participants": participants,
        "delivery": route,
    }


class DeliveryFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "external-state"
        self.config_path = self.root / "config.json"
        self.db_path = Path(self.temporary.name) / "chat.db"
        self.sends = []
        self.now = NOW
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.executescript(
                """
                CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
                CREATE TABLE chat (
                    ROWID INTEGER PRIMARY KEY,
                    guid TEXT,
                    chat_identifier TEXT
                );
                CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
                CREATE TABLE message (
                    ROWID INTEGER PRIMARY KEY,
                    text TEXT,
                    attributedBody BLOB,
                    is_from_me INTEGER,
                    is_sent INTEGER,
                    error INTEGER
                );
                CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
                """
            )

    def install_config(self, payload=None):
        return delivery.write_delivery_config(self.config_path, payload or config())

    def add_chat(self, rowid, participants, *, identifier=None, guid=None):
        identifier = identifier or f"chat{rowid:032x}"
        guid = guid or f"iMessage;+;{identifier}"
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "INSERT INTO chat (ROWID, guid, chat_identifier) VALUES (?, ?, ?)",
                (rowid, guid, identifier),
            )
            for handle in participants:
                handle_id = db.execute("INSERT INTO handle (id) VALUES (?)", (handle,)).lastrowid
                db.execute(
                    "INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (?, ?)",
                    (rowid, handle_id),
                )
        return guid, identifier

    def add_message(
        self,
        chat_id,
        text,
        *,
        rowid=None,
        from_me=1,
        sent=1,
        error=0,
        attributed=None,
    ):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            if rowid is None:
                rowid = db.execute(
                    """
                    INSERT INTO message
                        (text, attributedBody, is_from_me, is_sent, error)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (text, attributed, from_me, sent, error),
                ).lastrowid
            else:
                db.execute(
                    """
                    INSERT INTO message
                        (ROWID, text, attributedBody, is_from_me, is_sent, error)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (rowid, text, attributed, from_me, sent, error),
                )
            db.execute(
                "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
                (chat_id, rowid),
            )
        return int(rowid)

    def monitor(self, sender=None):
        if sender is None:
            sender = self.record_only_sender
        return delivery.LiveScoreDelivery(
            self.root,
            self.db_path,
            config_path=self.config_path,
            sender=sender,
            clock=lambda: self.now,
        )

    def record_only_sender(self, recipient, text, **kwargs):
        self.sends.append((recipient, text, kwargs))
        return True


class ConfigValidationTests(DeliveryFixture):
    def test_private_external_config_modes_and_round_trip(self):
        parsed = self.install_config()
        self.assertEqual("auto_group", parsed.mode)
        self.assertEqual(tuple(PARTICIPANTS), parsed.participants)
        self.assertEqual(0o700, stat.S_IMODE(self.root.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(self.config_path.stat().st_mode))
        self.assertEqual(parsed, delivery.load_delivery_config(self.config_path))

    def test_expected_count_duplicates_and_route_mismatch_rejected(self):
        cases = []
        wrong_count = config()
        wrong_count["expected_count"] = 2
        cases.append(wrong_count)
        duplicate = config(participants=[OWNER, OWNER, FRIEND_TWO])
        cases.append(duplicate)
        normalized_duplicate = config(participants=[OWNER, "(555) 765-4321", FRIEND_ONE])
        cases.append(normalized_duplicate)
        mismatch = config(mode="direct", destinations=[OWNER, FRIEND_ONE, "+15550000000"])
        cases.append(mismatch)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(delivery.DeliveryError):
                delivery.validate_delivery_config(payload)

    def test_unknown_fields_group_shapes_and_invalid_destinations_rejected(self):
        extra = config()
        extra["command"] = "send"
        with self.assertRaisesRegex(delivery.DeliveryError, "invalid_config"):
            delivery.validate_delivery_config(extra)
        with self.assertRaisesRegex(delivery.DeliveryError, "invalid_config"):
            delivery.validate_delivery_config(config(mode="group", group_guid=None))
        with self.assertRaisesRegex(delivery.DeliveryError, "invalid_config"):
            delivery.validate_delivery_config(config(participants=[OWNER, FRIEND_ONE, "not-a-handle"]))

    def test_duplicate_keys_nonfinite_oversize_and_public_file_rejected(self):
        self.root.mkdir(mode=0o700)
        for raw in (
            b'{"schema_version":1,"schema_version":1}',
            b'{"schema_version":NaN}',
        ):
            self.config_path.write_bytes(raw)
            self.config_path.chmod(0o600)
            with self.assertRaises(delivery.DeliveryError):
                delivery.load_delivery_config(self.config_path)
        self.config_path.write_bytes(b" " * (delivery.MAX_CONFIG_BYTES + 1))
        self.config_path.chmod(0o600)
        with self.assertRaises(delivery.DeliveryError):
            delivery.load_delivery_config(self.config_path)
        self.config_path.write_text(json.dumps(config()))
        self.config_path.chmod(0o644)
        with self.assertRaisesRegex(delivery.DeliveryError, "invalid_config_file"):
            delivery.load_delivery_config(self.config_path)

    def test_relative_state_directory_is_rejected(self):
        with self.assertRaisesRegex(delivery.DeliveryError, "external_state_dir_required"):
            delivery.LiveScoreDelivery("relative", self.db_path, sender=self.record_only_sender)


class DiscoveryTests(DeliveryFixture):
    def test_exact_normalized_participant_set_only_and_newest_match(self):
        # Subset and superset chats must never be eligible.
        self.add_chat(1, PARTICIPANTS[:2])
        self.add_chat(2, PARTICIPANTS + ["+15550000000"])
        _, old_identifier = self.add_chat(
            3,
            ["+1 (555) 123-4567", "(555) 765-4321", "555-987-6543"],
        )
        newest_guid, newest_identifier = self.add_chat(4, list(reversed(PARTICIPANTS)))
        self.add_message(3, "older", rowid=10)
        self.add_message(4, "newer", rowid=20)
        route = delivery.discover_exact_group(self.db_path, PARTICIPANTS)
        self.assertIsNotNone(route)
        self.assertEqual(newest_guid, route.group_guid)
        self.assertEqual(newest_identifier, route.send_recipient)
        self.assertNotEqual(old_identifier, route.send_recipient)

    def test_new_empty_exact_chat_wins_empty_tie(self):
        self.add_chat(10, PARTICIPANTS)
        newest_guid, _ = self.add_chat(11, PARTICIPANTS)
        self.assertEqual(
            newest_guid,
            delivery.discover_exact_group(self.db_path, PARTICIPANTS).group_guid,
        )

    def test_bound_group_is_revalidated_against_exact_participants(self):
        guid, _ = self.add_chat(1, PARTICIPANTS + ["+15550000000"])
        self.install_config(config(mode="group", group_guid=guid))
        with self.monitor() as monitor:
            status = monitor.route_status()
            self.assertFalse(status.ready)
            self.assertEqual("no_exact_group", status.reason)
            receipt = monitor.deliver("game.start", "Live now")
        self.assertEqual("waiting_route", receipt[0].state)
        self.assertEqual("no_route", receipt[0].outcome)
        self.assertEqual([], self.sends)


class GroupDeliveryTests(DeliveryFixture):
    def test_auto_group_never_switches_while_bound_group_remains_exact(self):
        old_guid, old_identifier = self.add_chat(1, PARTICIPANTS)
        self.install_config()

        def successful_old(recipient, text, **kwargs):
            self.sends.append((recipient, text, kwargs))
            self.add_message(1, text)
            return True

        with self.monitor(sender=successful_old) as monitor:
            first = monitor.deliver("initial.live", "Q1 12:00 | Initial")[0]
            self.assertTrue(first.confirmed)
            self.assertEqual(old_guid, monitor.bound_group_guid)

        self.add_chat(2, PARTICIPANTS)
        with self.monitor(sender=successful_old) as monitor:
            status = monitor.route_status()
            self.assertEqual(old_guid, status.group_guid)
            second = monitor.deliver("next.live", "Q1 2:00 | Next")[0]
            self.assertTrue(second.confirmed)
            self.assertEqual(old_guid, monitor.bound_group_guid)

        self.assertEqual(old_identifier, self.sends[-1][0])

    def test_auto_group_rebinds_only_after_bound_group_loses_exact_participants(self):
        old_guid, _ = self.add_chat(1, PARTICIPANTS)
        self.install_config()

        with self.monitor() as monitor:
            monitor.deliver("initial.live", "Q1 12:00 | Initial")
            self.assertEqual(old_guid, monitor.bound_group_guid)

        # Simulate one configured participant leaving the original thread.
        with closing(sqlite3.connect(self.db_path)) as db, db:
            leaving_handle = db.execute(
                "SELECT ROWID FROM handle WHERE id = ? ORDER BY ROWID LIMIT 1",
                (FRIEND_TWO,),
            ).fetchone()[0]
            db.execute(
                "DELETE FROM chat_handle_join WHERE chat_id = ? AND handle_id = ?",
                (1, leaving_handle),
            )
        new_guid, new_identifier = self.add_chat(2, PARTICIPANTS)

        def successful(recipient, text, **kwargs):
            self.sends.append((recipient, text, kwargs))
            self.add_message(2, text)
            return True

        with self.monitor(sender=successful) as monitor:
            status = monitor.route_status()
            self.assertTrue(status.ready)
            self.assertEqual(new_guid, status.group_guid)
            receipt = monitor.deliver("replacement.live", "Q2 10:00 | Replacement")[0]
            self.assertTrue(receipt.confirmed)
            self.assertEqual(new_guid, monitor.bound_group_guid)

        self.assertEqual(new_identifier, self.sends[-1][0])

    def test_auto_group_rebind_rejects_subset_and_superset_replacements(self):
        self.add_chat(1, PARTICIPANTS)
        self.install_config()
        with self.monitor() as monitor:
            monitor.deliver("initial.live", "Q1 12:00 | Initial")

        with closing(sqlite3.connect(self.db_path)) as db, db:
            leaving_handle = db.execute(
                "SELECT ROWID FROM handle WHERE id = ? ORDER BY ROWID LIMIT 1",
                (FRIEND_TWO,),
            ).fetchone()[0]
            db.execute(
                "DELETE FROM chat_handle_join WHERE chat_id = ? AND handle_id = ?",
                (1, leaving_handle),
            )
        self.add_chat(2, PARTICIPANTS[:2])
        self.add_chat(3, PARTICIPANTS + ["+15550000000"])

        with self.monitor() as monitor:
            status = monitor.route_status()
            self.assertFalse(status.ready)
            self.assertEqual("no_exact_group", status.reason)
            receipt = monitor.deliver("replacement.live", "Q2 10:00 | Replacement")[0]
            self.assertEqual("waiting_route", receipt.state)

        self.assertEqual(1, len(self.sends))

    def test_auto_group_waits_without_direct_fallback_then_bootstraps_exact_receipt(self):
        self.install_config()
        # Establish a global watermark greater than any old synthetic receipt.
        direct_guid, _ = self.add_chat(1, [OWNER])
        self.assertTrue(direct_guid)
        self.add_message(1, "unrelated", rowid=50)
        with self.monitor() as monitor:
            self.assertFalse(monitor.route_status().ready)
            first = monitor.deliver("raptors.start", "Q1 12:00 | Toronto 0, Detroit 0")[0]
            self.assertEqual("waiting_route", first.state)
            self.assertEqual("no_route", first.outcome)
            self.assertEqual(50, first.watermark)
            self.assertEqual([], self.sends)

            guid, identifier = self.add_chat(2, PARTICIPANTS)
            # An old identical row cannot satisfy the saved watermark.
            self.add_message(2, first.text, rowid=40)
            waiting = monitor.reconcile("raptors.start")[0]
            self.assertEqual("waiting_route", waiting.state)
            self.assertEqual("waiting_bootstrap_receipt", waiting.outcome)
            self.assertEqual(guid, monitor.bound_group_guid)

            receipt_rowid = self.add_message(2, first.text, rowid=51)
            sent = monitor.reconcile("raptors.start")[0]
            self.assertTrue(sent.confirmed)
            self.assertEqual(receipt_rowid, sent.receipt_rowid)
            self.assertEqual(identifier, monitor.route_status().destinations[0])

        # The persisted binding is used for future alerts after restart.
        def group_sender(recipient, text, **kwargs):
            self.sends.append((recipient, text, kwargs))
            self.add_message(2, text)
            return True

        with self.monitor(sender=group_sender) as restarted:
            second = restarted.deliver("raptors.update.1", "Q1 8:00 | Toronto 12, Detroit 10")[0]
            self.assertTrue(second.confirmed)
        self.assertEqual(identifier, self.sends[0][0])
        self.assertEqual(
            {"is_group": True, "recovery_mode": "none"},
            self.sends[0][2],
        )

    def test_existing_exact_group_sends_once_and_restart_deduplicates(self):
        guid, identifier = self.add_chat(1, PARTICIPANTS)
        self.install_config(config(mode="group", group_guid=guid))

        def successful(recipient, text, **kwargs):
            self.sends.append((recipient, text, kwargs))
            self.add_message(1, text)
            return True

        with self.monitor(sender=successful) as monitor:
            receipt = monitor.deliver("pacers.start", "Q1 12:00 | Indiana 0, Cleveland 0")[0]
            self.assertTrue(receipt.confirmed)
            self.assertIsInstance(receipt.receipt_rowid, int)
        with self.monitor(sender=successful) as restarted:
            duplicate = restarted.deliver("pacers.start", "Q1 12:00 | Indiana 0, Cleveland 0")[0]
            self.assertTrue(duplicate.confirmed)
        self.assertEqual(1, len(self.sends))
        self.assertEqual(identifier, self.sends[0][0])

    def test_success_without_receipt_is_unknown_and_never_blind_retried(self):
        guid, _ = self.add_chat(1, PARTICIPANTS)
        self.install_config(config(mode="group", group_guid=guid))
        with self.monitor() as monitor:
            first = monitor.deliver("clemson.start", "Q1 15:00 | Clemson 0, Duke 0")[0]
            self.assertEqual("unknown", first.state)
            again = monitor.deliver("clemson.start", first.text)[0]
            self.assertEqual("unknown", again.state)
        self.assertEqual(1, len(self.sends))

        rowid = self.add_message(1, first.text)
        with self.monitor() as restarted:
            reconciled = restarted.reconcile("clemson.start")[0]
        self.assertTrue(reconciled.confirmed)
        self.assertEqual(rowid, reconciled.receipt_rowid)
        self.assertEqual(1, len(self.sends))

    def test_process_exit_after_attempt_checkpoint_reconciles_without_retry(self):
        guid, _ = self.add_chat(1, PARTICIPANTS)
        self.install_config(config(mode="group", group_guid=guid))

        def interrupted(_recipient, _text, **_kwargs):
            raise SystemExit("synthetic process exit")

        with self.monitor(sender=interrupted) as monitor:
            with self.assertRaises(SystemExit):
                monitor.deliver("crash.window", "Q3 2:00 | Toronto 70, Detroit 72")
        persisted = json.loads((self.root / "state.json").read_text())
        self.assertEqual(
            "attempting",
            persisted["alerts"]["crash.window"]["targets"]["group"]["state"],
        )

        with self.monitor() as restarted:
            receipt = restarted.deliver(
                "crash.window",
                "Q3 2:00 | Toronto 70, Detroit 72",
            )[0]
        self.assertEqual("unknown", receipt.state)
        self.assertEqual([], self.sends)

    def test_pending_failed_wrong_chat_and_inbound_rows_are_exactly_scoped(self):
        guid, _ = self.add_chat(1, PARTICIPANTS)
        self.add_chat(2, PARTICIPANTS[:2])
        self.install_config(config(mode="group", group_guid=guid))

        def pending_sender(_recipient, text, **_kwargs):
            self.add_message(2, text)  # Wrong chat.
            self.add_message(1, text, from_me=0)  # Wrong direction.
            self.add_message(1, text + " changed")  # Wrong body.
            self.add_message(1, text, sent=0, error=0)
            return False

        with self.monitor(sender=pending_sender) as monitor:
            receipt = monitor.deliver("unc.start", "1H 20:00 | Marshall 10, UNC 12")[0]
            self.assertEqual("pending", receipt.state)
            with closing(sqlite3.connect(self.db_path)) as db, db:
                db.execute(
                    "UPDATE message SET is_sent=1, error=0 WHERE ROWID=?",
                    (receipt.receipt_rowid,),
                )
            final = monitor.reconcile("unc.start")[0]
            self.assertTrue(final.confirmed)

        # A separately recorded Messages error is a failed receipt and also
        # remains one-shot in this layer.
        failed_calls = []

        def failed_sender(_recipient, text, **_kwargs):
            failed_calls.append(text)
            self.add_message(1, text, sent=0, error=1)
            return False

        with self.monitor(sender=failed_sender) as monitor:
            failed = monitor.deliver("wyoming.start", "Q1 15:00 | New Mexico 0, Wyoming 0")[0]
            self.assertEqual("failed", failed.state)
            monitor.deliver("wyoming.start", failed.text)
        self.assertEqual([failed.text], failed_calls)

    def test_failed_exact_row_later_reconciles_sent_without_second_native_call(self):
        guid, _ = self.add_chat(1, PARTICIPANTS)
        self.install_config(config(mode="group", group_guid=guid))
        native_calls = []

        def failed_sender(_recipient, text, **_kwargs):
            native_calls.append(text)
            self.add_message(1, text, sent=0, error=1)
            return False

        text = "Final | Wyoming 21, New Mexico 24 | New Mexico -9: lost."
        with self.monitor(sender=failed_sender) as monitor:
            failed = monitor.deliver("wyoming.final", text)[0]
            self.assertEqual("failed", failed.state)
            failed_rowid = failed.receipt_rowid

            sent_rowid = self.add_message(1, text, sent=1, error=0)
            recovered = monitor.deliver("wyoming.final", text)[0]

        self.assertEqual([text], native_calls)
        self.assertTrue(recovered.confirmed)
        self.assertEqual(sent_rowid, recovered.receipt_rowid)
        self.assertGreater(recovered.receipt_rowid, failed_rowid)

    def test_attributed_body_exact_match_exposes_receipt_rowid(self):
        guid, _ = self.add_chat(1, PARTICIPANTS)
        self.install_config(config(mode="group", group_guid=guid))
        body = "Q2 4:00 | Toronto 44, Detroit 40"

        def attributed_sender(_recipient, _text, **_kwargs):
            self.add_message(1, None, attributed=b"synthetic archive")
            return True

        with patch.object(delivery, "decode_attributed_body", return_value=body):
            with self.monitor(sender=attributed_sender) as monitor:
                receipt = monitor.deliver("raptors.attr", body)[0]
        self.assertTrue(receipt.confirmed)
        self.assertIsInstance(receipt.receipt_rowid, int)

    def test_alert_text_is_immutable(self):
        guid, _ = self.add_chat(1, PARTICIPANTS)
        self.install_config(config(mode="group", group_guid=guid))
        with self.monitor() as monitor:
            monitor.deliver("immutable.alert", "first body")
            with self.assertRaisesRegex(delivery.DeliveryError, "immutable_alert_changed"):
                monitor.deliver("immutable.alert", "different body")


class DirectAndStateTests(DeliveryFixture):
    def add_direct_chats(self):
        for index, destination in enumerate(PARTICIPANTS, start=1):
            self.add_chat(index, [destination], identifier=destination, guid=f"iMessage;-;{destination}")

    def test_direct_mode_fans_out_only_when_explicit_and_receipts_are_per_destination(self):
        self.add_direct_chats()
        self.install_config(config(mode="direct"))
        chat_by_destination = {value: index for index, value in enumerate(PARTICIPANTS, start=1)}

        def successful(recipient, text, **kwargs):
            self.sends.append((recipient, text, kwargs))
            self.add_message(chat_by_destination[recipient], text)
            return True

        with self.monitor(sender=successful) as monitor:
            self.assertEqual(tuple(PARTICIPANTS), monitor.route_status().destinations)
            receipts = monitor.deliver("direct.start", "Game live")
            self.assertEqual(3, len(receipts))
            self.assertTrue(all(receipt.confirmed for receipt in receipts))
            self.assertEqual(3, len({receipt.receipt_rowid for receipt in receipts}))
        self.assertEqual(PARTICIPANTS, [call[0] for call in self.sends])
        self.assertTrue(all(call[2] == {"is_group": False, "recovery_mode": "none"} for call in self.sends))

    @unittest.skipIf(os.name == "nt", "fcntl is a POSIX runtime primitive")
    def test_lifetime_single_process_lock(self):
        self.install_config()
        first = self.monitor()
        self.addCleanup(first.close)
        with self.assertRaisesRegex(delivery.DeliveryError, "monitor_busy"):
            self.monitor()
        first.close()
        second = self.monitor()
        second.close()

    def test_state_is_private_strict_and_config_change_fails_closed(self):
        self.install_config()
        with self.monitor() as monitor:
            monitor.deliver("waiting.alert", "Waiting")
        state_path = self.root / "state.json"
        self.assertEqual(0o600, stat.S_IMODE(state_path.stat().st_mode))
        parsed = json.loads(state_path.read_text())
        parsed["unexpected"] = True
        state_path.write_text(json.dumps(parsed))
        state_path.chmod(0o600)
        with self.assertRaisesRegex(delivery.DeliveryError, "state_corrupt"):
            self.monitor()

        parsed.pop("unexpected")
        state_path.write_text(json.dumps(parsed))
        state_path.chmod(0o600)
        delivery.write_delivery_config(
            self.config_path,
            config(participants=[OWNER, FRIEND_ONE, "+15550000000"]),
        )
        with self.assertRaisesRegex(delivery.DeliveryError, "config_changed"):
            self.monitor()

    def test_snapshot_is_copy_and_closed_monitor_rejects_operations(self):
        self.install_config()
        monitor = self.monitor()
        snapshot = monitor.snapshot()
        snapshot["alerts"]["injected"] = {}
        self.assertNotIn("injected", monitor.snapshot()["alerts"])
        monitor.close()
        with self.assertRaisesRegex(delivery.DeliveryError, "monitor_closed"):
            monitor.route_status()


if __name__ == "__main__":
    unittest.main()
