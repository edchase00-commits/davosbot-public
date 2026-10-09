"""Synthetic messages use real owner checks, dispatch, parsing and saved-row readback."""

from contextlib import ExitStack
import json
from unittest.mock import patch
import unittest

from davosbot import commands, main, permissions, research_cron, research_reports
import test_group_cron_creation_permissions as cron_fixtures
from test_group_cron_creation_permissions import (
    OWNER, ADMIN, FRIEND, GROUP, OTHER_GROUP, REAL_IS_OWNER,
)


COMPLETE = "Create a weekly fantasy waiver wire report top 10 with why, FAAB budget $100, every Wednesday at 00:59 PT"
MISSING_BUDGET = COMPLETE.replace("FAAB budget $100, ", "")


class ResearchCronDraftTests(unittest.TestCase):
    def setUp(self):
        self.fixture = cron_fixtures.GroupCronCreationPermissionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(permissions, "OWNER_ID", OWNER))
        for module in (main, commands, permissions):
            self.stack.enter_context(patch.object(module, "is_owner", REAL_IS_OWNER))
        for name in ("detect_user_fact", "_log_owner_quality_intake_if_needed", "_remember_unmentioned_group_text"):
            self.stack.enter_context(patch.object(main, name, return_value=None))
        self.history = self.stack.enter_context(patch.object(main, "save_turn"))
        self.stack.enter_context(patch.object(research_reports, "fetch_sources", side_effect=AssertionError("No research during intake")))

    def ask(self, text, *, group=False, mention=False, sender=OWNER, chat=None):
        fixture = self.fixture
        fixture.send.reset_mock()
        fixture.model.reset_mock()
        self.history.reset_mock()
        destination = chat or (GROUP if group else sender)
        main.handle_message({"sender": sender, "chat_identifier": destination,
                             "text": ("@Davos " if mention else "") + text})
        fixture.errors.assert_not_called()
        if not fixture.send.called:
            return None
        fixture.send.assert_called_once()
        self.assertEqual(destination, fixture.send.call_args.args[0])
        self.assertEqual(group, fixture.send.call_args.kwargs.get("is_group", False))
        return fixture.send.call_args.args[1]

    def payload(self, *, count=1, schedule="00:59 wed", recipient=OWNER):
        rows = self.fixture.rows()
        self.assertEqual(count, len(rows))
        row = rows[-1]
        self.assertEqual((schedule, "research_report"), row[1:3])
        payload = json.loads(row[3])
        self.assertEqual(recipient, payload["recipient"])
        self.fixture.model.assert_not_called()
        return payload

    def test_complete_affirmative_forms_reach_native_creation_for_verified_owner(self):
        for index, prefix in enumerate(("Create", "Can you please create", "I want", "New cron:"), 1):
            text = COMPLETE.replace("Create", prefix, 1)
            self.assertIn("Saved research cron", self.ask(text))
            payload = self.payload(count=index)
            self.assertEqual((10, 100), (payload["top_n"], payload["faab_budget"]))

    def test_missing_weekday_time_and_budget_complete_one_slot_at_a_time(self):
        for group in (False, True):
            with self.subTest(group=group):
                count = len(self.fixture.rows())
                self.assertIn("weekday", self.ask("Create a weekly waiver report top 10 with why", group=group, mention=group))
                self.assertIn("Pacific time", self.ask("Wednesday please", group=group))
                self.assertIn("FAAB budget", self.ask("00:59 PT", group=group))
                self.assertEqual(count, len(self.fixture.rows()))
                self.assertIn("Saved research cron", self.ask("$100", group=group))
                payload = self.payload(count=count + 1, recipient=GROUP if group else OWNER)
                self.assertEqual((10, 100), (payload["top_n"], payload["faab_budget"]))

    def test_missing_topic_retains_weekday_time_and_instructions(self):
        self.assertIn("public topic", self.ask("Schedule a weekly research report every Friday at 9am PT. Compare practical tradeoffs.", group=True, mention=True))
        self.assertIn("Saved research cron", self.ask("about battery technology", group=True))
        payload = self.payload(schedule="09:00 fri", recipient=GROUP)
        self.assertEqual("battery technology", payload["research_topic"])
        self.assertIn("Compare practical tradeoffs", payload["instructions"])

    def test_invalid_slot_can_be_corrected_without_retyping_the_request(self):
        self.ask(MISSING_BUDGET, group=True, mention=True)
        self.assertIn("$10,000", self.ask("$10001", group=True))
        self.assertEqual([], self.fixture.rows())
        self.assertIn("Saved research cron", self.ask("$100", group=True))
        self.payload(recipient=GROUP)

    def test_new_ordinary_cron_supersedes_only_same_chat_research_draft(self):
        self.ask(MISSING_BUDGET, group=True, mention=True)
        self.ask(MISSING_BUDGET)
        self.assertIn("scheduled", self.ask("create a quote cron at 8am", group=True, mention=True))
        self.assertIsNone(self.ask("$100", group=True))
        self.assertIn("Saved research cron", self.ask("$100"))
        self.payload(count=2)

    def test_generic_new_cron_handoff_retains_time_and_accepts_unmentioned_answer(self):
        self.assertIn("research/waiver", self.ask("create a weekly cron at 8am", group=True, mention=True))
        self.assertIn("weekday", self.ask("fantasy waiver wire report top 10 with why and FAAB budget $100", group=True))
        self.assertIn("Saved research cron", self.ask("Friday", group=True))
        self.payload(schedule="08:00 fri", recipient=GROUP)
        self.assertIsNone(self.ask("9am", group=True))
        self.assertEqual(1, len(self.fixture.rows()))

    def test_generic_handoff_never_drops_previously_explicit_daily_cadence(self):
        self.ask("create a daily cron at 8am", group=True, mention=True)
        reply = self.ask("waiver wire report top 10 with why and FAAB budget $100 every Friday", group=True)
        self.assertIn("one weekly", reply)
        self.assertEqual([], self.fixture.rows())

    def test_normalized_owner_draft_and_destination_do_not_depend_on_phone_format(self):
        self.ask(MISSING_BUDGET, sender="(555) 000-0001")
        self.assertIn("Saved research cron", self.ask("100 dollars", sender=OWNER))
        self.payload()
        self.ask(MISSING_BUDGET, group=True, mention=True, sender="5550000001")
        self.assertIn("Saved research cron", self.ask("FAAB budget 100", group=True, sender="(555) 000-0001"))
        self.payload(count=2, recipient=GROUP)

    def test_complete_raw_owner_dm_destination_is_canonical_and_date_error_is_not_invented(self):
        self.assertIn("Saved research cron", self.ask(COMPLETE, sender="(555) 000-0001"))
        self.payload()

    def test_other_senders_chats_expiry_and_current_access_cannot_complete_draft(self):
        self.ask(MISSING_BUDGET, group=True, mention=True)
        for sender, chat in ((ADMIN, GROUP), (FRIEND, GROUP), (OWNER, OTHER_GROUP)):
            self.assertIsNone(self.ask("$100", sender=sender, group=True, chat=chat))
        with patch.object(permissions, "OWNER_ID", "+15550000009"):
            self.assertIsNone(self.ask("$100", group=True))
        for gate in (self.fixture.enabled, self.fixture.owner_present):
            gate.return_value = False
            self.assertIsNone(self.ask("$100", group=True))
            gate.return_value = True
        self.fixture.rate_limit.return_value = False
        with patch.object(main._rate_limit_notice, "allow", return_value=True):
            self.assertIn("message limit", self.ask("$100", group=True))
        self.fixture.rate_limit.return_value = True
        self.fixture.clock.return_value = 401
        self.assertIsNone(self.ask("$100", group=True))
        self.assertEqual([], self.fixture.rows())

    def test_unrelated_discussion_and_cancellation_do_not_create_or_modify_saved_jobs(self):
        self.ask(COMPLETE, group=True, mention=True)
        original = self.fixture.rows()
        self.ask(MISSING_BUDGET, group=True, mention=True)
        for text in ("yes", "What about golf clubs?", "Why would the budget be $100?", "don't create it"):
            self.assertIsNone(self.ask(text, group=True))
        self.assertIn("Cancelled the new research cron draft", self.ask("never mind", group=True))
        self.assertIsNone(self.ask("$100", group=True))
        self.assertEqual(original, self.fixture.rows())
        self.ask("How do you create weekly research reports?", group=True, mention=True)
        self.assertEqual(original, self.fixture.rows())

    def test_private_actions_cross_chat_and_calendar_limits_survive_followups(self):
        for suffix in ("for another chat", "starting 2026-99-99", "every Monday and Friday", "at 8am ET"):
            self.ask("Create a weekly research report every Wednesday at 9am PT " + suffix)
            self.assertEqual([], self.fixture.rows())
        self.ask("Create a weekly research report every Wednesday at 9am PT")
        reply = self.ask("about private messages")
        self.assertIn("only read public", reply)
        self.assertEqual([], self.fixture.rows())

    def test_repeating_completed_draft_reuses_readback_and_failed_reply_is_not_success_history(self):
        self.ask(MISSING_BUDGET, group=True, mention=True)
        self.fixture.send.return_value = False
        self.assertIn("Saved research cron #1", self.ask("$100", group=True))
        self.history.assert_called_once()
        self.assertEqual("user", self.history.call_args.args[1])
        self.payload(recipient=GROUP)
        self.fixture.send.return_value = True
        self.ask(MISSING_BUDGET, group=True, mention=True)
        self.assertIn("Already saved research cron #1", self.ask("$100", group=True))
        self.payload(recipient=GROUP)
        self.assertEqual(["user", "assistant"], [call.args[1] for call in self.history.call_args_list])

    def test_ordinary_quote_draft_accepts_natural_same_owner_answer_without_new_mention(self):
        self.ask("create a new cron", group=True, mention=True, sender="(555) 000-0001")
        self.assertIn("scheduled", self.ask("I want an inspirational quote every morning at 8am", group=True))
        self.assertEqual(("08:00", "morning_message"), self.fixture.rows()[0][1:3])
        self.fixture.model.assert_not_called()

    def test_pending_research_answer_keeps_private_send_priority(self):
        for group in (False, True):
            for name in ("handle_private_send_confirmation", "handle_private_send_request"):
                with self.subTest(group=group, gate=name):
                    self.ask(MISSING_BUDGET, group=group, mention=group)
                    with patch.object(main, name, return_value="PRIVATE FLOW FIRST") as private:
                        self.assertEqual("PRIVATE FLOW FIRST", self.ask("$100", group=group))
                        private.assert_called_once()
                    self.assertEqual([], self.fixture.rows())

    def test_pending_handoff_skips_only_its_own_native_command_collision(self):
        text = "fantasy waiver wire report top 10 FAAB budget $100 every Friday at 8am"
        self.ask("create a new cron", group=True, mention=True)
        real_command = main.handle_group_command
        with patch.object(main, "handle_group_command", wraps=real_command) as command:
            self.assertIn("Fourth Down", self.ask("fantasy", group=True, mention=True))
            command.assert_called_once()
            command.reset_mock()
            self.assertIn("pong", self.ask("ping", group=True, mention=True))
            command.assert_called_once()
            command.reset_mock()
            self.assertIn("Saved research cron", self.ask(text, group=True))
            command.assert_not_called()
        self.payload(schedule="08:00 fri", recipient=GROUP)

    def test_missing_expired_other_actor_disabled_and_reset_keep_native_dispatch(self):
        text = "fantasy waiver wire report top 10 FAAB budget $100 every Friday at 8am"
        for scenario in ("missing", "expired", "other_actor", "disabled", "reset"):
            with self.subTest(scenario=scenario):
                if scenario != "missing":
                    self.ask("create a new cron", group=True, mention=True)
                if scenario == "expired":
                    self.fixture.clock.return_value += 301
                if scenario == "disabled":
                    self.fixture.enabled.return_value = False
                native_text = "persona reset" if scenario == "reset" else text
                sender = FRIEND if scenario == "other_actor" else OWNER
                with patch.object(main, "handle_group_command", return_value="NATIVE COMMAND") as command:
                    self.assertEqual("NATIVE COMMAND", self.ask(native_text, group=True, mention=True, sender=sender))
                    command.assert_called_once()
                self.fixture.enabled.return_value = True
                self.assertEqual([], self.fixture.rows())


if __name__ == "__main__":
    unittest.main()
