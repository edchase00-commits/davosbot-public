"""Fantasy topic prose must retain its request through real inbound dispatch."""

from contextlib import ExitStack
import unittest
from unittest.mock import patch

from davosbot import commands, dashboard_links, research_cron
import test_research_cron_drafts as fixtures
import test_group_cron_creation_permissions as actors


class FantasyIntentRoutingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ResearchCronDraftTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(commands, "FANTASY_DASHBOARD_URL", "https://fantasy.example.test"))
        stack.enter_context(patch.object(commands, "is_approved_user", lambda sender: sender in {actors.OWNER, actors.ADMIN, actors.FRIEND}))
        self.access = {
            name: stack.enter_context(patch.object(commands.fantasy_access, name))
            for name in ("list_access", "grant_access", "set_access_role", "revoke_access", "request_access")
        }

    def ask(self, text, *, group=False, sender=actors.OWNER, chat=None):
        return self.fixture.ask(text, group=group, mention=group, sender=sender, chat=chat)

    def assert_no_access_call(self):
        for method in self.access.values():
            method.assert_not_called()

    def test_fantasy_topic_requests_reach_model_intact_for_existing_roles(self):
        for group in (False, True):
            for sender in (actors.OWNER, actors.ADMIN, actors.FRIEND):
                for text in (
                    "fantasy football waiver advice: compare two available receivers",
                    "fantasy lineup question: should I prefer the safer floor?",
                    "fantasy request advice about picking a quarterback",
                ):
                    with self.subTest(group=group, sender=sender, text=text):
                        self.assertEqual("MODEL_FALLTHROUGH", self.ask(text, group=group, sender=sender))
                        self.assertEqual(text, self.fixture.fixture.model.call_args.args[2])
                        self.assert_no_access_call()
                        self.assertEqual([], self.fixture.fixture.rows())

    def test_unprefixed_report_details_do_not_implicitly_create_a_schedule(self):
        text = "fantasy waiver report every Wednesday at 12:59 AM with top 10 players and $100 FAAB"
        self.assertFalse(research_cron.is_creation_request(text))
        for group in (False, True):
            with self.subTest(group=group):
                self.assertEqual("MODEL_FALLTHROUGH", self.ask(text, group=group))
                self.assertEqual([], self.fixture.fixture.rows())
                self.assert_no_access_call()

    def test_complete_explicit_research_requests_keep_original_native_creation(self):
        for group in (False, True):
            with self.subTest(group=group):
                self.assertIn("Saved research cron", self.ask(fixtures.COMPLETE, group=group))
                self.fixture.fixture.model.assert_not_called()
                self.assert_no_access_call()
        rows = self.fixture.fixture.rows()
        self.assertEqual(2, len(rows))
        self.assertEqual([("00:59 wed", "research_report")] * 2, [row[1:3] for row in rows])

    def test_existing_generic_cron_handoff_still_works_without_second_mention(self):
        self.assertIn("research/waiver", self.ask("create a weekly cron at 8am", group=True))
        self.assertIn("weekday", self.fixture.ask("fantasy waiver report top 10 FAAB budget $100", group=True))
        self.assertIn("Saved research cron", self.fixture.ask("Friday", group=True))
        self.fixture.payload(schedule="08:00 fri", recipient=actors.GROUP)
        self.assert_no_access_call()

    def test_link_requests_preserve_current_group_signup_and_owner_dm_boundary(self):
        for text in ("fantasy", "Fourth Down", "show me the fantasy dashboard"):
            with self.subTest(text=text):
                self.assertIn("https://fantasy.example.test", self.ask(text))
                self.assertIn("owner-only", self.ask(text, sender=actors.ADMIN))
                for sender in (actors.OWNER, actors.ADMIN, actors.FRIEND, actors.UNKNOWN):
                    self.assertIn("https://fantasy.example.test", self.ask(text, group=True, sender=sender))
                self.assert_no_access_call()

    def test_group_legacy_signup_and_access_commands_never_call_access_api(self):
        for text in ("fantasy request", "fantasy request tester@example.com", "fantasy access",
                     "fantasy grant #7 viewer", "fantasy revoke #7"):
            with self.subTest(text=text):
                reply = self.ask(text, group=True, sender=actors.FRIEND)
                self.assertIn("https://fantasy.example.test", reply)
                self.assertNotIn("tester@example.com", reply)
                self.assert_no_access_call()

    def test_exact_owner_dm_access_commands_keep_their_existing_executor(self):
        self.access["list_access"].return_value = {"members": []}
        self.access["grant_access"].return_value = {"member": {"id": 7, "role": "viewer"}}
        self.access["set_access_role"].return_value = {"member": {"id": 7, "role": "editor"}}
        self.access["revoke_access"].return_value = {"member": {"id": 7}}
        for text in ("fantasy requests", "fantasy request list", "fantasy access", "fantasy access list", "fantasy users", "fantasy members"):
            self.ask(text)
            self.fixture.fixture.model.assert_not_called()
        self.assertEqual(6, self.access["list_access"].call_count)
        self.ask("fantasy grant #7 viewer")
        self.access["grant_access"].assert_called_once_with(7, "viewer")
        self.ask("fantasy promote #7 editor")
        self.ask("fantasy role #7 editor")
        self.assertEqual(2, self.access["set_access_role"].call_count)
        self.ask("fantasy revoke #7")
        self.access["revoke_access"].assert_called_once_with(7)

    def test_quoted_negated_or_compound_access_mentions_are_not_native_commands(self):
        for text in ('"fantasy grant #7 owner"', "`fantasy revoke #7`", "don't fantasy revoke #7",
                     "fantasy grant #7 viewer and revoke #8", "fantasy grant #7 owner but do not change access",
                     "fantasy requests are confusing; explain how to sign in"):
            with self.subTest(text=text):
                self.assertFalse(dashboard_links.is_fantasy_command(text, is_group=True))
                self.assertEqual("MODEL_FALLTHROUGH", self.ask(text, group=True))
                self.assert_no_access_call()

    def test_disabled_group_and_unapproved_topic_access_are_unchanged(self):
        text = "fantasy football advice about a receiver"
        self.assertIsNone(self.ask(text, group=True, sender=actors.UNKNOWN))
        self.fixture.fixture.enabled.return_value = False
        self.assertIsNone(self.ask(text, group=True))
        self.assertIsNone(self.ask("fantasy", group=True, sender=actors.FRIEND))
        self.assertIn("https://fantasy.example.test", self.ask("fantasy", group=True))
        self.assert_no_access_call()


if __name__ == "__main__":
    unittest.main()
