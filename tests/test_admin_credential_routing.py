"""Actual DM dispatch with synthetic credentials, actors, providers and IO."""

import unittest
from unittest.mock import patch

import test_actor_capability_context as actor_fixture
from davosbot import commands, cron_creation, main, permissions, request_context, tools


OWNER = actor_fixture.OWNER
ADMIN = actor_fixture.ADMIN
FRIEND = actor_fixture.FRIEND
CREDENTIAL = "synthetic-credential-unit-only"


class AdminCredentialRoutingTests(unittest.TestCase):
    def setUp(self):
        self.f = actor_fixture.ActorCapabilityContextTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.stack.enter_context(patch.object(permissions, "ADMIN_PASSWORD", CREDENTIAL))
        self.check = self.f.stack.enter_context(patch.object(main, "check_admin_password", side_effect=permissions.check_admin_password))
        self.strip = self.f.stack.enter_context(patch.object(main, "strip_password", side_effect=permissions.strip_password))
        self.logger = self.f.stack.enter_context(patch.object(main, "logger"))
        self.f.stack.enter_context(patch.dict(cron_creation._pending, {}, clear=True))

    def route(self, text, *, sender=ADMIN):
        main.save_turn.reset_mock()
        self.strip.reset_mock()
        self.f.route(text, sender=sender)

    def assert_local_only(self):
        self.f.send.assert_called_once()
        self.f.model.assert_not_called()
        main.save_turn.assert_not_called()
        self.assertEqual(ADMIN, self.f.send.call_args.args[0])
        self.assertNotIn(CREDENTIAL, self.f.send.call_args.args[1])
        self.assertNotIn(CREDENTIAL, str(self.logger.mock_calls))

    def test_bare_or_labeled_credentials_get_one_finite_local_reply(self):
        for text in (CREDENTIAL, "pw: " + CREDENTIAL, "password: " + CREDENTIAL):
            with self.subTest(form=text.split(":", 1)[0] if ":" in text else "bare"):
                self.route(text)
                self.assert_local_only()
                self.strip.assert_called_once()
                self.assertIn("doesn't change your admin access", self.f.send.call_args.args[1])

    def test_unstrippable_punctuation_credential_fails_closed_before_model(self):
        for credential in (CREDENTIAL + "!", "!" + CREDENTIAL):
            with self.subTest(ending=credential.endswith("!")), patch.object(permissions, "ADMIN_PASSWORD", credential):
                self.route("What does this mean for me? " + credential)
                self.assert_local_only()
                self.strip.assert_called_once()
                self.assertIn("Resend the task without a password", self.f.send.call_args.args[1])

    def test_clean_task_reaches_provider_once_with_verified_admin_role(self):
        self.route("What does this mean for me? pw: " + CREDENTIAL)
        self.strip.assert_called_once()
        self.f.local.assert_called_once()
        context, _ = request_context.split_request_context(self.f.local.call_args.args[0])
        self.assertIn("Verified current requester: admin", context)
        self.assertNotIn("owner (the owner)", context)
        self.assertNotIn(CREDENTIAL, str(self.f.local.call_args))
        self.assertNotIn(CREDENTIAL, str(main.save_turn.mock_calls))
        self.assertFalse(self.f.model.call_args.kwargs.get("use_tools", False))
        self.f.send.assert_called_once()

    def test_embedded_duplicate_configured_credential_is_not_forwarded(self):
        self.route("What does X" + CREDENTIAL + "Y mean? pw: " + CREDENTIAL)
        self.assert_local_only()
        self.strip.assert_called_once()
        self.assertIn("Resend the task without a password", self.f.send.call_args.args[1])

    def test_nonmatching_credential_does_not_enter_new_credential_branch(self):
        wrong = "synthetic-nonmatching-unit-only"
        self.route("What does this mean for me? pw: " + wrong)
        self.strip.assert_not_called()
        self.f.local.assert_called_once()
        context, _ = request_context.split_request_context(self.f.local.call_args.args[0])
        self.assertIn("Verified current requester: admin", context)
        self.assertNotIn(wrong, self.f.send.call_args.args[1])
        # Existing nonmatching-input handling is unchanged; no new error echoes
        # the supplied value or claims its author gained owner permissions.

    def test_pending_private_confirmation_gets_original_input_before_stripping(self):
        text = "confirm pw: " + CREDENTIAL
        with patch.object(main, "handle_private_send_confirmation", return_value="Synthetic confirmation result") as confirm:
            self.route(text)
        confirm.assert_called_once_with(ADMIN, text, allow_password=True)
        self.strip.assert_not_called()
        self.f.model.assert_not_called()
        self.f.send.assert_called_once_with(ADMIN, "Synthetic confirmation result")

    def test_private_request_keeps_same_admin_and_current_dm(self):
        task = "text synthetic-contact hello"
        with patch.object(main, "handle_private_send_request", return_value="Synthetic private request result") as request:
            self.route(task + " pw: " + CREDENTIAL)
        request.assert_called_once_with(ADMIN, task, originating_chat_id=ADMIN)
        self.f.model.assert_not_called()
        self.f.send.assert_called_once_with(ADMIN, "Synthetic private request result")

    def test_owner_commands_stay_denied_without_external_mutation(self):
        for task in ("pull", "grant " + FRIEND, "memory add synthetic fact"):
            with self.subTest(task=task), patch.object(commands.subprocess, "run") as process, patch.object(commands.sqlite3, "connect") as database:
                self.route(task + " pw: " + CREDENTIAL)
                process.assert_not_called()
                database.assert_not_called()
                self.f.model.assert_not_called()
                self.assertIn("the owner-only", self.f.send.call_args.args[1])

    def test_quote_creation_stays_owner_only_but_admin_sports_draft_still_works(self):
        with patch.object(main, "_schedule_cron_from_text", side_effect=tools._schedule_cron_from_text), patch.object(main, "_sports_recap_cron_from_text", side_effect=tools._sports_recap_cron_from_text), patch.object(tools, "_schedule_cron") as schedule:
            self.route("create a quote cron at 8am pw: " + CREDENTIAL)
            self.assertIn("the owner-only", self.f.send.call_args.args[1])
            self.assertIsNone(cron_creation.pending_draft(ADMIN, ADMIN))
            self.route("create a sports recap cron pw: " + CREDENTIAL)
            self.assertEqual("sports_recap", cron_creation.pending_draft(ADMIN, ADMIN)["action"])
            schedule.assert_not_called()
            self.f.model.assert_not_called()

    def test_shell_request_does_not_offer_owner_tools(self):
        self.route("Run a shell command to list project files pw: " + CREDENTIAL)
        self.f.model.assert_called_once()
        self.assertFalse(self.f.model.call_args.kwargs.get("use_tools", False))
        self.assertNotIn("shell_exec", self.f.model.call_args.kwargs.get("allowed_tools") or [])
        self.assertEqual(ADMIN, self.f.model.call_args.kwargs["sender"])

    def test_owner_and_friend_dispatch_roles_are_unchanged(self):
        self.route("who am I?", sender=OWNER)
        self.assertIn("the owner", self.f.send.call_args.args[1])
        self.strip.assert_not_called()
        self.route(CREDENTIAL, sender=FRIEND)
        self.assertIn("owner access", self.f.send.call_args.args[1])
        self.strip.assert_not_called()
        self.f.model.assert_not_called()

    def test_failed_local_send_is_not_retried_or_passed_to_model(self):
        for failure in (False, RuntimeError("synthetic send failure")):
            with self.subTest(raised=isinstance(failure, Exception)), patch.object(main, "send_message", side_effect=failure if isinstance(failure, Exception) else None, return_value=False) as send:
                if isinstance(failure, Exception):
                    with self.assertRaises(RuntimeError):
                        self.route(CREDENTIAL)
                else:
                    self.route(CREDENTIAL)
                send.assert_called_once()
                self.f.model.assert_not_called()
                main.save_turn.assert_not_called()

    def test_help_does_not_promise_password_based_owner_elevation(self):
        with patch.object(commands, "list_personas", return_value=["default"]):
            reply = commands._cmd_help(OWNER)
        self.assertIn("Password text never changes", reply)
        self.assertNotIn("routed as owner", reply)
        self.assertNotIn("admins can elevate", reply)


if __name__ == "__main__":
    unittest.main()
