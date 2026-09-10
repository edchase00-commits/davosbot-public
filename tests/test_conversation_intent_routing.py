import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from davosbot import failure_copy, main, simple_chat
from test_model_routing import _load_routing_helpers


class ConversationIntentRoutingTests(unittest.TestCase):
    def test_greeting_with_a_subject_reaches_conversation(self):
        for prompt in (
            "what's up with my invoice?", "what's good about that?",
            "what's up for dinner?", "what's up with you ignoring me?",
            "how is your day trading strategy", "how you doing that trick",
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(simple_chat.fast_chat_reply(prompt))

    def test_greetings_keep_the_instant_route(self):
        for prompt in (
            "Yo", "What's up bro", "What's up Davos", "Yoooo wassup bro what's good",
            "How is ur day going bro", "How you doing now LC", "ping",
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNotNone(simple_chat.fast_chat_reply(prompt))

    def test_contextual_short_messages_keep_more_history(self):
        for prompt in ("what do you think?", "no, the second one", "make it shorter", "why?", "caption that meme"):
            with self.subTest(prompt=prompt):
                self.assertEqual(12, simple_chat.history_limit(prompt))
        self.assertEqual(2, simple_chat.history_limit("Yo"))
        self.assertEqual(2, simple_chat.history_limit("lol what are you doing"))
        self.assertEqual(20, simple_chat.history_limit("which one?", 20))

    def test_backend_failure_does_not_answer_an_opinion_with_a_greeting(self):
        for prompt in ("what do you think?", "which one is better?", "make it shorter", "why?", "did you send it?"):
            with self.subTest(prompt=prompt):
                helpers, gemini, ollama, _events = _load_routing_helpers(ollama_reply=None, gemini_reply=None)
                reply = helpers["get_response"]("system", [], prompt, sender="+15550000001", simple_chat=True)
                self.assertEqual(failure_copy.DIRECT_CHAT_FAILURE_REPLY, reply)
                gemini.assert_called_once()
                ollama.assert_called_once()

    def test_reply_polish_does_not_invent_an_unrelated_greeting(self):
        self.assertEqual("I'm here.", simple_chat.polish_simple_chat_reply("where are you?", "I'm here."))
        self.assertEqual("Alive. What's up?", simple_chat.polish_simple_chat_reply("are you alive?", "I'm here."))

    def test_creative_requests_do_not_use_generic_meme_quips(self):
        for prompt in ("make this skibidi bit funny", "caption that meme", "roast his NPC outfit", "write a joke about his TikTok obsession"):
            with self.subTest(prompt=prompt):
                self.assertIsNone(main._viral_banter_reply(prompt))
        self.assertIn("Horny DLC", main._viral_banter_reply("computa make these guys super gay and horny"))

    def test_owner_followups_reach_model_with_original_choices(self):
        sender = "+15550000001"
        history = [
            {"role": "user", "content": "Draft one: team dinner. Draft two: rooftop party."},
            {"role": "assistant", "content": "Both are possible; the rooftop needs a rain plan."},
            {"role": "user", "content": "The forecast is rain."},
            {"role": "assistant", "content": "Use an indoor backup."},
        ]
        for prompt in ("what do you think?", "what's good about that?", "make this skibidi bit funny"):
            with self.subTest(prompt=prompt), ExitStack() as stack:
                for name in (
                    "_handle_screenshot_issue_log", "_handle_priority_intake_command",
                    "_handle_self_status_question", "_handle_model_status_question",
                    "handle_private_send_confirmation", "handle_private_send_request",
                    "handle_style_directive_message", "_describe_cron_from_text",
                    "_sports_recap_cron_from_text", "_schedule_cron_from_text", "_cancel_cron_from_text",
                    "_handle_openai_image_intent", "_handle_image_capability_status", "handle_command",
                    "get_persona", "decatur_behavior_fast_reply", "_log_owner_quality_intake_if_needed",
                    "_complex_analysis_preflight_reply", "match_skill", "detect_user_fact", "_market_fast_reply",
                ):
                    stack.enter_context(patch.object(main, name, return_value=None))
                stack.enter_context(patch.object(main, "is_owner", return_value=True))
                stack.enter_context(patch.object(main, "classify_reminder_intent", return_value="none"))
                stack.enter_context(patch.object(main, "classify_cron_list_intent", return_value=False))
                stack.enter_context(patch.object(main, "detect_reminder_edit_intent", return_value=False))
                stack.enter_context(patch.object(main, "save_turn"))
                stack.enter_context(patch.object(main, "extract_and_update_memory"))
                stack.enter_context(patch.object(main, "build_light_chat_system_prompt", return_value="light prompt"))
                stack.enter_context(patch.object(main, "build_system_prompt", return_value="full prompt"))
                stack.enter_context(patch.dict(main._image_buffer, {}, clear=True))
                stack.enter_context(patch.dict(main._text_buffer, {}, clear=True))
                history_loader = stack.enter_context(patch.object(main, "get_history", side_effect=lambda _sender, limit=20: history[-limit:]))
                model = stack.enter_context(patch.object(main, "get_response", return_value="Choose the team dinner; it stays dry."))
                send = stack.enter_context(patch.object(main, "send_message", return_value=True))
                main.handle_dm(sender, prompt)
                model.assert_called_once()
                self.assertEqual(history, model.call_args.args[1])
                self.assertEqual(prompt, model.call_args.args[2])
                self.assertEqual(12, history_loader.call_args.args[1])
                self.assertFalse(model.call_args.kwargs["use_tools"])
                send.assert_called_once_with(sender, "Choose the team dinner; it stays dry.")


if __name__ == "__main__":
    unittest.main()
