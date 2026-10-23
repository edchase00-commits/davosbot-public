"""Origin, current authorization, and no-replay boundaries for durable intake."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from davosbot.inbox import InboxSourceError, MessageInbox, inbox_health
from inbox_fixtures import SourceFixture, NOW, OWNER, FRIEND, GROUP


class InboxPermissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = SourceFixture(temporary.name)
        self.inbox = self.restart()
        self.inbox.poll()

    def restart(self):
        return MessageInbox(self.source.path, self.source.bot_path, now=lambda: NOW)

    def assert_held(self, rowid, reason):
        with closing(sqlite3.connect(self.source.bot_path)) as conn:
            self.assertEqual(("held", reason), conn.execute("SELECT state,reason FROM inbound_messages WHERE source_rowid=?", (rowid,)).fetchone())

    def test_outbound_source_is_never_dispatched(self):
        self.source.add_message(1, "grant someone", from_me=True)
        self.inbox.poll()
        self.assertIsNone(self.inbox.claim_next())

    def test_ambiguous_origin_cannot_choose_a_destination(self):
        self.source.add_message(1, "private send request")
        self.source.add_join(1, 3)
        self.inbox.poll()
        self.assertIsNone(self.inbox.claim_next())
        self.assert_held(1, "ambiguous_origin")

    def test_sender_change_after_binding_cannot_inherit_owner_authority(self):
        self.source.add_message(1, "grant someone", sender_id=2, chat_id=2, attachment=True)
        self.inbox.poll()
        self.assertIsNone(self.inbox.claim_next())
        with self.source.connect() as conn:
            conn.execute("UPDATE message SET handle_id=1 WHERE ROWID=1")
        self.source.add_image(1)
        self.assertIsNone(self.restart().claim_next())
        self.assert_held(1, "origin_changed")

    def test_sender_change_between_intake_and_first_claim_is_rejected(self):
        self.source.add_message(1, "grant someone", sender_id=2, chat_id=2)
        self.inbox.poll()
        with self.source.connect() as conn:
            conn.execute("UPDATE message SET handle_id=1 WHERE ROWID=1")
        self.assertIsNone(self.restart().claim_next())
        self.assert_held(1, "origin_changed")

    def test_sender_handle_record_cannot_change_between_intake_and_claim(self):
        self.source.add_message(1, "grant someone", sender_id=2, chat_id=2)
        self.inbox.poll()
        with self.source.connect() as conn:
            conn.execute("UPDATE handle SET id=? WHERE ROWID=2", (OWNER,))
        self.assertIsNone(self.restart().claim_next())
        self.assert_held(1, "origin_changed")

    def test_chat_change_after_binding_cannot_redirect_a_reply(self):
        self.source.add_message(1, "caption", chat_id=3, attachment=True)
        self.inbox.poll()
        self.assertIsNone(self.inbox.claim_next())
        with self.source.connect() as conn:
            conn.execute("UPDATE chat_message_join SET chat_id=1 WHERE message_id=1")
        self.source.add_image(1)
        self.assertIsNone(self.restart().claim_next())
        self.assert_held(1, "origin_changed")

    def test_origin_late_join_keeps_confirmation_before_newer_sender_work(self):
        self.source.add_message(1, "yes", chat_id=None)
        self.source.add_message(2, "new request")
        self.inbox.poll()
        seen = []
        self.inbox.dispatch_ready(seen.append)
        self.assertEqual([], seen)
        self.source.add_join(1)
        self.inbox.dispatch_ready(seen.append)
        self.assertEqual([1, 2], [message["ROWID"] for message in seen])

    def test_stale_future_and_invalid_timestamps_are_held(self):
        for rowid, timestamp, reason in ((1, NOW - 901, "stale_message"),
                                         (2, NOW + 121, "future_timestamp"),
                                         (3, None, "invalid_timestamp")):
            self.source.add_message(rowid, "do something", timestamp=timestamp)
        self.inbox.poll()
        self.assertIsNone(self.inbox.claim_next())
        for rowid, reason in ((1, "stale_message"), (2, "future_timestamp"), (3, "invalid_timestamp")):
            self.assert_held(rowid, reason)

    def test_age_is_rechecked_at_claim_not_only_intake(self):
        self.source.add_message(1)
        self.inbox.poll()
        self.inbox.now = lambda: NOW + 901
        self.assertIsNone(self.inbox.claim_next())
        self.assert_held(1, "stale_message")

    def test_claimed_guid_cannot_reappear_under_a_new_rowid(self):
        self.source.add_message(1)
        self.inbox.poll()
        self.inbox.dispatch_ready(lambda _: None)
        self.source.add_message(2, guid="synthetic-guid-1")
        with self.assertRaisesRegex(InboxSourceError, "duplicate_message_identity"):
            self.restart().poll()
        self.assertEqual({"handler_returned": 1}, inbox_health(self.source.bot_path)["counts"])

    def test_guid_duplicating_pre_cutover_history_is_not_dispatched(self):
        with self.source.connect() as conn:
            conn.execute("DELETE FROM message")
        # A distinct fresh ledger establishes a non-empty initial cutover.
        second_db = self.source.bot_path.with_suffix(".second")
        self.source.add_message(1, "pre-install history")
        inbox = MessageInbox(self.source.path, second_db, now=lambda: NOW)
        inbox.poll()
        self.source.add_message(2, "duplicate old identity", guid="synthetic-guid-1")
        inbox.poll()
        self.assertIsNone(inbox.claim_next())
        self.assertEqual({"ambiguous_message_identity": 1}, inbox_health(second_db)["held_reasons"])

    def test_present_conflicting_anchor_fails_closed(self):
        self.source.add_message(1)
        self.inbox.poll()
        with self.source.connect() as conn:
            conn.execute("UPDATE message SET guid='different-source-guid' WHERE ROWID=1")
        with self.assertRaisesRegex(InboxSourceError, "cursor_anchor_conflict"):
            self.restart().poll()
        self.assertEqual("cursor_anchor_conflict", inbox_health(self.source.bot_path)["source_error"])

    def test_replaced_source_file_cannot_replay_history(self):
        original = self.source.path
        replacement = original.with_suffix(".replacement")
        with closing(sqlite3.connect(original)) as source, closing(sqlite3.connect(replacement)) as destination:
            source.backup(destination)
        original.unlink()
        replacement.rename(original)
        with self.assertRaisesRegex(InboxSourceError, "source_identity_changed"):
            self.restart().poll()

    def test_current_permission_is_checked_after_restart(self):
        from davosbot import permissions
        self.source.add_message(1, "owner-only operation")
        self.inbox.poll()
        decisions = []
        # Being the owner at intake is not saved as an authorization capability.
        with patch.object(permissions, "OWNER_ID", FRIEND), patch.object(permissions, "is_admin", return_value=False):
            self.restart().dispatch_ready(lambda message: decisions.append(
                permissions.can_user_do(message["sender"], "grant_admin")))
        self.assertEqual([False], decisions)

    def test_unchanged_owner_and_group_origin_reach_existing_permission_gate(self):
        from davosbot import permissions
        self.source.add_message(1, "owner-only operation", chat_id=3)
        self.inbox.poll()
        seen = []
        with patch.object(permissions, "OWNER_ID", OWNER):
            self.restart().dispatch_ready(lambda message: seen.append((
                permissions.can_user_do(message["sender"], "grant_admin"), message["chat_identifier"])))
        self.assertEqual([(True, GROUP)], seen)

    def test_recovered_private_send_request_still_requires_fresh_confirmation(self):
        from davosbot import permissions, tools
        self.source.add_message(1, "msg +1 (555) 000-0002 synthetic hello")
        self.inbox.poll()
        tools._pending_private_sends.clear()
        self.addCleanup(tools._pending_private_sends.clear)
        replies = []
        with patch.object(permissions, "OWNER_ID", OWNER), \
                patch.object(tools, "_log_send_imessage_call"), \
                patch.object(tools, "_send_private_imessage") as send:
            self.restart().dispatch_ready(lambda message: replies.append(tools.handle_private_send_request(
                message["sender"], message["text"], originating_chat_id=message["chat_identifier"])))
        self.assertIn("Confirm", replies[0])
        send.assert_not_called()

    def test_claimed_private_confirmation_never_runs_again(self):
        self.source.add_message(1, "synthetic password confirmation")
        self.inbox.poll()
        self.inbox.claim_next()
        with patch("davosbot.tools.handle_private_send_confirmation") as confirm:
            self.restart().dispatch_ready(lambda message: confirm(message["sender"], message["text"]))
        confirm.assert_not_called()

    def test_recovered_private_request_password_pair_cannot_send_for_owner_or_admin(self):
        from davosbot import config, main, permissions, tools
        password = "synthetic-password-only"
        self.source.add_message(1, "msg +15550000002 synthetic private hello")
        self.source.add_message(2, password)
        self.source.add_message(3, "msg +15550000001 synthetic admin hello", sender_id=2, chat_id=2)
        self.source.add_message(4, "password: " + password, sender_id=2, chat_id=2)
        self.inbox.poll()
        tools._pending_private_sends.clear()
        self.addCleanup(tools._pending_private_sends.clear)
        requests = []
        def dispatch(message):
            requests.append(message["ROWID"])
            reply = tools.handle_private_send_confirmation(message["sender"], message["text"])
            if reply is None:
                tools.handle_private_send_request(message["sender"], message["text"], originating_chat_id=message["chat_identifier"])
        with patch.object(permissions, "OWNER_ID", OWNER), patch.object(permissions, "is_admin", return_value=True), \
                patch.object(config, "ADMIN_PASSWORD", password), patch.object(permissions, "ADMIN_PASSWORD", password), \
                patch.object(tools, "_log_send_imessage_call"), patch.object(tools, "_send_private_imessage") as send:
            restored = MessageInbox(self.source.path, self.source.bot_path, now=lambda: NOW,
                                    confirmation_guard=main._recovered_message_requires_confirmation)
            restored.dispatch_ready(dispatch)
        self.assertEqual([1, 3], requests)
        send.assert_not_called()
        self.assert_held(2, "fresh_confirmation_required")
        self.assert_held(4, "fresh_confirmation_required")

    def test_current_session_private_confirmation_still_sends_after_prompt(self):
        from davosbot import config, main, permissions, tools
        password = "synthetic-current-password"
        self.inbox.confirmation_guard = main._recovered_message_requires_confirmation
        self.source.add_message(1, "msg +15550000002 synthetic private hello")
        self.source.add_message(2, password)
        self.inbox.poll()
        tools._pending_private_sends.clear()
        self.addCleanup(tools._pending_private_sends.clear)
        def dispatch(message):
            reply = tools.handle_private_send_confirmation(message["sender"], message["text"])
            if reply is None:
                tools.handle_private_send_request(message["sender"], message["text"], originating_chat_id=message["chat_identifier"])
        with patch.object(permissions, "OWNER_ID", OWNER), \
                patch.object(config, "ADMIN_PASSWORD", password), patch.object(permissions, "ADMIN_PASSWORD", password), \
                patch.object(tools, "_log_send_imessage_call"), patch.object(tools, "_send_private_imessage", return_value=True) as send:
            self.assertEqual(2, self.inbox.dispatch_ready(dispatch))
        send.assert_called_once()

    def test_downtime_yes_fix_and_group_password_are_held_but_normal_group_text_runs(self):
        from davosbot import main, permissions
        forms = ["yes", "yes fix", "yes ship them", "fix the log", "ship fixes", "go ahead",
                 "log clear confirm", "chats disable stale confirm", "@Davos yes", "@Davos synthetic-secret"]
        for rowid, text in enumerate(forms, 1):
            self.source.add_message(rowid, text, chat_id=3)
        self.source.add_message(len(forms) + 1, "@Davos what do you think?", chat_id=3)
        with patch.object(permissions, "ADMIN_PASSWORD", "synthetic-secret"):
            restored = MessageInbox(self.source.path, self.source.bot_path, now=lambda: NOW,
                                    confirmation_guard=main._recovered_message_requires_confirmation)
            restored.poll()  # These were received before the new session's cutover.
            seen = []
            restored.dispatch_ready(seen.append)
        self.assertEqual([len(forms) + 1], [m["ROWID"] for m in seen])
        for rowid in range(1, len(forms) + 1):
            self.assert_held(rowid, "fresh_confirmation_required")

    def test_new_session_confirmation_after_startup_is_not_marked_recovered(self):
        from davosbot import main
        restored = MessageInbox(self.source.path, self.source.bot_path, now=lambda: NOW,
                                confirmation_guard=main._recovered_message_requires_confirmation)
        restored.poll()
        self.source.add_message(1, "yes")
        restored.poll()
        seen = []
        self.assertEqual(1, restored.dispatch_ready(seen.append))
        self.assertEqual("yes", seen[0]["text"])

    def test_confirmation_arriving_while_recovered_prompt_runs_is_held(self):
        from davosbot import main
        self.source.add_message(1, "prepare a private send")
        self.inbox.poll()
        restored = MessageInbox(self.source.path, self.source.bot_path, now=lambda: NOW,
                                confirmation_guard=main._recovered_message_requires_confirmation)
        restored.poll()
        def reconstruct(_message):
            self.source.add_message(2, "yes")
        restored.dispatch_ready(reconstruct)
        restored.poll()
        self.assertIsNone(restored.claim_next())
        self.assert_held(2, "fresh_confirmation_required")

    def test_confirmation_after_recovered_prompt_fence_remains_valid_at_same_timestamp(self):
        from davosbot import main
        self.source.add_message(1, "prepare a private send")
        self.inbox.poll()
        restored = MessageInbox(self.source.path, self.source.bot_path, now=lambda: NOW,
                                confirmation_guard=main._recovered_message_requires_confirmation)
        restored.poll()
        restored.dispatch_ready(lambda _: None)
        # Same-second dates are permitted: the source ROWID proves later arrival.
        self.source.add_message(2, "yes", timestamp=NOW)
        restored.poll()
        self.assertEqual(2, restored.claim_next()["ROWID"])

    def test_failed_recovery_fence_read_keeps_claim_and_blocks_followup(self):
        from davosbot import main
        self.source.add_message(1, "prepare a private send")
        self.inbox.poll()
        restored = MessageInbox(self.source.path, self.source.bot_path, now=lambda: NOW,
                                confirmation_guard=main._recovered_message_requires_confirmation)
        restored.poll()
        message = restored.claim_next()
        with patch.object(restored, "_source", side_effect=sqlite3.OperationalError("synthetic source failure")):
            with self.assertRaises(sqlite3.OperationalError):
                restored.finish(message["guid"])
        self.source.add_message(2, "yes")
        restored.poll()
        self.assertIsNone(restored.claim_next())
        self.assertEqual({"processing": 1, "pending": 1}, inbox_health(self.source.bot_path)["counts"])

    def test_recovered_group_request_cannot_use_password_dm_arriving_during_prompt(self):
        from davosbot import main, permissions
        self.source.add_message(1, "@Davos prepare a private send", chat_id=3)
        self.inbox.poll()
        restored = MessageInbox(self.source.path, self.source.bot_path, now=lambda: NOW,
                                confirmation_guard=main._recovered_message_requires_confirmation)
        restored.poll()
        with patch.object(permissions, "ADMIN_PASSWORD", "synthetic-password"):
            restored.dispatch_ready(lambda _: self.source.add_message(2, "synthetic-password", chat_id=1))
            restored.poll()
            self.assertIsNone(restored.claim_next())
        self.assert_held(2, "fresh_confirmation_required")

    def test_recovery_fence_does_not_hold_another_senders_current_confirmation(self):
        from davosbot import main
        self.source.add_message(1, "@Davos recovered request", chat_id=3)
        self.inbox.poll()
        restored = MessageInbox(self.source.path, self.source.bot_path, now=lambda: NOW,
                                confirmation_guard=main._recovered_message_requires_confirmation)
        restored.poll()
        restored.dispatch_ready(lambda _: self.source.add_message(2, "yes", sender_id=2, chat_id=3))
        restored.poll()
        self.assertEqual(2, restored.claim_next()["ROWID"])

    def test_old_group_request_cannot_overwrite_newer_same_sender_dm_request(self):
        self.source.add_message(1, "old private request", chat_id=None)
        self.source.add_message(2, "new private request", chat_id=1)
        self.inbox.poll()
        seen = []
        self.inbox.dispatch_ready(seen.append)
        self.assertEqual([], seen)
        self.source.add_join(1, 3)
        self.inbox.dispatch_ready(seen.append)
        self.assertEqual([1, 2], [m["ROWID"] for m in seen])

    def test_late_group_origin_cannot_follow_another_senders_newer_group_work(self):
        self.source.add_message(1, "old group request", sender_id=1, chat_id=None)
        self.source.add_message(2, "new group request", sender_id=2, chat_id=3)
        self.inbox.poll()
        seen = []
        self.inbox.dispatch_ready(seen.append)
        self.source.add_join(1, 3)
        self.inbox.dispatch_ready(seen.append)
        self.assertEqual([2], [m["ROWID"] for m in seen])
        self.assert_held(1, "origin_arrived_after_newer_work")

    def test_cross_chat_confirmation_fence_uses_existing_phone_normalization(self):
        from davosbot import main, config, permissions
        with self.source.connect() as conn:
            conn.execute("INSERT INTO handle VALUES (3, '+1 (555) 000-0001')")
        self.source.add_message(1, "recovered group request", chat_id=3)
        self.inbox.poll()
        restored = MessageInbox(self.source.path, self.source.bot_path, now=lambda: NOW,
                                confirmation_guard=main._recovered_message_requires_confirmation,
                                normalize_sender=config.normalize_handle)
        restored.poll()
        with patch.object(permissions, "ADMIN_PASSWORD", "synthetic-password"):
            restored.dispatch_ready(lambda _: self.source.add_message(2, "synthetic-password", sender_id=3, chat_id=1))
            restored.poll()
            self.assertIsNone(restored.claim_next())
        self.assert_held(2, "fresh_confirmation_required")

    def test_failed_fence_read_blocks_same_sender_cross_chat_followup(self):
        from davosbot import main
        self.source.add_message(1, "group request", chat_id=3)
        self.inbox.poll()
        restored = MessageInbox(self.source.path, self.source.bot_path, now=lambda: NOW,
                                confirmation_guard=main._recovered_message_requires_confirmation)
        restored.poll()
        claimed = restored.claim_next()
        with patch.object(restored, "_source", side_effect=sqlite3.OperationalError("synthetic source failure")):
            with self.assertRaises(sqlite3.OperationalError):
                restored.finish(claimed["guid"])
        self.source.add_message(2, "yes", chat_id=1)
        restored.poll()
        self.assertIsNone(restored.claim_next())


class InboxSessionSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = SourceFixture(temporary.name)
        with closing(sqlite3.connect(self.source.bot_path)) as conn, conn:
            conn.execute("CREATE TABLE bot_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, messages_processed INTEGER DEFAULT 0)")

    def session(self, commit=True):
        with closing(sqlite3.connect(self.source.bot_path)) as conn:
            cursor = conn.execute("INSERT INTO bot_sessions DEFAULT VALUES")
            session_id = cursor.lastrowid
            conn.commit() if commit else conn.rollback()
        return session_id

    def consumer(self, session_id):
        return MessageInbox(self.source.path, self.source.bot_path, now=lambda: NOW, session_id=session_id)

    def test_first_upgrade_skips_history_after_existing_legacy_sessions(self):
        self.session()
        self.session()
        self.source.add_message(1, "old history")
        inbox = self.consumer(self.session())
        self.assertEqual(0, inbox.poll())
        self.source.add_message(2, "fresh request")
        inbox.poll()
        self.assertEqual(2, inbox.claim_next()["ROWID"])

    def test_clean_committed_session_restart_recovers_pending(self):
        initial = self.consumer(self.session())
        initial.poll()
        self.source.add_message(1)
        initial.poll()
        restored = self.consumer(self.session())
        restored.poll()
        self.assertEqual(1, restored.claim_next()["ROWID"])

    def test_legacy_session_gap_holds_instead_of_replaying_legacy_handled_message(self):
        initial = self.consumer(self.session())
        initial.poll()
        self.session()  # Legacy polling session, with no durable consumer attached.
        self.source.add_message(1, "already handled by legacy polling")
        restored = self.consumer(self.session())
        for attempt in (restored, self.consumer(self.session())):
            with self.assertRaisesRegex(InboxSourceError, "untracked_runtime_session"):
                attempt.poll()
            with self.assertRaisesRegex(InboxSourceError, "untracked_runtime_session"):
                attempt.claim_next()
        self.assertEqual("untracked_runtime_session", inbox_health(self.source.bot_path)["source_error"])
        self.assertEqual({}, inbox_health(self.source.bot_path)["counts"])

    def test_failed_session_does_not_create_a_cutover_and_valid_next_session_can_start(self):
        failed = self.consumer(-1)
        with self.assertRaisesRegex(InboxSourceError, "runtime_session_not_committed"):
            failed.poll()
        self.assertFalse(inbox_health(self.source.bot_path)["initialized"])
        valid = self.consumer(self.session())
        self.assertEqual(0, valid.poll())
        self.assertTrue(inbox_health(self.source.bot_path)["initialized"])

    def test_noncommitted_session_id_cannot_authorize_consumer_start(self):
        uncommitted = self.session(commit=False)
        inbox = self.consumer(uncommitted)
        with self.assertRaisesRegex(InboxSourceError, "runtime_session_not_committed"):
            inbox.poll()
        self.assertFalse(inbox_health(self.source.bot_path)["initialized"])

    def test_reused_session_id_cannot_recover_or_dispatch(self):
        session_id = self.session()
        self.consumer(session_id).poll()
        reused = self.consumer(session_id)
        with self.assertRaisesRegex(InboxSourceError, "runtime_session_not_new"):
            reused.poll()


if __name__ == "__main__":
    unittest.main()
