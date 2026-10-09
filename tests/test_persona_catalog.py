import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from davosbot import commands, main, personality


OWNER = "+15550000001"
ADMIN = "+15550000002"
FRIEND = "+15550000003"
UNKNOWN = "+15550000004"
GROUP = "a" * 32


class PersonaCatalogTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        (directory / "jarjar.md").write_text("Jar Jar voice", encoding="utf-8")
        (directory / "hansi flick.md").write_text("Football manager voice", encoding="utf-8")
        (directory / "_private mode.md").write_text("Hidden voice", encoding="utf-8")
        (directory / "gruden.md").write_text("Hidden by name", encoding="utf-8")
        self.stack.enter_context(patch.object(personality, "_PERSONAS_DIR", directory))
        self.stack.enter_context(patch.object(commands, "is_owner", side_effect=lambda sender: sender == OWNER))
        self.stack.enter_context(patch.object(commands, "is_approved_user", side_effect=lambda sender: sender in {ADMIN, FRIEND}))
        self.stack.enter_context(patch.object(commands, "is_admin", side_effect=lambda sender: sender in {OWNER, ADMIN}))
        self.current = self.stack.enter_context(patch.object(commands, "get_persona", return_value="jarjar"))
        self.stack.enter_context(patch.object(commands, "list_group_personas", side_effect=lambda context: [{"name": "Sideline"}] if context == GROUP else []))
        self.set_persona = self.stack.enter_context(patch.object(commands, "set_persona"))
        self.clear_history = self.stack.enter_context(patch.object(commands, "clear_history"))

    def assert_catalog(self, reply):
        self.assertIn("Current: jarjar", reply)
        self.assertIn("Available global: hansi flick, jarjar", reply)
        self.assertNotIn("private mode", reply)
        self.assertNotIn("gruden", reply)
        self.set_persona.assert_not_called()
        self.clear_history.assert_not_called()

    def test_real_reported_group_question_uses_catalog_for_each_approved_role(self):
        for sender in (OWNER, ADMIN, FRIEND):
            with self.subTest(sender=sender):
                reply = commands.handle_group_command(sender, GROUP, "@davos what other personalities do u have?")
                self.assert_catalog(reply)
                self.assertIn("This chat: Sideline", reply)

    def test_natural_questions_and_explicit_catalog_commands_are_read_only(self):
        for query in (
            "persona", "persona list", "persona options", "personalities",
            "what personas are available?", "which personalities can you use",
            "what are your other personas?", "show me your personalities please",
            "what is your current personality?", "which persona are you using?",
        ):
            with self.subTest(query=query):
                self.assert_catalog(commands.handle_group_command(FRIEND, GROUP, "@Davos " + query))

    def test_switch_and_edit_requests_do_not_match_or_gain_authority(self):
        for query in (
            "persona jarjar", "switch persona to jarjar", "create a persona",
            "what personalities do you have and switch to jarjar",
            "show me your personalities and reset them", "change your personality",
            "why did you change personalities?", "what is hansi flick",
        ):
            with self.subTest(query=query):
                self.assertFalse(commands._is_persona_catalog_request(query))
                self.assertIsNone(commands.handle_group_command(FRIEND, GROUP, "@Davos " + query))
        self.set_persona.assert_not_called()
        self.clear_history.assert_not_called()

    def test_unknown_member_does_not_get_catalog(self):
        with patch.object(commands, "_persona_status") as status:
            self.assertIsNone(commands.handle_group_command(UNKNOWN, GROUP, "@davos what personalities do you have"))
        status.assert_not_called()

    def test_hidden_current_persona_and_other_chat_personas_are_not_exposed(self):
        self.current.return_value = "private mode"
        reply = commands.handle_group_command(FRIEND, "b" * 32, "@Davos personas")
        self.assertIn("Current: hidden persona", reply)
        self.assertNotIn("private mode", reply)
        self.assertNotIn("gruden", reply)
        self.assertNotIn("Sideline", reply)

    def test_owner_dm_uses_existing_catalog_without_expanding_dm_permission(self):
        with patch.object(commands, "handle_club_command", return_value=None):
            reply = commands.handle_command(OWNER, "what other personalities do u have?")
            self.assert_catalog(reply)
            self.assertNotIn("Sideline", reply)
            self.current.assert_called_with("dm")
            for sender in (ADMIN, FRIEND, UNKNOWN):
                with self.subTest(sender=sender), patch.object(commands, "_persona_status") as status:
                    commands.handle_command(sender, "what other personalities do u have?")
                    status.assert_not_called()

    def test_exact_owner_dm_personalities_keeps_file_validation_command(self):
        with (
            patch.object(commands, "handle_club_command", return_value=None),
            patch.object(commands, "_cmd_personalities", return_value="Synthetic validation report") as validate,
            patch.object(commands, "_persona_status") as status,
        ):
            self.assertEqual("Synthetic validation report", commands.handle_command(OWNER, "personalities"))
        validate.assert_called_once_with(OWNER)
        status.assert_not_called()

    def test_enabled_group_native_dispatch_avoids_model_and_keeps_group_destination(self):
        with (
            patch.object(main, "is_owner_in_chat", return_value=True),
            patch.object(main, "is_gc_enabled", return_value=True),
            patch.object(main, "is_owner", return_value=False),
            patch.object(main, "save_turn"),
            patch.object(main, "get_response", side_effect=AssertionError("Catalog must not use a model")),
            patch.object(main, "send_message", return_value=True) as send,
        ):
            main.handle_group_message(FRIEND, GROUP, "@Davos what other personalities do u have?")
        self.assertEqual(GROUP, send.call_args.args[0])
        self.assertTrue(send.call_args.kwargs["is_group"])
        self.assert_catalog(send.call_args.args[1])

    def test_disabled_group_stays_silent_for_nonowner(self):
        with (
            patch.object(main, "is_owner_in_chat", return_value=True),
            patch.object(main, "is_gc_enabled", return_value=False),
            patch.object(main, "is_owner", return_value=False),
            patch.object(main, "handle_group_command") as command,
            patch.object(main, "get_response") as model,
            patch.object(main, "send_message") as send,
        ):
            main.handle_group_message(FRIEND, GROUP, "@Davos what other personalities do u have?")
        command.assert_not_called()
        model.assert_not_called()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
