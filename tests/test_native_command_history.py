"""Native command results must be available to the next real chat turn."""

import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

from davosbot import commands, food_order, image_conversation, main, memory, permissions, tools
from davosbot.text_safety import normalize_bot_text


OWNER = "+15550000001"
ADMIN = "+15550000002"
FRIEND = "+15550000003"
GROUP = "0123456789abcdef0123456789abcdef"
OTHER_GROUP = "fedcba9876543210fedcba9876543210"


class NativeCommandHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db_path = str(Path(temporary.name) / "command-history.sqlite")
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript("""
                CREATE TABLE messages (id INTEGER PRIMARY KEY, sender TEXT, role TEXT, content TEXT, ts TEXT);
                CREATE TABLE bot_log (id INTEGER PRIMARY KEY, sender TEXT, event_type TEXT, payload TEXT);
                CREATE TABLE gemini_usage (
                    prompt_tokens INTEGER, candidates_tokens INTEGER, total_tokens INTEGER,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO gemini_usage (prompt_tokens, candidates_tokens, total_tokens)
                    VALUES (100, 20, 120);
            """)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for module in (main, memory, commands, tools, permissions):
            self.stack.enter_context(patch.object(module, "BOT_DB_PATH", self.db_path))
        for module in (main, commands, permissions):
            self.stack.enter_context(patch.object(module, "is_owner", lambda sender: sender == OWNER))
            self.stack.enter_context(patch.object(module, "is_admin", lambda sender: sender in (OWNER, ADMIN)))
        for name in ("is_owner_in_chat", "is_gc_enabled", "is_approved_user", "check_rate_limit"):
            self.stack.enter_context(patch.object(main, name, return_value=True))
        self.stack.enter_context(patch.object(commands, "is_approved_user", return_value=True))
        self.stack.enter_context(patch.object(main, "_is_rate_limited", return_value=False))
        for name in ("get_persona", "match_skill", "detect_user_fact", "decatur_behavior_fast_reply"):
            self.stack.enter_context(patch.object(main, name, return_value=None))
        for name in ("build_system_prompt", "build_light_chat_system_prompt"):
            self.stack.enter_context(patch.object(main, name, return_value="synthetic system"))
        for name in ("extract_and_update_memory", "update_heartbeat", "_log_message_trace", "_log_quality_signal", "log_session_error"):
            self.stack.enter_context(patch.object(main, name))
        self.errors = self.stack.enter_context(patch.object(main, "log_error"))
        self.stack.enter_context(patch.object(main, "handle_style_directive_message", return_value=None))
        self.stack.enter_context(patch.object(main, "format_style_directives_for_prompt", return_value=""))
        self.stack.enter_context(patch.object(main, "_log_owner_quality_intake_if_needed", return_value=None))
        self.confirmation = self.stack.enter_context(patch.object(main, "handle_private_send_confirmation", return_value=None))
        self.private_send = self.stack.enter_context(patch.object(main, "handle_private_send_request", return_value=None))
        self.stack.enter_context(patch.object(main, "_schedule_cron_from_text", return_value=None))
        self.stack.enter_context(patch.object(tools, "_edit_cron_from_text", return_value=None))
        self.stack.enter_context(patch.object(commands, "handle_club_command", return_value=None))
        self.stack.enter_context(patch.object(food_order, "handle_checkout_control", return_value=None))
        self.stack.enter_context(patch.dict(food_order._drafts, {}, clear=True))
        self.stack.enter_context(patch.object(image_conversation, "begin_message", return_value=False))
        self.stack.enter_context(patch.object(image_conversation, "path_for_followup", return_value=None))
        self.stack.enter_context(patch.dict(main._image_buffer, {}, clear=True))
        self.stack.enter_context(patch.dict(main._text_buffer, {}, clear=True))
        self.stack.enter_context(patch.object(main, "handle_market_query", return_value=None))
        self.model = self.stack.enter_context(patch.object(main, "get_response", return_value="The previous result explains the available option."))
        self.send = self.stack.enter_context(patch.object(main, "send_message", return_value=True))

    def route(self, text, *, sender=OWNER, chat=None):
        main.handle_message({"sender": sender, "chat_identifier": chat or sender, "text": text})
        self.errors.assert_not_called()

    def clear_history(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("DELETE FROM messages")
            conn.commit()
        self.send.reset_mock()
        self.model.reset_mock()

    def test_billing_real_command_result_reaches_followup(self):
        self.route("billing")
        self.model.assert_not_called()
        result = normalize_bot_text(self.send.call_args.args[1])
        self.assertIn("Gemini", result)
        self.assertIn("Input: 100 tokens", result)
        self.route("What does that total include?")
        self.model.assert_called_once()
        self.assertEqual([
            {"role": "user", "content": "billing"},
            {"role": "assistant", "content": result},
        ], self.model.call_args.args[1])

    def test_native_status_options_and_capability_routes_reach_followup(self):
        cases = (
            ("status", "_cmd_status"),
            ("model options", "_cmd_model_options"),
            ("Which model are you running?", "_cmd_model_status"),
            ("What can you do?", "_cmd_capabilities"),
            ("cleanup status", "_cmd_cleanup_status"),
        )
        for request, endpoint in cases:
            with self.subTest(request=request), patch.object(commands, endpoint, return_value="  Synthetic   status\n  Ready. "):
                self.clear_history()
                self.route(request)
                self.route("Explain that result.")
                self.assertEqual({"role": "assistant", "content": "Synthetic status\nReady."}, self.model.call_args.args[1][-1])

    def test_group_native_command_preserves_destination_and_speaker(self):
        with patch.object(commands, "_cmd_help", return_value="Synthetic help: image scan is available."):
            self.route("@Davos help", sender=FRIEND, chat=GROUP)
        self.assertEqual([
            {"role": "user", "content": f"{FRIEND}: help"},
            {"role": "assistant", "content": "Synthetic help: image scan is available."},
        ], memory.get_history(GROUP))
        self.route("@Davos explain that option.", sender=FRIEND, chat=GROUP)
        self.assertEqual("Synthetic help: image scan is available.", self.model.call_args.args[1][-1]["content"])
        self.assertEqual([], memory.get_history(FRIEND))
        self.assertEqual([], memory.get_history(OTHER_GROUP))

    def test_help_words_inside_real_tasks_reach_conversation(self):
        requests = (
            "Help pick crops for a farmer. What do you do?",
            "What can you do about a slice with my driver?",
            "How do I use this golf club?",
            "help me compare two golf clubs",
        )
        for sender, chat in ((OWNER, None), (OWNER, GROUP), (FRIEND, GROUP)):
            for request in requests:
                with self.subTest(sender=sender, chat=chat, request=request):
                    self.clear_history()
                    self.route(("@Davos " if chat else "") + request, sender=sender, chat=chat)
                    self.model.assert_called_once()
                    self.assertEqual(request, self.model.call_args.args[2])

    def test_direct_natural_help_still_returns_native_response(self):
        with patch.object(commands, "_cmd_help", return_value="Synthetic help overview."):
            self.route("@Davos how do I use this bot?", sender=FRIEND, chat=GROUP)
        self.model.assert_not_called()
        self.assertEqual("Synthetic help overview.", self.send.call_args.args[1])

    def test_admin_and_friend_help_keep_their_existing_scope(self):
        for sender in (ADMIN, FRIEND):
            with self.subTest(sender=sender), patch.object(commands, "_cmd_help", return_value="Synthetic tier help."):
                self.clear_history()
                self.route("help", sender=sender)
                self.route("Explain that option.", sender=sender)
                self.assertEqual("Synthetic tier help.", self.model.call_args.args[1][-1]["content"])
                self.assertEqual([], memory.get_history(OWNER))

    def test_persona_catalog_reaches_same_chat_followup(self):
        result = "Current: jarjar\nAvailable global: hansi flick, jarjar"
        for sender, chat in ((OWNER, None), (ADMIN, GROUP), (FRIEND, GROUP)):
            with self.subTest(sender=sender, chat=chat), patch.object(commands, "_persona_status", return_value=result):
                self.clear_history()
                prefix = "@Davos " if chat else ""
                self.route(prefix + "what other personalities do u have?", sender=sender, chat=chat)
                self.model.assert_not_called()
                self.route(prefix + "what is hansi flick?", sender=sender, chat=chat)
                self.model.assert_called_once()
                self.assertEqual({"role": "assistant", "content": result}, self.model.call_args.args[1][-1])
                self.assertEqual([], memory.get_history(OTHER_GROUP))
                if chat:
                    self.assertEqual([], memory.get_history(sender))

    def test_failed_persona_catalog_send_does_not_enter_context(self):
        self.send.return_value = False
        with patch.object(commands, "_persona_status", return_value="Visible synthetic catalog"):
            self.route("@Davos what personalities do you have?", sender=FRIEND, chat=GROUP)
        self.assertEqual([], memory.get_history(GROUP))
        self.send.assert_called_once()

    def test_failed_send_never_becomes_native_context(self):
        self.send.return_value = False
        self.route("billing")
        self.assertEqual([], memory.get_history(OWNER))
        self.send.assert_called_once()

    def test_history_failure_after_native_send_is_contained_without_second_send(self):
        for failed_call in (1, 2):
            with self.subTest(failed_call=failed_call):
                self.clear_history()
                calls = 0
                def save(*args):
                    nonlocal calls
                    calls += 1
                    if calls == failed_call:
                        raise RuntimeError("synthetic private database detail")
                    return memory.save_turn(*args)
                with patch.object(main, "save_turn", side_effect=save), patch.object(main.logger, "warning") as warning:
                    self.route("billing")
                self.send.assert_called_once()
                warning.assert_called_once()
                self.assertNotIn("synthetic private database detail", str(warning.call_args))

    def test_auth_private_send_and_sensitive_command_outputs_are_excluded(self):
        self.confirmation.return_value = "Synthetic authorization result."
        self.route("synthetic authorization token")
        self.assertEqual([], memory.get_history(OWNER))
        self.confirmation.return_value = None
        self.private_send.return_value = "Synthetic private-send confirmation."
        self.route("text +15550000009: confidential synthetic body")
        self.assertEqual([], memory.get_history(OWNER))
        self.private_send.return_value = None
        for command in ("logs", "memory notes", "admins", "fantasy requests", "log board", "model request chat synthetic", "personalities"):
            with self.subTest(command=command), patch.object(main, "handle_command", return_value="Synthetic sensitive or mutating output."):
                self.route(command)
                self.assertEqual([], memory.get_history(OWNER))

    def test_status_with_credential_is_excluded_and_output_secrets_are_redacted(self):
        with patch.object(permissions, "ADMIN_PASSWORD", "synthetic-private-passphrase"), patch.object(commands, "_cmd_model_status", return_value="Model: synthetic-private-passphrase"):
            self.route("Which model are you using? pw: synthetic-private-passphrase")
            self.assertEqual([], memory.get_history(OWNER))
            self.route("Which model are you using? password: synthetic-wrong-password")
            self.assertEqual([], memory.get_history(OWNER))
            self.route("model status")
            self.assertNotIn("synthetic-private-passphrase", str(memory.get_history(OWNER)))
            self.assertIn("[redacted]", memory.get_history(OWNER)[-1]["content"])

    def test_failed_model_output_is_not_assistant_history_for_any_chat_tier(self):
        for sender, chat in ((OWNER, None), (ADMIN, None), (FRIEND, None), (FRIEND, GROUP)):
            with self.subTest(sender=sender, chat=chat):
                self.clear_history()
                self.send.return_value = False
                self.route(("@Davos " if chat else "") + "Explain how rainbows form.", sender=sender, chat=chat)
                self.model.assert_called_once()
                self.assertFalse(any(row["role"] == "assistant" for row in memory.get_history(chat or sender)))


if __name__ == "__main__":
    unittest.main()
