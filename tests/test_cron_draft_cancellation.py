"""Punctuated draft controls cannot mutate saved jobs or another actor's state."""
from contextlib import closing
import sqlite3
import unittest

from davosbot import cron_creation, research_cron
import test_research_cron_drafts as research_fixture
import test_group_cron_creation_permissions as constants


INITIALS = ("Create a quote cron", "Create a weekly research report about battery technology every Friday")
CONTROLS = ("Never mind.", "cancel cron draft.", "cancel new cron!", " cancel\ncron   draft ? ")


class CronDraftCancellationTests(unittest.TestCase):
    def setUp(self):
        self.case = research_fixture.ResearchCronDraftTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.fixture = self.case.fixture

    def ask(self, text, *, group=False, mention=False, sender=constants.OWNER, chat=None):
        return self.case.ask(text, group=group, mention=mention, sender=sender, chat=chat)

    def reset(self):
        cron_creation._pending.clear()
        research_cron._pending.clear()
        with closing(sqlite3.connect(self.fixture.db_path)) as conn:
            conn.execute("DELETE FROM cron_jobs")
            conn.commit()

    def test_exact_whole_controls_accept_punctuation_not_quotes_negations_or_content(self):
        for text in (*CONTROLS, "never mind", "nevermind...", "cancel new cron?!", "@Davos cancel cron draft."):
            with self.subTest(accepted=text):
                self.assertTrue(cron_creation.is_draft_cancel(text))
        for text in ('"cancel cron draft."', "'never mind'", "do not cancel cron draft.",
                     "don't cancel new cron!", "he said cancel cron draft.", "Explain cancel cron draft.",
                     "cancel cron draft and all saved jobs", "never mind the previous paragraph",
                     "cancel cron #1", "cancel cron draft; cancel cron #1", "cancel cron drafts"):
            with self.subTest(rejected=text):
                self.assertFalse(cron_creation.is_draft_cancel(text))

    def test_both_draft_families_cancel_in_dm_and_mentioned_or_unmentioned_groups(self):
        for initial in INITIALS:
            for group, mention in ((False, False), (True, True), (True, False)):
                for control in CONTROLS:
                    with self.subTest(initial=initial, group=group, mention=mention, control=control):
                        self.reset()
                        self.ask("Create a quote cron at 6am", group=group, mention=group)
                        saved = self.fixture.rows()
                        self.assertIn("time", self.ask(initial, group=group, mention=group))
                        self.assertIn("draft", self.ask(control, group=group, mention=mention))
                        self.assertEqual(saved, self.fixture.rows())
                        self.ask("9am", group=group)
                        self.assertEqual(saved, self.fixture.rows())
                        destination = constants.GROUP if group else constants.OWNER
                        self.assertIsNone(cron_creation.pending_draft(constants.OWNER, destination))
                        self.assertFalse(research_cron.is_pending_followup(constants.OWNER, destination, "9am"))

    def test_no_draft_controls_do_not_disable_saved_jobs(self):
        for group, mention in ((False, False), (True, True), (True, False)):
            for control in CONTROLS:
                with self.subTest(group=group, mention=mention, control=control):
                    self.reset()
                    self.ask("Create a quote cron at 6am", group=group, mention=group)
                    saved = self.fixture.rows()
                    self.ask(control, group=group, mention=mention)
                    self.assertEqual(saved, self.fixture.rows())

    def test_wrong_actor_or_other_chat_does_not_clear_owner_draft(self):
        for initial in INITIALS:
            with self.subTest(initial=initial):
                self.reset()
                self.ask(initial, group=True, mention=True)
                for sender, chat, mention in ((constants.FRIEND, constants.GROUP, False),
                                              (constants.FRIEND, constants.GROUP, True),
                                              (constants.ADMIN, constants.GROUP, True),
                                              (constants.OWNER, constants.OTHER_GROUP, True)):
                    self.ask("cancel cron draft.", group=True, mention=mention, sender=sender, chat=chat)
                    self.assertEqual([], self.fixture.rows())
                self.ask("9am", group=True)
                self.assertEqual(1, len(self.fixture.rows()))

    def test_quoted_and_negated_unmentioned_text_preserves_pending_control_scope(self):
        for initial in INITIALS:
            for text in ('"Never mind."', "don't cancel cron draft.", "He said cancel new cron!"):
                with self.subTest(initial=initial, text=text):
                    self.reset()
                    self.ask(initial, group=True, mention=True)
                    self.assertIsNone(self.ask(text, group=True))
                    self.assertEqual([], self.fixture.rows())
                    self.ask("9am", group=True)
                    self.assertEqual(1, len(self.fixture.rows()))

    def test_explicit_saved_job_cancel_keeps_existing_authority_and_behavior(self):
        for group in (False, True):
            with self.subTest(group=group):
                self.reset()
                self.ask("Create a quote cron at 6am", group=group, mention=group)
                saved = self.fixture.rows()
                request = "cancel cron #" + str(saved[0][0])
                self.ask(request, group=group, mention=group, sender=constants.FRIEND)
                self.assertEqual(saved, self.fixture.rows())
                self.assertIn("Disabled cron", self.ask(request, group=group, mention=group))
                self.assertEqual(0, self.fixture.rows()[0][4])


if __name__ == "__main__":
    unittest.main()
