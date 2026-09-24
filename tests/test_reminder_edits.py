"""Real owner-DM reminder edits use temporary state, never live scheduling."""

from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from davosbot import brain, main, memory, reminder_edits as edits
import test_native_command_history as native_history


OWNER = native_history.OWNER
FRIEND = native_history.FRIEND
GROUP = native_history.GROUP
NOW = datetime(2026, 9, 9, 18, 0, tzinfo=timezone.utc)
OLD_DUE = "2026-09-10 18:00:00"
NEW_DUE = "2026-09-10 15:00:00"


class _ReminderFixture:
    def reminder_setup(self):
        self.stack.enter_context(patch.object(brain, "BOT_DB_PATH", self.db_path))
        self.owner = self.stack.enter_context(patch.object(edits, "is_owner", side_effect=lambda value: value == OWNER))
        self.stack.enter_context(patch.object(edits, "_now", return_value=NOW))
        self.clock = self.stack.enter_context(patch.object(edits.time, "monotonic", return_value=100.0))
        self.stack.enter_context(patch.dict(edits._drafts, {}, clear=True))
        self.sql("""CREATE TABLE reminders (
            id INTEGER PRIMARY KEY, chat_id TEXT, origin_chat_id TEXT, message TEXT,
            due_ts TEXT, sent INTEGER DEFAULT 0, send_attempts INTEGER DEFAULT 0,
            last_attempt_ts TEXT)""")
        self.sql("INSERT INTO reminders (id,chat_id,origin_chat_id,message,due_ts) VALUES (?,?,?,?,?)",
                 (71, OWNER, OWNER, "Pack lunch\nBring bottle", OLD_DUE))
        self.sql("INSERT INTO reminders (id,chat_id,origin_chat_id,message,due_ts) VALUES (?,?,?,?,?)",
                 (93, GROUP, GROUP, "Group reminder", OLD_DUE))

    def sql(self, query, parameters=()):
        with closing(sqlite3.connect(self.db_path)) as conn:
            result = conn.execute(query, parameters).fetchall()
            conn.commit()
        return result

    def rows(self):
        return self.sql("SELECT * FROM reminders ORDER BY id")

    def edit(self, text, sender=OWNER, origin=OWNER):
        return edits.handle_edit(sender, text, originating_chat_id=origin, db_path=self.db_path)


class ReminderEditTests(_ReminderFixture, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db_path = str(Path(temporary.name) / "reminder-edit.sqlite")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.reminder_setup()

    def test_direct_reschedule_changes_only_due_time(self):
        original = self.rows()
        reply = self.edit("Move my reminder to tomorrow at 8am.")
        expected = [tuple(NEW_DUE if index == 4 else value for index, value in enumerate(original[0])), original[1]]
        self.assertEqual(expected, self.rows())
        self.assertIn("Moved the reminder", reply)
        self.assertIn(original[0][3], reply)
        self.assertNotIn("71", reply)
        self.assertFalse(edits._drafts)

    def test_missing_time_retains_original_and_continues_once(self):
        original = self.rows()
        self.assertIn("still active", self.edit("Move my reminder."))
        self.assertEqual(original, self.rows())
        self.assertIn("Moved the reminder", self.edit("Tomorrow at 8am."))
        saved = self.rows()
        self.assertIsNone(self.edit("Tomorrow at 9am."))
        self.assertEqual(saved, self.rows())

    def test_multiple_targets_retain_time_then_select_visible_position(self):
        self.sql("INSERT INTO reminders (id,chat_id,origin_chat_id,message,due_ts) VALUES (?,?,?,?,?)",
                 (101, OWNER, OWNER, "Second reminder", "2026-09-11 18:00:00"))
        original = self.rows()
        reply = self.edit("Reschedule my reminder to tomorrow at 8am")
        self.assertIn("Which reminder number", reply)
        self.assertIn("2.", reply)
        self.assertEqual(original, self.rows())
        self.assertIn("Moved the reminder", self.edit("2"))
        self.assertEqual(original[:2], self.rows()[:2])
        self.assertEqual(NEW_DUE, self.rows()[2][4])

    def test_position_then_time_and_direct_position(self):
        self.sql("INSERT INTO reminders (id,chat_id,origin_chat_id,message,due_ts) VALUES (?,?,?,?,?)",
                 (101, OWNER, OWNER, "Second reminder", "2026-09-11 18:00:00"))
        self.assertIn("Which reminder number", self.edit("Edit my reminder"))
        self.assertIn("future time", self.edit("number 2"))
        self.assertIn("Moved", self.edit("in 2 hours"))
        self.assertEqual("2026-09-09 20:00:00", self.rows()[2][4])
        self.assertIn("Moved", self.edit("Move reminder #2 to tomorrow at 8am"))
        # Current visible position 2 now refers to the original first reminder.
        self.assertEqual(NEW_DUE, self.rows()[0][4])

    def test_ambiguous_invalid_and_nonfuture_times_never_cancel(self):
        original = self.rows()
        for when in ("tomorrow", "8", "today at 10am", "today at 11am", "in 0 minutes",
                     "September 8 2026 at 8am", "tomorrow at 8am and 9am", "at 25:99"):
            with self.subTest(when=when):
                edits.clear_drafts(OWNER)
                reply = self.edit("Move my reminder to " + when)
                self.assertNotIn("Moved the reminder", reply)
                self.assertEqual(original, self.rows())
        self.assertIn("Moved", self.edit("tomorrow at 8am"))

    def test_bare_clock_is_not_misread_as_position(self):
        self.assertIn("Moved", self.edit("Move my reminder 8am"))
        self.assertEqual(NEW_DUE, self.rows()[0][4])

    def test_explanations_quotes_negations_and_hypotheticals_are_not_edits(self):
        original = self.rows()
        for text in ("Explain how to move my reminder.", '"Move my reminder to tomorrow at 8am"',
                     "Don't move my reminder.", "If I move my reminder, what happens?",
                     "He said move my reminder to tomorrow at 8am.", "How do I reschedule my reminder?"):
            with self.subTest(text=text):
                self.assertFalse(brain.detect_reminder_edit_intent(text))
                self.assertIsNone(self.edit(text))
                self.assertEqual(original, self.rows())

    def test_expired_cancelled_and_unrelated_drafts_cannot_execute(self):
        original = self.rows()
        self.edit("Move my reminder")
        self.clock.return_value = 401.0
        self.assertIsNone(self.edit("Tomorrow at 8am"))
        self.edit("Move my reminder")
        self.assertIn("edit cancelled", self.edit("never mind"))
        self.assertIsNone(self.edit("Tomorrow at 8am"))
        self.edit("Move my reminder")
        edits.discard_unrelated(OWNER, OWNER, "status", authorized=True)
        self.assertIsNone(self.edit("Tomorrow at 8am"))
        self.assertEqual(original, self.rows())

    def test_access_and_origin_are_rechecked_without_cross_chat_effects(self):
        original = self.rows()
        self.edit("Move my reminder")
        edits.discard_unrelated(FRIEND, FRIEND, "hello", authorized=False)
        self.assertTrue(edits._drafts)
        self.assertIsNone(self.edit("Tomorrow at 8am", sender=FRIEND))
        self.assertIsNone(self.edit("Move my reminder to tomorrow at 8am", origin=GROUP))
        self.owner.side_effect = lambda sender: False
        self.assertIsNone(self.edit("Tomorrow at 8am"))
        self.assertFalse(edits._drafts)
        self.assertEqual(original, self.rows())

    def test_changed_due_sent_attempted_or_missing_rows_cannot_replay_draft(self):
        for change in ("UPDATE reminders SET due_ts='2026-09-12 18:00:00' WHERE id=71",
                       "UPDATE reminders SET sent=1 WHERE id=71",
                       "UPDATE reminders SET send_attempts=1 WHERE id=71",
                       "UPDATE reminders SET message='Changed' WHERE id=71"):
            with self.subTest(change=change):
                self.sql("UPDATE reminders SET due_ts=?,sent=0,send_attempts=0,message=? WHERE id=71",
                         (OLD_DUE, "Pack lunch\nBring bottle"))
                self.edit("Move my reminder")
                self.sql(change)
                changed = self.rows()
                self.assertNotIn("Moved", self.edit("Tomorrow at 8am"))
                self.assertFalse(edits._drafts)
                self.assertEqual(changed, self.rows())
        self.sql("UPDATE reminders SET sent=0,send_attempts=0 WHERE id=71")
        self.edit("Move my reminder")
        self.sql("DELETE FROM reminders WHERE id=71")
        changed = self.rows()
        self.assertIn("No pending", self.edit("Tomorrow at 8am"))
        self.assertEqual(changed, self.rows())

    def test_already_due_and_legacy_failed_rows_are_never_revived(self):
        for due, sent, attempts in (("2026-09-09 17:00:00", 0, 0),
                                     ("2026-09-09 18:00:00", 0, 0),
                                     (OLD_DUE, 0, 1), (OLD_DUE, 1, 5)):
            with self.subTest(due=due, sent=sent, attempts=attempts):
                self.sql("UPDATE reminders SET due_ts=?,sent=?,send_attempts=? WHERE id=71", (due, sent, attempts))
                original = self.rows()
                self.assertIn("haven't changed or restarted", self.edit("Move my reminder"))
                self.assertFalse(edits._drafts)
                self.assertEqual(original, self.rows())

    def test_legacy_origin_keeps_destination_and_origin_fields(self):
        self.sql("UPDATE reminders SET origin_chat_id=NULL WHERE id=71")
        self.assertIn("Moved", self.edit("Move my reminder to tomorrow at 8am"))
        self.assertEqual((OWNER, None), self.rows()[0][1:3])

    def test_changed_list_insertion_invalidates_positional_draft(self):
        self.edit("Move my reminder")
        self.sql("INSERT INTO reminders (id,chat_id,origin_chat_id,message,due_ts) VALUES (?,?,?,?,?)",
                 (101, OWNER, OWNER, "New reminder", "2026-09-09 23:00:00"))
        original = self.rows()
        self.assertIn("list changed", self.edit("Tomorrow at 8am"))
        self.assertEqual(original, self.rows())
        self.assertFalse(edits._drafts)

    def test_access_and_time_are_checked_again_at_write_boundary(self):
        original = self.rows()
        self.owner.side_effect = [True, False]
        self.assertIn("Only the owner", self.edit("Move my reminder to tomorrow at 8am"))
        self.assertEqual(original, self.rows())
        self.owner.side_effect = lambda sender: sender == OWNER
        later = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)
        with patch.object(edits, "_now", side_effect=[NOW, NOW, later]):
            self.assertIn("no longer in the future", self.edit("Move my reminder to tomorrow at 8am"))
        self.assertEqual(original, self.rows())
        self.assertFalse(edits._drafts)

    def test_invalid_position_never_mutates_and_equal_time_is_verified(self):
        original = self.rows()
        self.edit("Move my reminder")
        self.assertIn("Choose one", self.edit("Move reminder 99 to tomorrow at 8am"))
        self.assertFalse(edits._drafts)
        self.assertEqual(original, self.rows())
        self.assertIn("No change needed", self.edit("Move reminder 1 to tomorrow at 11am"))
        self.assertEqual(original, self.rows())

    def test_transaction_failure_rolls_back_without_replay(self):
        original = self.rows()
        self.sql("CREATE TRIGGER block_edit BEFORE UPDATE ON reminders BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        self.assertIn("couldn't verify", self.edit("Move my reminder to tomorrow at 8am"))
        self.assertEqual(original, self.rows())
        self.assertFalse(edits._drafts)
        self.assertIsNone(self.edit("Tomorrow at 8am"))

    def test_fresh_snapshot_cas_and_readback_are_required(self):
        original_snapshot = edits._snapshot
        calls = 0
        def changed_snapshot(conn, origin):
            nonlocal calls
            calls += 1
            if calls == 2:
                conn.execute("UPDATE reminders SET message='Changed concurrently' WHERE id=71")
            return original_snapshot(conn, origin)
        with patch.object(edits, "_snapshot", side_effect=changed_snapshot):
            self.assertIn("list changed", self.edit("Move my reminder to tomorrow at 8am"))
        self.assertEqual(OLD_DUE, self.rows()[0][4])
        calls = 0
        def failed_readback(conn, origin):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise sqlite3.OperationalError("synthetic readback failure")
            return original_snapshot(conn, origin)
        with patch.object(edits, "_snapshot", side_effect=failed_readback):
            self.assertIn("couldn't verify", self.edit("Move my reminder to tomorrow at 8am"))
        self.assertEqual(NEW_DUE, self.rows()[0][4])
        self.assertIsNone(self.edit("Tomorrow at 9am"))


class ReminderEditDispatchTests(_ReminderFixture, unittest.TestCase):
    route = native_history.NativeCommandHistoryTests.route

    def setUp(self):
        native_history.NativeCommandHistoryTests.setUp(self)
        self.reminder_setup()

    def test_real_owner_handler_preserves_reminder_then_executes_followup(self):
        original = self.rows()
        self.route("Move my reminder.")
        self.assertEqual(original, self.rows())
        self.model.assert_not_called()
        self.route("Tomorrow at 8am.")
        self.model.assert_not_called()
        self.assertEqual(NEW_DUE, self.rows()[0][4])
        history = memory.get_history(OWNER)
        self.assertEqual("Move my reminder.", history[0]["content"])
        self.assertIn("Moved the reminder", history[-1]["content"])
        saved = self.rows()
        self.route("Tomorrow at 9am.")
        self.assertEqual(saved, self.rows())

    def test_direct_supplied_time_is_used_by_real_handler(self):
        self.route("Move my reminder to tomorrow at 8am.")
        self.model.assert_not_called()
        self.assertEqual(NEW_DUE, self.rows()[0][4])

    def test_failed_clarification_send_clears_draft_without_cancel(self):
        original = self.rows()
        self.send.return_value = False
        self.route("Move my reminder.")
        self.assertFalse(edits._drafts)
        self.assertEqual(original, self.rows())
        self.assertFalse(any(row["role"] == "assistant" for row in memory.get_history(OWNER)))
        self.send.return_value = True
        self.route("Tomorrow at 8am.")
        self.assertEqual(original, self.rows())

    def test_failed_or_raised_success_send_does_not_repeat_mutation(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                self.sql("UPDATE reminders SET due_ts=? WHERE id=71", (OLD_DUE,))
                self.send.reset_mock()
                self.send.return_value = False
                self.send.side_effect = RuntimeError("synthetic failure") if raises else None
                self.route("Move my reminder to tomorrow at 8am.")
                self.send.assert_called_once()
                self.assertFalse(edits._drafts)
                self.assertEqual(NEW_DUE, self.rows()[0][4])
                self.send.side_effect = None
                self.send.return_value = True
                self.route("Tomorrow at 9am.")
                self.assertEqual(NEW_DUE, self.rows()[0][4])

    def test_private_send_and_acute_safety_keep_precedence(self):
        original = self.rows()
        self.route("Move my reminder")
        self.confirmation.return_value = "Synthetic private confirmation"
        self.route("Tomorrow at 8am")
        self.assertEqual(original, self.rows())
        self.assertFalse(edits._drafts)
        self.confirmation.return_value = None
        self.route("Move my reminder")
        with patch.object(main, "_handle_acute_safety", return_value=True):
            self.route("I am in immediate danger")
        self.assertEqual(original, self.rows())
        self.assertFalse(edits._drafts)

    def test_native_unrelated_request_and_history_erasure_invalidate(self):
        original = self.rows()
        for clear in (lambda: self.route("billing"), lambda: memory.clear_history(OWNER),
                      lambda: memory.clear_history_count(OWNER, 1), lambda: memory.clear_history_minutes(OWNER, 1)):
            self.route("Move my reminder")
            self.assertTrue(edits._drafts)
            clear()
            self.assertFalse(edits._drafts)
            self.route("Tomorrow at 8am")
            self.assertEqual(original, self.rows())

    def test_owner_access_revocation_drops_pending_dm_edit(self):
        original = self.rows()
        self.route("Move my reminder")
        with patch.object(main, "is_owner", return_value=False):
            self.route("Tomorrow at 8am")
        self.assertFalse(edits._drafts)
        self.assertEqual(original, self.rows())

    def test_failed_history_save_never_repeats_verified_edit_or_reply(self):
        with patch.object(main, "save_turn", side_effect=sqlite3.OperationalError("synthetic failure")):
            self.route("Move my reminder to tomorrow at 8am")
        self.send.assert_called_once()
        self.assertEqual(NEW_DUE, self.rows()[0][4])
        self.assertFalse(edits._drafts)

    def test_explanation_and_nonowner_group_inputs_do_not_edit(self):
        original = self.rows()
        self.route("Explain how to move my reminder.")
        self.model.assert_called_once()
        self.assertEqual(original, self.rows())
        for sender, chat in ((FRIEND, None), (OWNER, GROUP)):
            self.route(("@Davos " if chat else "") + "Move my reminder to tomorrow at 8am.", sender=sender, chat=chat)
            self.assertEqual(original, self.rows())
            self.assertFalse(edits._drafts)


if __name__ == "__main__":
    unittest.main()
