"""Failed native questions clear only their own unfinished synthetic draft."""
from contextlib import closing
import sqlite3
import threading
import unittest
from unittest.mock import patch

from davosbot import cron_creation, main, research_cron
import test_research_cron_drafts as research_fixture
import test_group_cron_creation_permissions as constants


INITIALS = ("Create a quote cron", "Create a weekly research report about battery technology every Friday")
MULTISTEP = ("Create a weekly quote cron", "Create a weekly waiver report top 10 with why")
COMPLETE = ("Create a quote cron at 9am", research_fixture.COMPLETE)


class CronClarificationDeliveryTests(unittest.TestCase):
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
        self.fixture.send.side_effect = None
        self.fixture.send.return_value = True
        with closing(sqlite3.connect(self.fixture.db_path)) as conn:
            conn.execute("DELETE FROM cron_jobs")
            conn.commit()

    def fail_send(self, raises):
        self.fixture.send.return_value = False
        self.fixture.send.side_effect = RuntimeError("synthetic outbound failure") if raises else None

    def restore_send(self):
        self.fixture.send.side_effect = None
        self.fixture.send.return_value = True

    def assert_no_assistant_history(self):
        self.assertTrue(all(call.args[1] != "assistant" for call in self.case.history.call_args_list))

    def test_initial_false_or_raised_question_send_does_not_leave_hidden_draft(self):
        for initial in INITIALS:
            for group in (False, True):
                for raises in (False, True):
                    with self.subTest(initial=initial, group=group, raises=raises):
                        self.reset()
                        self.fail_send(raises)
                        self.ask(initial, group=group, mention=group)
                        self.assert_no_assistant_history()
                        self.assertFalse(cron_creation._pending)
                        self.assertFalse(research_cron._pending)
                        self.restore_send()
                        self.ask("9am", group=group)
                        self.assertEqual([], self.fixture.rows())

    def test_advanced_multistep_question_failure_drops_that_draft_only(self):
        for initial in MULTISTEP:
            for group in (False, True):
                for raises in (False, True):
                    with self.subTest(initial=initial, group=group, raises=raises):
                        self.reset()
                        self.assertIn("weekday", self.ask(initial, group=group, mention=group))
                        self.fail_send(raises)
                        self.assertIn("time", self.ask("Friday", group=group))
                        self.assert_no_assistant_history()
                        self.assertFalse(cron_creation._pending)
                        self.assertFalse(research_cron._pending)
                        self.restore_send()
                        self.ask("9am", group=group)
                        self.assertEqual([], self.fixture.rows())

    def test_admin_sports_initial_and_followup_failures_cannot_later_create_job(self):
        for group in (False, True):
            for advanced in (False, True):
                for raises in (False, True):
                    with self.subTest(group=group, advanced=advanced, raises=raises):
                        self.reset()
                        if advanced:
                            self.assertIn("weekday", self.ask("Create a weekly sports recap cron", group=group,
                                                             mention=group, sender=constants.ADMIN))
                        self.fail_send(raises)
                        self.ask("Friday" if advanced else "Create a sports recap cron", group=group,
                                 mention=group and not advanced, sender=constants.ADMIN)
                        self.assert_no_assistant_history()
                        self.assertFalse(cron_creation._pending)
                        self.restore_send()
                        self.ask("9am", group=group, sender=constants.ADMIN)
                        self.assertEqual([], self.fixture.rows())

    def test_failed_saved_receipt_keeps_job_and_never_replays_send(self):
        for complete in COMPLETE:
            for group in (False, True):
                for raises in (False, True):
                    with self.subTest(complete=complete, group=group, raises=raises):
                        self.reset()
                        self.fail_send(raises)
                        self.ask(complete, group=group, mention=group)
                        self.fixture.send.assert_called_once()
                        self.assert_no_assistant_history()
                        saved = self.fixture.rows()
                        self.assertEqual(1, len(saved))
                        self.assertEqual(1, saved[0][4])
                        self.restore_send()
                        self.ask("9am", group=group)
                        self.assertEqual(saved, self.fixture.rows())

    def test_unrelated_existing_sports_edit_receipt_cannot_clear_research_draft(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                self.reset()
                self.ask("Create a sports recap cron at 6am")
                self.ask(INITIALS[1])
                original = dict(research_cron._pending)
                self.fail_send(raises)
                self.assertIn("Updated sports recap", self.ask("change sports cron to 7pm"))
                self.assertEqual(original, research_cron._pending)
                self.assertEqual("19:00", self.fixture.rows()[0][1])
                self.restore_send()
                self.ask("9am")
                self.assertEqual(2, len(self.fixture.rows()))

    def test_replacement_draft_during_send_survives_compare_and_clear(self):
        for initial in INITIALS:
            for raises in (False, True):
                with self.subTest(initial=initial, raises=raises):
                    self.reset()
                    entered = False
                    failures = []
                    workers = []
                    def send(_recipient, _reply, **_kwargs):
                        nonlocal entered
                        if entered:
                            return True
                        entered = True
                        def replace():
                            try:
                                main.handle_message({"sender": constants.OWNER, "chat_identifier": constants.OWNER,
                                                     "text": initial.replace("quote", "health report").replace("battery technology", "solar panels")})
                            except Exception as exc:
                                failures.append(type(exc).__name__)
                        worker = threading.Thread(target=replace)
                        workers.append(worker)
                        worker.start()
                        worker.join(3)
                        if raises:
                            raise RuntimeError("synthetic old send failure")
                        return False
                    self.fixture.send.side_effect = send
                    main.handle_message({"sender": constants.OWNER, "chat_identifier": constants.OWNER, "text": initial})
                    self.assertTrue(workers)
                    self.assertTrue(all(not worker.is_alive() for worker in workers))
                    self.assertFalse(failures)
                    self.fixture.errors.assert_not_called()
                    self.assertTrue(cron_creation._pending or research_cron._pending)
                    self.restore_send()
                    self.ask("9am")
                    self.assertEqual(1, len(self.fixture.rows()))
                    row = self.fixture.rows()[0]
                    if "quote" in initial:
                        self.assertEqual("drift_check", row[2])
                    else:
                        self.assertIn("solar panels", row[3])

    def test_other_actor_chat_drafts_survive_a_failed_question(self):
        for initial in INITIALS:
            with self.subTest(initial=initial):
                self.reset()
                self.ask(initial, group=True, mention=True, chat=constants.OTHER_GROUP)
                self.ask("Create a sports recap cron", group=True, mention=True, sender=constants.ADMIN)
                self.fail_send(False)
                self.ask(initial)
                self.restore_send()
                self.ask("9am", group=True, chat=constants.OTHER_GROUP)
                self.ask("9am", group=True, sender=constants.ADMIN)
                self.assertEqual(2, len(self.fixture.rows()))

    def test_private_flow_failure_does_not_clear_existing_cron_state(self):
        for initial in INITIALS:
            for group in (False, True):
                with self.subTest(initial=initial, group=group):
                    self.reset()
                    self.ask(initial, group=group, mention=group)
                    self.fail_send(False)
                    with patch.object(main, "handle_private_send_confirmation", return_value="PRIVATE FLOW FIRST"):
                        self.assertEqual("PRIVATE FLOW FIRST", self.ask("9am", group=group))
                    self.assertTrue(cron_creation._pending or research_cron._pending)
                    self.assertEqual([], self.fixture.rows())
                    self.restore_send()
                    self.ask("9am", group=group)
                    self.assertEqual(1, len(self.fixture.rows()))

    def test_history_failure_does_not_resend_delivered_question_or_lose_draft(self):
        self.case.history.side_effect = sqlite3.OperationalError("synthetic history failure")
        self.ask(INITIALS[0])
        self.fixture.send.assert_called_once()
        self.assertTrue(cron_creation._pending)
        self.case.history.side_effect = None
        self.ask("9am")
        self.assertEqual(1, len(self.fixture.rows()))


if __name__ == "__main__":
    unittest.main()
