"""Saved cron cancellation needs an affirmative whole command, not nearby words."""
from contextlib import closing
import sqlite3
from unittest.mock import patch
import unittest

from davosbot import commands, main, tools
import test_cron_draft_cancellation as cancellation_fixture
from test_group_cron_creation_permissions import OWNER, ADMIN, FRIEND, OTHER_GROUP


class CronCancelIntentBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.case = cancellation_fixture.CronDraftCancellationTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.fixture = self.case.fixture

    def ask(self, text, *, group=False, mention=False, sender=OWNER, chat=None):
        return self.case.ask(text, group=group, mention=mention, sender=sender, chat=chat)

    def test_documented_and_polite_single_id_commands_parse(self):
        for text in ("please cancel cron #2", "turn off #2", "delete #2", "cancel cron 2",
                     "remove job id #2", "disable the cron id 2.", "delete id 2", "cancel daily job #2",
                     "Can you cancel cron #2?", "Could you please delete #2?",
                     "please would you turn off #2 please!", "@Davos: cancel cron #2", "Can you cancel cron #2 ?"):
            with self.subTest(text=text):
                self.assertEqual({"cron_id": 2, "time_pt": "", "action": ""},
                                 tools._parse_cron_cancel_command(text))

    def test_native_dm_id_shortcut_matches_the_whole_positive_command(self):
        for text in ("delete #7", "delete id 7", "cancel cron 7", "cancel daily job #7",
                     "cancel job id #7 please.", "disable #7?"):
            with self.subTest(accepted=text):
                self.assertEqual(7, commands._parse_cancel_cron_id(text))
        for text in ("cancel cron #1 but not #2", "Cancel cron #1? No, don't.",
                     'cancel cron #1 "#2"', "cancel cron #0", "cancel #000",
                     "cancel cron #1; cancel 17", "he said cancel cron #1",
                     '`cancel cron #1`', '"cancel cron #1"', 'cancel 17', "delete 6:30 daily"):
            with self.subTest(rejected=text):
                self.assertIsNone(commands._parse_cancel_cron_id(text))

    def test_named_and_single_valid_time_commands_parse(self):
        for text, clock, action in (
            ("cancel the 6:30 daily", "06:30", ""),
            ("kill the morning job", "", "morning_message"),
            ("Could you kill the morning job?", "", "morning_message"),
            ("stop the daily quote at 8am PT please.", "08:00", "morning_message"),
            ("cancel the sports recap cron at 6pm", "18:00", "sports_recap"),
            ("disable drift check at midnight", "00:00", "drift_check"),
            ("delete the noon daily", "12:00", ""),
        ):
            with self.subTest(text=text):
                self.assertEqual({"cron_id": None, "time_pt": clock, "action": action},
                                 tools._parse_cron_cancel_command(text))

    def test_reported_quoted_retracted_and_ambiguous_selectors_do_not_parse(self):
        samples = (
            '"cancel cron #1"', '“cancel cron #1”', '`cancel cron #1`', '> cancel cron #1',
            "he said cancel cron #1", "Explain how to cancel cron #1", "Why did you cancel cron #1?",
            "Do not cancel cron #1", "don't cancel cron #1", "Never cancel cron #1",
            "cancel cron #1 but not #2", "cancel cron #1 and #2", "cancel cron #1; list crons",
            "cancel cron #1? No, don't.", 'cancel cron "#1"', 'cancel cron #1 "#2"',
            "cancel cron #0", "cancel #000", "cancel cron -1", "cancel cron #1 at 8am",
            "cancel the daily at 25:90", "cancel the daily at 0am", "cancel the daily at 13pm",
            "cancel the daily at 8", "cancel the daily at 8am ET", "cancel the 6am daily at 8am",
            "cancel the daily at 6am and 8am", "cancel cron draft.", "cancel new cron!",
            "cancel the unfinished cron", "stop talking about your job", "cancel the daily except Friday",
        )
        for text in samples:
            with self.subTest(text=text):
                self.assertIsNone(tools._parse_cron_cancel_command(text))

    def test_nonexact_draft_references_never_disable_saved_jobs_or_clear_pending_drafts(self):
        samples = ('"cancel cron draft."', '“cancel cron draft.”', '`cancel cron draft.`',
                   '> cancel cron draft.', 'he said cancel cron draft.',
                   "Cancel cron draft? No, don't.", "don't cancel new cron!",
                   "delete the unfinished cron", "cancel cron draft and all saved jobs")
        for initial in (None, *cancellation_fixture.INITIALS):
            for group, mention in ((False, False), (True, True), (True, False)):
                for text in samples:
                    with self.subTest(initial=initial, group=group, mention=mention, text=text):
                        self.case.reset()
                        self.ask("Create a quote cron at 6am", group=group, mention=group)
                        saved = self.fixture.rows()
                        if initial:
                            self.ask(initial, group=group, mention=group)
                        reply = self.ask(text, group=group, mention=mention)
                        self.assertEqual(saved, self.fixture.rows())
                        self.fixture.model.assert_not_called()
                        if not group or mention:
                            self.assertIn("No saved cron was changed", reply)
                        else:
                            self.assertIsNone(reply)
                        if initial:
                            self.ask("9am", group=group)
                            self.assertEqual(len(saved) + 1, len(self.fixture.rows()))

    def test_rejected_saved_job_text_preserves_original_conversation_and_native_rows(self):
        samples = ('`cancel cron #1`', '> cancel cron #1', 'he said cancel cron #1',
                   'Explain cancel cron #1', 'cancel cron #1 but not #2',
                   "Cancel cron #1? No, don't.", 'cancel cron #0')
        for group in (False, True):
            for text in samples:
                with self.subTest(group=group, text=text):
                    self.case.reset()
                    self.ask("Create a quote cron at 6am", group=group, mention=group)
                    saved = self.fixture.rows()
                    self.ask(text, group=group, mention=group)
                    self.assertEqual(saved, self.fixture.rows())
                    # Read-only description and one-off usage shortcuts retain
                    # their native replies. Reported/quoted text reaches chat
                    # unchanged; model-driven action safety is not simulated.
                    if text.startswith(('`', '>', 'he said')):
                        self.fixture.model.assert_called_once()
                        self.assertIn(text, str(self.fixture.model.call_args))

    def test_invalid_named_selectors_do_not_fall_back_to_the_only_saved_job(self):
        for group in (False, True):
            for text in ("cancel the daily at 25:90", "cancel the daily at 13pm",
                         "cancel the daily at 8am ET", "cancel the 6am daily at 8am",
                         "cancel the daily at 6am and 8am", "cancel cron #1 at 8am"):
                with self.subTest(group=group, text=text):
                    self.case.reset()
                    self.ask("Create a quote cron at 6am", group=group, mention=group)
                    saved = self.fixture.rows()
                    self.ask(text, group=group, mention=group)
                    self.assertEqual(saved, self.fixture.rows())

    def test_native_oneoff_cancel_keeps_its_separate_complete_numeric_tail(self):
        self.ask("Create a quote cron at 6am")
        saved = self.fixture.rows()
        with closing(sqlite3.connect(self.fixture.db_path)) as conn:
            conn.execute("CREATE TABLE scheduled_tasks (id INTEGER PRIMARY KEY, status TEXT)")
            conn.execute("INSERT INTO scheduled_tasks VALUES (17, 'pending')")
            conn.commit()
        for text in ("cancel cron #1 but not #2", "cancel 17 and cron #1", "cancel cron #1; cancel 17"):
            self.ask(text)
            self.assertEqual(saved, self.fixture.rows())
            with closing(sqlite3.connect(self.fixture.db_path)) as conn:
                self.assertEqual("pending", conn.execute("SELECT status FROM scheduled_tasks WHERE id = 17").fetchone()[0])
        self.assertIn("Cancelled #17", self.ask("cancel 17"))
        self.assertEqual(saved, self.fixture.rows())
        with closing(sqlite3.connect(self.fixture.db_path)) as conn:
            self.assertEqual("cancelled", conn.execute("SELECT status FROM scheduled_tasks WHERE id = 17").fetchone()[0])

    def test_direct_commands_keep_owner_gate_and_reopen_disabled_row(self):
        for group in (False, True):
            for command in ("Can you please cancel cron #1?", "turn off #1", "delete #1"):
                with self.subTest(group=group, command=command):
                    self.case.reset()
                    self.ask("Create a quote cron at 6am", group=group, mention=group)
                    saved = self.fixture.rows()
                    for sender in (ADMIN, FRIEND):
                        self.ask(command, group=group, mention=group, sender=sender)
                        self.assertEqual(saved, self.fixture.rows())
                    reply = self.ask(command, group=group, mention=group)
                    self.assertIn("Disabled cron #1", reply)
                    self.assertEqual(0, self.fixture.rows()[0][4])
                    self.fixture.model.assert_not_called()

    def test_named_matching_keeps_current_chat_and_ambiguity_rules(self):
        self.ask("Create a quote cron at 6:30am", group=True, mention=True)
        self.ask("Create a quote cron at 7am", group=True, mention=True)
        self.ask("Create a quote cron at 6:30am", group=True, mention=True, chat=OTHER_GROUP)
        saved = self.fixture.rows()
        self.assertIn("2 possible jobs", self.ask("kill the morning job", group=True, mention=True))
        self.assertEqual(saved, self.fixture.rows())
        self.assertIn("Disabled cron #1", self.ask("cancel the 6:30 daily", group=True, mention=True))
        self.assertEqual([0, 1, 1], [row[4] for row in self.fixture.rows()])
        # Stable IDs intentionally retain the owner's existing cross-chat behavior.
        self.assertIn("Disabled cron #3", self.ask("delete #3", group=True, mention=True))
        self.assertEqual([0, 1, 0], [row[4] for row in self.fixture.rows()])

    def test_group_access_and_mention_gates_still_precede_cancellation(self):
        self.ask("Create a quote cron at 6am", group=True, mention=True)
        saved = self.fixture.rows()
        self.assertIsNone(self.ask("cancel cron #1", group=True))
        for gate in (self.fixture.enabled, self.fixture.owner_present):
            gate.return_value = False
            self.assertIsNone(self.ask("cancel cron #1", group=True, mention=True))
            self.assertEqual(saved, self.fixture.rows())
            gate.return_value = True
        self.fixture.rate_limit.return_value = False
        with patch.object(main._rate_limit_notice, "allow", return_value=True):
            self.assertIn("message limit", self.ask("cancel cron #1", group=True, mention=True))
        self.assertEqual(saved, self.fixture.rows())


if __name__ == "__main__":
    unittest.main()
