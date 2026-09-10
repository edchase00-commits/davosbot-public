"""Real inbound routing must not interpret conversational on/off as controls."""

import unittest
from contextlib import ExitStack
from unittest.mock import patch

from davosbot import commands, main, permissions
import test_group_cron_creation_permissions as fixtures


class GroupControlIntentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.GroupCronCreationPermissionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.enable = stack.enter_context(patch.object(commands, "enable_gc"))
        self.disable = stack.enter_context(patch.object(commands, "disable_gc"))
        stack.enter_context(patch.object(commands, "is_approved_user", return_value=True))

    def ask(self, text, sender=fixtures.OWNER, chat=fixtures.GROUP):
        self.enable.reset_mock()
        self.disable.reset_mock()
        return self.fixture.ask(text, sender=sender, chat=chat)

    def assert_no_control(self):
        self.enable.assert_not_called()
        self.disable.assert_not_called()

    def test_complete_controls_keep_current_chat_and_native_single_reply(self):
        for text, enabled in (
            ("@Davos on", True), ("@DAVOS ON!", True), ("@Davos on.", True),
            ("@Davos off", False), ("@DAVOS OFF!", False), ("@Davos off.", False),
        ):
            with self.subTest(text=text):
                reply = self.ask(text, chat=fixtures.OTHER_GROUP)
                effect, untouched = (self.enable, self.disable) if enabled else (self.disable, self.enable)
                effect.assert_called_once_with(fixtures.OTHER_GROUP)
                untouched.assert_not_called()
                self.assertIn("I'm on." if enabled else "Going quiet.", reply)
                self.fixture.model.assert_not_called()

    def test_idioms_reach_conversation_with_original_request(self):
        for text in (
            "off the top of your head, what hybrid fits my golf bag?",
            "on a scale of 1 to 10, how strong is this lineup?",
            "on the other hand, compare a 4 hybrid with a 7 wood",
            "on second thought, compare the two options",
        ):
            with self.subTest(text=text):
                self.assertEqual("MODEL_FALLTHROUGH", self.ask("@Davos " + text))
                self.assert_no_control()
                self.assertEqual(text, self.fixture.model.call_args.args[2])

    def test_retractions_questions_and_compound_commands_do_not_mutate(self):
        for text in (
            "@Davos off? No, do not turn this chat off.",
            "@Davos on but don't change any settings",
            "@Davos off and then on",
            "@Davos on and explain cron jobs",
            "@Davos off?",
            "@Davos do not turn this chat off",
            "@Davos off\nActually, leave the group enabled",
        ):
            with self.subTest(text=text):
                self.ask(text)
                self.assert_no_control()

    def test_quoted_reported_and_code_examples_do_not_mutate(self):
        for text in (
            '"@Davos off"', "`@Davos on`", "> @Davos off",
            "Explain what `@Davos off` does",
            'My friend said "@Davos on" but leave this group alone',
            "@Davos review this example:\n```text\n@Davos off\n```",
            "@Davos on = true is the value in my sample code",
        ):
            with self.subTest(text=text):
                self.ask(text)
                self.assert_no_control()

    def test_nonowners_cannot_control_enabled_or_disabled_groups(self):
        for enabled in (True, False):
            self.fixture.enabled.return_value = enabled
            for sender in (fixtures.ADMIN, fixtures.FRIEND, fixtures.UNKNOWN):
                for text in ("@Davos on", "@Davos off"):
                    with self.subTest(enabled=enabled, sender=sender, text=text):
                        self.ask(text, sender=sender)
                        self.assert_no_control()

    def test_owner_can_reenable_disabled_group_but_idiom_cannot(self):
        self.fixture.enabled.return_value = False
        self.assertIn("I'm on.", self.ask("@Davos on"))
        self.enable.assert_called_once_with(fixtures.GROUP)
        self.fixture.model.assert_not_called()
        self.assertIsNone(self.ask("@Davos on a scale of 1 to 10, rate this"))
        self.assert_no_control()
        self.fixture.model.assert_not_called()

    def test_owner_presence_and_direct_address_requirements_are_unchanged(self):
        self.fixture.owner_present.return_value = False
        self.assertIsNone(self.ask("@Davos on"))
        self.assert_no_control()
        self.fixture.owner_present.return_value = True
        self.assertIsNone(self.ask("off"))
        self.assert_no_control()

    def test_normalized_verified_owner_handles_keep_existing_control(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(permissions, "OWNER_ID", fixtures.OWNER))
            for module in (main, commands, permissions):
                stack.enter_context(patch.object(module, "is_owner", fixtures.REAL_IS_OWNER))
                stack.enter_context(patch.object(module, "is_admin", fixtures.REAL_IS_ADMIN))
            for sender in (fixtures.OWNER, "15550000001", "5550000001", "(555) 000-0001"):
                with self.subTest(sender=sender):
                    self.assertIn("I'm on.", self.ask("@Davos on", sender=sender))
                    self.enable.assert_called_once_with(fixtures.GROUP)
                    self.disable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
