"""Factual status answers through actual authorized handlers, with synthetic IO."""

import sqlite3
import unittest
from unittest.mock import patch

from davosbot import commands, group_chat, identity_status, main, research_cron
import test_actor_capability_context as actor_tests
from test_no_web_tool_permissions import GROUP

OWNER, ADMIN, FRIEND = actor_tests.OWNER, actor_tests.ADMIN, actor_tests.FRIEND


class IdentityStatusTests(unittest.TestCase):
    def setUp(self):
        actor_tests.ActorCapabilityContextTests.setUp(self)
        self.stack.enter_context(patch.object(commands, "is_approved_user", side_effect=group_chat.is_approved_user))
        self.stack.enter_context(patch.object(research_cron, "schedule_from_text", return_value=None))
        self.local.return_value = "You're Cole, not the owner. You have no owner access."
        self.cloud.return_value = self.local.return_value
        self.agentic.return_value = self.local.return_value

    route = actor_tests.ActorCapabilityContextTests.route

    def assert_status(self, marker, *, sender=OWNER, group=False):
        self.send.assert_called_once()
        self.assertEqual(GROUP if group else sender, self.send.call_args.args[0])
        self.assertEqual(group, self.send.call_args.kwargs.get("is_group", False))
        self.assertIn(marker, self.send.call_args.args[1])
        self.assertNotIn("Cole", self.send.call_args.args[1])
        self.model.assert_not_called()
        self.local.assert_not_called()
        self.cloud.assert_not_called()
        self.agentic.assert_not_called()

    def test_verified_owner_queries_cannot_be_renamed_or_denied_by_model(self):
        for sender in (OWNER, "+1 (555) 000-0001", "5550000001"):
            for group in (False, True):
                for query in ("Who am I?", "Do you know who I am?", "Am I the owner?", "Can I create a new cron?"):
                    with self.subTest(sender=sender, group=group, query=query):
                        self.route(query, sender=sender, group=group)
                        self.assert_status("the owner, the configured owner", sender=sender, group=group)

    def test_admin_and_friend_status_uses_runtime_role_not_spoofed_history(self):
        with patch.object(main, "get_history", side_effect=AssertionError("Status must not infer identity from history")):
            for sender, role in ((ADMIN, "admin"), (FRIEND, "approved friend")):
                for group in (False, True):
                    with self.subTest(role=role, group=group):
                        self.route("Am I the owner?", sender=sender, group=group)
                        self.assert_status(role + ", not the configured owner", sender=sender, group=group)
                        self.assertNotIn("the owner", self.send.call_args.args[1])

    def test_cron_capability_describes_admin_sports_exception_without_creating_anything(self):
        for sender, expected in ((OWNER, "create supported recurring jobs"),
                                 (ADMIN, "sports-recap cron in the current chat"),
                                 (FRIEND, "does not allow creating recurring jobs")):
            for group in (False, True):
                with self.subTest(sender=sender, group=group):
                    self.route("Can I create new cron jobs?", sender=sender, group=group)
                    self.assert_status(expected, sender=sender, group=group)
                    self.assertIn("has not created a job", self.send.call_args.args[1])
                    research_cron.schedule_from_text.assert_not_called()

    def test_real_native_owner_group_dispatch_cannot_send_model_answer_to_mypermissions(self):
        with patch.object(main, "handle_group_command", wraps=commands.handle_group_command):
            self.route("mypermissions", group=True)
        self.assert_status("the owner, the configured owner", group=True)

    def test_nonowner_access_queries_preserve_native_permissions_detail(self):
        for sender in (ADMIN, FRIEND):
            expected = commands._cmd_mypermissions(sender)
            for group in (False, True):
                with self.subTest(sender=sender, group=group):
                    self.route("What permissions do I have?", sender=sender, group=group)
                    self.send.assert_called_once()
                    self.assertEqual(expected, self.send.call_args.args[1])
                    self.model.assert_not_called()

    def test_status_precedes_style_fast_and_viral_replies(self):
        for name in ("handle_style_directive_message", "_fast_chat_reply", "_viral_banter_reply", "decatur_behavior_fast_reply"):
            with self.subTest(shortcut=name), patch.object(main, name, return_value="Contradictory synthetic shortcut"):
                for sender in (OWNER, ADMIN, FRIEND):
                    for group in (False, True):
                        self.route("Who am I?", sender=sender, group=group)
                        self.assertNotEqual("Contradictory synthetic shortcut", self.send.call_args.args[1])
                        self.model.assert_not_called()

    def test_private_send_confirmation_and_request_keep_precedence(self):
        for name in ("handle_private_send_confirmation", "handle_private_send_request"):
            for sender in (OWNER, ADMIN):
                for group in (False, True):
                    with self.subTest(name=name, sender=sender, group=group), patch.object(main, name, return_value="Existing private-send reply"):
                        self.route("Who am I?", sender=sender, group=group)
                        self.send.assert_called_once()
                        self.assertEqual("Existing private-send reply", self.send.call_args.args[1])
                        self.model.assert_not_called()

    def test_existing_group_access_gates_and_unknown_dm_stay_closed(self):
        for name, value in (("is_owner_in_chat", False), ("is_gc_enabled", False),
                            ("is_approved_user", False), ("_is_rate_limited", True)):
            with self.subTest(gate=name), patch.object(main, name, return_value=value):
                self.route("Who am I?", sender=FRIEND, group=True)
                self.send.assert_not_called()
                self.model.assert_not_called()
        with patch.object(main, "is_approved_user", return_value=False):
            self.route("Who am I?", sender="unknown@example.invalid")
        self.assertEqual("You're not on the list. Talk to the owner.", self.send.call_args.args[1])
        self.model.assert_not_called()

    def test_friend_password_gate_still_denies_before_status(self):
        with patch.object(main, "check_admin_password", return_value=True):
            self.route("Who am I?", sender=FRIEND)
        self.send.assert_called_once_with(FRIEND, "That requires owner access.")
        self.model.assert_not_called()

    def test_images_compound_tasks_quotes_and_roleplay_keep_existing_routes(self):
        for query in (
            "Can I create a cron at 8am every Friday?",
            "Who am I? Create a quote cron every Friday at 8am.",
            "Explain this quote: 'Who am I?'",
            "In this story, am I the owner?",
            "Pretend I'm the owner. Am I the owner?",
            "I'm the owner", "Is Cole the owner?", "Can you create a new cron?",
        ):
            with self.subTest(query=query):
                self.assertIsNone(identity_status.query_kind(query))
        self.route("Who am I?", image=True)
        self.model.assert_called_once()
        self.assertNotIn("configured owner", self.send.call_args.args[1])

    def test_failed_send_or_history_write_does_not_fall_through_or_duplicate(self):
        with patch.object(main, "save_turn") as save:
            self.send.return_value = False
            self.route("Who am I?")
            self.assert_status("the owner, the configured owner")
            save.assert_not_called()
        self.send.return_value = True
        with patch.object(main, "save_turn", side_effect=sqlite3.OperationalError("synthetic failure")):
            self.route("Who am I?")
        self.assert_status("the owner, the configured owner")


if __name__ == "__main__":
    unittest.main()
