"""Switching explicit workflows cannot later complete an abandoned cron."""

import unittest
from unittest.mock import patch

from davosbot import cron_creation, file_continuation, main, research_cron, tools
import test_reminder_edits as reminder_fixture
import test_group_cron_creation_permissions as group_fixture


INITIALS = ("Create a quote cron", "Create a weekly research report about battery technology every Friday")
FILE_REQUEST = "Save these rows as a CSV file: name,total / Sample,42"


class WorkflowDraftHandoffTests(unittest.TestCase):
    def setUp(self):
        self.fixture = reminder_fixture.ReminderEditDispatchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        for module in (cron_creation, research_cron, file_continuation):
            self.fixture.stack.enter_context(patch.object(module, "_pending", {}))
        self.fixture.stack.enter_context(patch.object(main, "_schedule_cron_from_text", tools._schedule_cron_from_text))
        self.fixture.sql("""CREATE TABLE cron_jobs (
            id INTEGER PRIMARY KEY, cron_expression TEXT, action_type TEXT,
            action_payload TEXT, enabled INTEGER DEFAULT 1, created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_run TEXT)""")

    def ask(self, text):
        self.fixture.send.reset_mock()
        self.fixture.route(text)
        self.fixture.send.assert_called_once()
        return self.fixture.send.call_args.args[1]

    def assert_no_cron(self):
        self.assertEqual([], self.fixture.sql("SELECT id FROM cron_jobs"))

    def test_new_reminder_task_prevents_old_ordinary_or_research_cron_revival(self):
        for initial in INITIALS:
            with self.subTest(initial=initial):
                self.fixture.sql("UPDATE reminders SET due_ts=? WHERE id=71", (reminder_fixture.OLD_DUE,))
                self.assertIn("time", self.ask(initial))
                self.assertIn("Moved the reminder", self.ask("Move my reminder to tomorrow at 8am"))
                self.ask("9am")
                self.assert_no_cron()
                self.assertEqual(reminder_fixture.NEW_DUE, self.fixture.rows()[0][4])
                self.assertFalse(cron_creation._pending)
                self.assertFalse(research_cron._pending)

    def test_new_file_task_prevents_old_cron_revival(self):
        for initial in INITIALS:
            with self.subTest(initial=initial):
                self.ask(initial)
                self.fixture.model.return_value = "What filename should I use?"
                self.ask(FILE_REQUEST)
                self.ask("9am")
                self.assert_no_cron()
                self.assertFalse(cron_creation._pending)
                self.assertFalse(research_cron._pending)

    def test_new_cron_clears_pending_reminder_and_file_in_reverse_direction(self):
        from davosbot import reminder_edits
        self.ask("Move my reminder")
        self.assertTrue(reminder_edits._drafts)
        self.ask(INITIALS[0])
        self.assertFalse(reminder_edits._drafts)
        self.fixture.model.return_value = "What filename should I use?"
        self.ask(FILE_REQUEST)
        self.assertTrue(file_continuation._pending)
        self.ask(INITIALS[1])
        self.assertFalse(file_continuation._pending)
        self.assert_no_cron()

    def test_other_actor_chat_or_readonly_query_cannot_clear_owner_cron(self):
        owner = reminder_fixture.OWNER
        self.ask(INITIALS[1])
        original = dict(research_cron._pending)
        for sender, chat, request in ((group_fixture.FRIEND, owner, FILE_REQUEST),
                                     (owner, group_fixture.OTHER_GROUP, FILE_REQUEST),
                                     (owner, owner, "How do I move my reminder?"),
                                     (owner, owner, "Who am I?")):
            main._clear_superseded_cron_drafts(sender, request, chat)
            self.assertEqual(original, research_cron._pending)
        self.ask("9am")
        self.assertEqual(1, len(self.fixture.sql("SELECT id FROM cron_jobs")))

    def test_saved_cron_is_never_disabled_by_new_workflow(self):
        self.ask(INITIALS[0])
        self.ask("9am")
        saved = self.fixture.sql("SELECT * FROM cron_jobs")
        self.ask(INITIALS[1])
        self.ask("Move my reminder to tomorrow at 8am")
        self.assertEqual(saved, self.fixture.sql("SELECT * FROM cron_jobs"))


class GroupWorkflowDraftHandoffTests(unittest.TestCase):
    def setUp(self):
        self.fixture = group_fixture.GroupCronCreationPermissionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(patch.stopall)
        patch.object(file_continuation, "_pending", {}).start()
        patch.object(main, "_remember_unmentioned_group_text").start()
        patch.object(main, "_log_owner_quality_intake_if_needed", return_value=None).start()

    def test_mentioned_file_request_supersedes_only_current_group_cron_draft(self):
        self.fixture.ask("@Davos " + INITIALS[0], chat=group_fixture.OTHER_GROUP)
        self.fixture.ask("@Davos " + INITIALS[1])
        self.fixture.model.return_value = "What filename should I use?"
        self.fixture.ask("@Davos " + FILE_REQUEST)
        self.assertIsNone(self.fixture.ask("9am"))
        self.assertEqual([], self.fixture.rows())
        self.assertIn("scheduled", self.fixture.ask("9am", chat=group_fixture.OTHER_GROUP))
        self.assertEqual(1, len(self.fixture.rows()))


if __name__ == "__main__":
    unittest.main()
