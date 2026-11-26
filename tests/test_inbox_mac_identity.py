from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

from davosbot.inbox import InboxSourceError, MessageInbox, inbox_health
from inbox_fixtures import NOW, SourceFixture


class MacSourceIdentityTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.fixture = SourceFixture(root)
        self.consumer = MessageInbox(self.fixture.path, self.fixture.bot_path, now=lambda: NOW)
        self.volume = UUID(int=100)
        self.device = 16777233
        self.inode = 317547
        self.birth = 1776994688.25
        real_stat = Path.stat

        def source_stat(path, *args, **kwargs):
            if path != self.consumer.source_path:
                return real_stat(path, *args, **kwargs)
            return SimpleNamespace(st_dev=self.device, st_ino=self.inode, st_birthtime=self.birth)

        self.enterContext(patch.object(Path, "stat", source_stat))
        self.enterContext(patch("davosbot.inbox.volume_uuid", side_effect=lambda _: self.volume))

    def checkpoint(self):
        with closing(sqlite3.connect(self.fixture.bot_path)) as db:
            return db.execute("SELECT source_identity,cursor_rowid,anchor_guid FROM inbound_source").fetchone()

    def bind(self):
        self.fixture.add_message(1, guid="baseline")
        self.assertEqual(0, self.consumer.poll())
        self.assertEqual(0, self.consumer.poll())

    def test_identical_apfs_source_remount_admits_only_new_ids(self):
        self.bind()
        self.fixture.add_message(2, guid="already-attempted")
        self.consumer.poll()
        self.assertEqual(2, self.consumer.claim_next()["ROWID"])
        old_binding = self.checkpoint()
        self.device = 16777231
        # Claim or finish may not migrate a source identity; only poll can.
        with self.assertRaisesRegex(InboxSourceError, "source_identity_changed"):
            self.consumer.claim_next()
        self.assertEqual(old_binding, self.checkpoint())
        self.consumer = MessageInbox(self.fixture.path, self.fixture.bot_path, now=lambda: NOW)
        self.fixture.add_message(3, guid="fresh")
        self.assertEqual(1, self.consumer.poll())
        self.assertNotEqual(old_binding[0], self.checkpoint()[0])
        seen = []
        self.assertEqual(1, self.consumer.dispatch_ready(lambda row: seen.append(row["ROWID"])))
        self.assertEqual([3], seen)
        self.assertEqual({"uncertain": 1, "handler_returned": 1}, inbox_health(self.fixture.bot_path)["counts"])

    def test_remount_does_not_release_old_pending_confirmation(self):
        self.bind()
        self.fixture.add_message(2, "yes", guid="old-confirmation")
        self.consumer.poll()
        self.device -= 2
        self.consumer = MessageInbox(
            self.fixture.path, self.fixture.bot_path, now=lambda: NOW,
            confirmation_guard=lambda *args: True,
        )
        self.assertEqual(0, self.consumer.poll())
        self.assertIsNone(self.consumer.claim_next())
        self.assertEqual({"held": 1}, inbox_health(self.fixture.bot_path)["counts"])

    def expect_hold(self):
        self.fixture.add_message(2, guid="new-not-allowed")
        old = self.checkpoint()
        with self.assertRaisesRegex(InboxSourceError, "source_identity_changed"):
            self.consumer.poll()
        self.assertEqual(old, self.checkpoint())
        self.assertEqual({}, inbox_health(self.fixture.bot_path)["counts"])

    def test_different_volume_with_matching_inode_birth_and_anchor_holds(self):
        self.bind()
        self.device -= 2
        self.volume = UUID(int=101)
        self.expect_hold()

    def test_different_inode_with_matching_birth_and_anchor_holds(self):
        self.bind()
        self.device -= 2
        self.inode += 1
        self.expect_hold()

    def test_different_birth_with_matching_volume_inode_and_anchor_holds(self):
        self.bind()
        self.device -= 2
        self.birth += 1
        self.expect_hold()

    def test_missing_stable_uuid_holds(self):
        self.bind()
        self.device -= 2
        self.volume = None
        self.expect_hold()

    def test_missing_anchor_holds_when_device_changed(self):
        self.bind()
        self.device -= 2
        with self.fixture.connect() as db:
            db.execute("DELETE FROM message WHERE ROWID=1")
        self.expect_hold()

    def test_conflicting_anchor_holds_when_device_changed(self):
        self.bind()
        self.device -= 2
        with self.fixture.connect() as db:
            db.execute("UPDATE message SET guid='wrong' WHERE ROWID=1")
        self.expect_hold()

    def test_unenrolled_legacy_checkpoint_never_learns_from_mismatch(self):
        self.fixture.add_message(1, guid="baseline")
        self.consumer.poll()  # no post-initialization poll to enroll it
        self.device -= 2
        self.expect_hold()

    def test_admission_failure_rolls_back_identity_rebinding(self):
        self.bind()
        self.fixture.add_message(2, guid="seen")
        self.consumer.poll()
        self.assertEqual(1, self.consumer.dispatch_ready(lambda _: None))
        old = self.checkpoint()
        self.device -= 2
        self.fixture.add_message(3, guid="seen")
        with self.assertRaisesRegex(InboxSourceError, "duplicate_message_identity"):
            self.consumer.poll()
        self.assertEqual(old, self.checkpoint())
        self.assertEqual({"handler_returned": 1}, inbox_health(self.fixture.bot_path)["counts"])

    def test_session_guard_still_wins(self):
        self.bind()
        self.device -= 2
        old = self.checkpoint()
        with closing(sqlite3.connect(self.fixture.bot_path)) as db, db:
            db.execute("UPDATE inbound_source SET session_error='untracked_runtime_session'")
        with self.assertRaisesRegex(InboxSourceError, "untracked_runtime_session"):
            self.consumer.poll()
        self.assertEqual(old, self.checkpoint())
