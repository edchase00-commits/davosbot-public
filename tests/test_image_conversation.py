import base64
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from davosbot import image_conversation, main


OWNER = "+15550000001"
FRIEND = "+15550000002"
ADMIN = "+15550000003"
GROUP = "0123456789abcdef0123456789abcdef"
OTHER_GROUP = "abcdef0123456789abcdef0123456789"


class ImageConversationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.image = Path(self.tmp.name) / "synthetic.png"
        self.image.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aJ1sAAAAASUVORK5CYII="
        ))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in (
            "_handle_priority_intake_command", "_handle_self_status_question", "_handle_model_status_question",
            "_handle_natural_model_request", "_handle_screenshot_issue_log", "_handle_image_capability_status",
            "handle_private_send_confirmation", "handle_private_send_request", "_describe_cron_from_text",
            "_cancel_cron_from_text", "_schedule_cron_from_text", "_sports_recap_cron_from_text",
            "_fast_chat_reply", "_market_fast_reply", "_viral_banter_reply",
            "handle_style_directive_message", "decatur_behavior_fast_reply", "_log_owner_quality_intake_if_needed",
            "_complex_analysis_preflight_reply", "_log_group_error_intake_if_needed", "handle_group_persona_editor_command",
            "detect_user_fact", "match_skill",
        ):
            self.stack.enter_context(patch.object(main, name, return_value=None))
        for name in ("is_ufc_fight_card_request", "check_admin_password", "detect_reminder_edit_intent",
                     "classify_cron_list_intent", "looks_like_tone_feedback", "_is_rate_limited", "detect_capability_gap"):
            self.stack.enter_context(patch.object(main, name, return_value=False))
        self.stack.enter_context(patch.object(main, "classify_reminder_intent", return_value="none"))
        self.stack.enter_context(patch.object(main, "get_persona", return_value=None))
        self.stack.enter_context(patch.object(main, "is_owner", lambda sender: sender == OWNER))
        self.stack.enter_context(patch.object(main, "is_admin", lambda sender: sender in {OWNER, ADMIN}))
        self.stack.enter_context(patch.object(main, "is_approved_user", lambda sender: sender in {OWNER, ADMIN, FRIEND}))
        self.stack.enter_context(patch.object(main, "is_owner_in_chat", return_value=True))
        self.stack.enter_context(patch.object(main, "is_gc_enabled", return_value=True))
        self.stack.enter_context(patch.object(main, "get_history", return_value=[]))
        self.stack.enter_context(patch.object(main, "get_tool_uses_today", return_value=0))
        self.stack.enter_context(patch.object(main, "format_style_directives_for_prompt", return_value=""))
        self.stack.enter_context(patch.object(main, "build_system_prompt", return_value="Test prompt"))
        self.stack.enter_context(patch.object(main, "build_light_chat_system_prompt", return_value="Test prompt"))
        self.stack.enter_context(patch.object(main, "extract_and_update_memory"))
        self.stack.enter_context(patch.object(main, "enforce_decatur_behavior_reply", side_effect=lambda reply, *args: reply))
        self.stack.enter_context(patch.object(main, "_image_buffer", {}))
        self.stack.enter_context(patch.object(main, "_text_buffer", {}))
        self.stack.enter_context(patch.object(image_conversation, "_recent", {}))
        self.clock = self.stack.enter_context(patch.object(image_conversation.time, "monotonic", return_value=100.0))
        self.scan = self.stack.enter_context(patch.object(main, "scan_image", return_value=SimpleNamespace(
            ok=True, api_called=True, message="The synthetic image is bright.", provider="test",
        )))
        self.stack.enter_context(patch.object(main, "choose_scan_provider", return_value="gemini"))
        self.stack.enter_context(patch.object(main, "estimate_scan_time", return_value="a moment"))
        self.denial = self.stack.enter_context(patch.object(main, "image_access_denial", return_value=None))
        self.usage = self.stack.enter_context(patch.object(main, "log_tool_use"))
        self.send = self.stack.enter_context(patch.object(main, "send_message", return_value=True))
        self.saved = self.stack.enter_context(patch.object(main, "save_turn"))
        self.chat = self.stack.enter_context(patch.object(main, "get_response", return_value="Regular chat reply."))
        self.discovery = self.stack.enter_context(patch.object(main, "find_recent_image_attachment", return_value=None))
        self.generation = self.stack.enter_context(patch.object(main, "_start_image_generation_job", return_value="Generating test image."))
        self.stack.enter_context(patch.object(main, "handle_command", side_effect=lambda sender, text: "Status OK" if text == "status" else None))
        self.stack.enter_context(patch.object(main, "handle_group_command", return_value=None))

    def route(self, text, sender=OWNER, chat=None, attach=False):
        if chat:
            main.handle_group_message(sender, chat, "@Davos " + text, msg={"image_path": str(self.image)} if attach else {})
        else:
            main.handle_dm(sender, text, image_path=str(self.image) if attach else None)

    def test_image_only_scans_in_each_allowed_dm_and_mentioned_group(self):
        for sender in (OWNER, ADMIN, FRIEND):
            for chat in (None, GROUP):
                with self.subTest(sender=sender, chat=chat):
                    self.scan.reset_mock()
                    self.usage.reset_mock()
                    self.route("", sender, chat, attach=True)
                    self.scan.assert_called_once()
                    self.assertEqual(str(self.image), self.scan.call_args.args[0])
                    self.assertIn("quick read", self.scan.call_args.args[1])
                    self.usage.assert_called_once_with(sender, main.OPENAI_IMAGE_SCAN_TOOL)
        self.chat.assert_not_called()

    def test_freeform_caption_uses_guarded_scan_for_all_allowed_tiers(self):
        for sender in (OWNER, ADMIN, FRIEND):
            for chat in (None, GROUP):
                with self.subTest(sender=sender, chat=chat):
                    self.scan.reset_mock()
                    self.usage.reset_mock()
                    self.route("Golden sunset over the mountains", sender, chat, attach=True)
                    self.scan.assert_called_once()
                    self.assertIn("Golden sunset", self.scan.call_args.args[1])
                    self.usage.assert_called_once_with(sender, main.OPENAI_IMAGE_SCAN_TOOL)
        self.chat.assert_not_called()

    def test_caption_cannot_bypass_revocation_or_quota_via_generic_vision(self):
        for denial in ("Image access is turned off for you right now.", "Image limit reached (5/5 today)."):
            self.denial.return_value = denial
            for sender in (ADMIN, FRIEND):
                for chat in (None, GROUP):
                    with self.subTest(denial=denial, sender=sender, chat=chat):
                        self.route("Golden sunset over the mountains", sender, chat, attach=True)
                        self.assertEqual(denial, self.send.call_args.args[1])
        self.scan.assert_not_called()
        self.chat.assert_not_called()
        self.usage.assert_not_called()

    def test_followup_reuses_file_and_previous_scan_context_in_same_chat(self):
        self.route("read this", chat=GROUP, attach=True)
        self.route("what do you think?", chat=GROUP)
        self.assertEqual(2, self.scan.call_count)
        path, prompt = self.scan.call_args.args
        self.assertEqual(str(self.image), path)
        self.assertIn("Current follow-up: what do you think?", prompt)
        self.assertIn("Previous question: read this", prompt)
        self.assertIn("The synthetic image is bright.", prompt)
        self.assertEqual(GROUP, self.send.call_args.args[0])
        self.assertTrue(self.send.call_args.kwargs["is_group"])
        self.discovery.assert_not_called()

    def test_followup_rechecks_access_and_counts_each_scan_attempt(self):
        self.route("read this", sender=FRIEND, attach=True)
        self.denial.return_value = "Image limit reached (5/5 today)."
        self.route("what do you think?", sender=FRIEND)
        self.scan.assert_called_once()
        self.usage.assert_called_once_with(FRIEND, main.OPENAI_IMAGE_SCAN_TOOL)
        self.assertIn("Image limit reached", self.send.call_args.args[1])

    def test_context_never_crosses_sender_chat_or_expiry(self):
        self.route("read this", chat=GROUP, attach=True)
        self.route("thoughts?", sender=FRIEND, chat=GROUP)
        self.route("thoughts?", chat=OTHER_GROUP)
        self.route("thoughts?")
        self.clock.return_value = 401
        self.route("thoughts?", chat=GROUP)
        self.scan.assert_called_once()
        self.discovery.assert_not_called()
        for call in self.chat.call_args_list:
            self.assertIsNone(call.kwargs.get("image_path"))

    def test_missing_previous_file_reports_unreadable_without_guessing(self):
        self.route("read this", attach=True)
        self.image.unlink()
        self.route("what do you think?")
        self.scan.assert_called_once()
        self.chat.assert_not_called()
        self.assertIn("not readable", self.send.call_args.args[1])
        self.assertIsNone(image_conversation.get(OWNER, OWNER))

    def test_unrelated_topic_ends_previous_image_discussion(self):
        self.route("read this", attach=True)
        self.route("status")
        self.route("what do you think?")
        self.scan.assert_called_once()
        self.discovery.assert_not_called()
        self.assertIsNone(self.chat.call_args.kwargs.get("image_path"))

    def test_image_only_does_not_replay_unrelated_previous_text(self):
        main._text_buffer[OWNER] = {"text": "buy wings tomorrow", "ts": main.time.time()}
        self.route("", attach=True)
        self.assertIn("quick read", self.scan.call_args.args[1])
        self.assertNotIn("buy wings", self.scan.call_args.args[1])

    def test_image_only_pairs_with_referential_pretext(self):
        main._text_buffer[OWNER] = {"text": "what do you think?", "ts": main.time.time()}
        self.route("", attach=True)
        self.assertIn("1-2 short sentences", self.scan.call_args.args[1])
        self.assertIn("what do you think?", self.scan.call_args.args[1])

    def test_unmentioned_group_image_is_silent_then_same_sender_can_ask(self):
        main.handle_group_message(FRIEND, GROUP, "", msg={"image_path": str(self.image)})
        self.scan.assert_not_called()
        self.send.assert_not_called()
        self.route("what do you think?", sender=FRIEND, chat=GROUP)
        self.scan.assert_called_once()

    def test_unmentioned_image_does_not_hijack_unrelated_mentioned_text(self):
        main.handle_group_message(FRIEND, GROUP, "", msg={"image_path": str(self.image)})
        self.route("tell me a joke", sender=FRIEND, chat=GROUP)
        self.route("what do you think?", sender=FRIEND, chat=GROUP)
        self.scan.assert_not_called()
        self.assertIsNone(self.chat.call_args.kwargs.get("image_path"))

    def test_group_access_gates_remain_before_image_scan(self):
        with patch.object(main, "is_owner_in_chat", return_value=False):
            self.route("read this", chat=GROUP, attach=True)
        with patch.object(main, "is_gc_enabled", return_value=False):
            self.route("read this", sender=FRIEND, chat=GROUP, attach=True)
        self.route("read this", sender="unknown", chat=GROUP, attach=True)
        self.scan.assert_not_called()
        self.chat.assert_not_called()

    def test_context_cache_is_bounded_and_discards_oldest(self):
        for index in range(image_conversation._MAX_CONTEXTS + 1):
            image_conversation.remember(str(index), GROUP, str(self.image), "q" * 2000, "a" * 4000)
        self.assertEqual(image_conversation._MAX_CONTEXTS, len(image_conversation._recent))
        self.assertIsNone(image_conversation.get("0", GROUP))
        latest = image_conversation.get(str(image_conversation._MAX_CONTEXTS), GROUP)
        self.assertEqual(800, len(latest.question))
        self.assertEqual(1400, len(latest.answer))

    def test_existing_command_with_attachment_still_reaches_command(self):
        self.route("status", attach=True)
        self.scan.assert_not_called()
        self.assertEqual("Status OK", self.send.call_args.args[1])

    def test_early_group_command_ends_pending_image_context(self):
        main.handle_group_message(OWNER, GROUP, "", msg={"image_path": str(self.image)})
        with patch.object(main, "handle_group_command", return_value="Status OK"):
            self.route("status", chat=GROUP)
        self.route("thoughts?", chat=GROUP)
        self.scan.assert_not_called()
        self.assertIsNone(self.chat.call_args.kwargs.get("image_path"))

    def test_unknown_sender_image_never_reaches_provider_or_context(self):
        self.route("what do you think?", sender="unknown", attach=True)
        self.scan.assert_not_called()
        self.assertIsNone(image_conversation.get("unknown", "unknown"))

    def test_new_attachment_supersedes_previous_image_context(self):
        self.route("read this", attach=True)
        previous = str(self.image)
        self.image = Path(self.tmp.name) / "second.png"
        self.image.write_bytes(Path(previous).read_bytes())
        self.route("what do you think?", attach=True)
        self.route("why?")
        self.assertEqual(str(self.image), self.scan.call_args.args[0])
        self.assertIn("Current follow-up: why?", self.scan.call_args.args[1])

    def test_image_only_scan_keeps_reference_for_generation_followup(self):
        for prompt in ("make this into a funny meme", "edit it to look more colorful", "nano banana use this image as a reference"):
            with self.subTest(prompt=prompt):
                self.scan.reset_mock()
                self.generation.reset_mock()
                self.route("", attach=True)
                self.route(prompt)
                self.scan.assert_called_once()
                self.generation.assert_called_once()
                self.assertEqual(str(self.image), self.generation.call_args.args[2])
                self.assertIn(prompt.replace("nano banana ", ""), self.generation.call_args.args[1])

    def test_unmentioned_group_image_can_be_used_as_generation_reference(self):
        main.handle_group_message(FRIEND, GROUP, "", msg={"image_path": str(self.image)})
        self.route("make this into a funny meme", sender=FRIEND, chat=GROUP)
        self.scan.assert_not_called()
        self.generation.assert_called_once()
        self.assertEqual(str(self.image), self.generation.call_args.args[2])
        self.assertEqual(GROUP, self.generation.call_args.args[3])
        self.assertTrue(self.generation.call_args.kwargs["is_group"])
        self.usage.assert_called_once_with(FRIEND, main.OPENAI_IMAGE_GENERATION_TOOL)

    def test_previous_scan_reaches_existing_screenshot_log_handler(self):
        self.route("", attach=True)
        with patch.object(main, "_handle_screenshot_issue_log", return_value="Test repair intake") as repair:
            self.route("analyze this and log")
            self.assertEqual(str(self.image), repair.call_args.args[2])
        self.scan.assert_called_once()

    def test_generation_reference_followup_cannot_bypass_revoked_access(self):
        self.route("", sender=FRIEND, attach=True)
        self.denial.return_value = "Image access is turned off for you right now."
        self.route("make this into a funny meme", sender=FRIEND)
        self.generation.assert_not_called()
        self.assertIn("turned off", self.send.call_args.args[1])

    def test_new_text_tasks_never_reuse_previous_photo_as_generation_reference(self):
        for task in ("make this email shorter", "edit this sentence", "create a budget and explain it",
                     "make this text shorter", "edit this code", "make this reminder daily"):
            with self.subTest(task=task):
                self.scan.reset_mock()
                self.chat.reset_mock()
                self.route("", attach=True)
                self.route(task)
                self.scan.assert_called_once()
                self.generation.assert_not_called()
                self.assertIsNone(self.chat.call_args.kwargs.get("image_path"))
                self.assertIsNone(image_conversation.get(OWNER, OWNER))

    def test_literal_and_negative_roast_captions_do_not_get_roast_instructions(self):
        for caption in ("cook wings", "don't roast", "do not roast this", "roast chicken", "cook dinner"):
            with self.subTest(caption=caption):
                self.route(caption, attach=True)
                self.assertNotIn("Roast this image", self.scan.call_args.args[1])

    def test_followup_matcher_rejects_new_topics_and_handles_image_references(self):
        for text in ("what do you think about ordering wings", "explain quantum physics", "read main.py", "grant Cole admin", "cancel cron 7"):
            self.assertFalse(image_conversation.is_image_followup(text), text)
        for text in ("what do u think of this guy?", "thoughts?", "can you read it again?", "what does this say?", "why?"):
            self.assertTrue(image_conversation.is_image_followup(text), text)


if __name__ == "__main__":
    unittest.main()
