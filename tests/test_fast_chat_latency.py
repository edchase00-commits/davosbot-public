import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from davosbot import main, simple_chat


META_REPAIR_PHRASES = (
    "i'm back",
    "im back",
    "back to normal",
    "back in the chat",
    "chiller mode",
    "less robot",
    "more davos",
    "vibes recalibrated",
    "clipboard burned",
    "back in the pocket",
    "mode restored",
    "normal-person",
)


class FastChatLatencyTests(unittest.TestCase):
    def test_owner_dm_trivial_casual_chat_uses_fast_table(self):
        prompts = [
            "Yo",
            "bet",
            "are you alive?",
            "we missed you",
            "Yoooo wassup bro what\u2019s good",
            "How is ur day going bro",
            "You ready for tmw?",
        ]

        for prompt in prompts:
            with self.subTest(prompt=prompt):
                sent = []
                saved = []

                patches = [
                    patch.object(main, "is_owner", return_value=True),
                    patch.object(main, "send_message", lambda recipient, text, is_group=False: sent.append((recipient, text, is_group)) or True),
                    patch.object(main, "save_turn", lambda *args: saved.append(args)),
                    patch.object(main, "_handle_screenshot_issue_log", return_value=None),
                    patch.object(main, "_handle_priority_intake_command", return_value=None),
                    patch.object(main, "_handle_self_status_question", return_value=None),
                    patch.object(main, "_handle_model_status_question", return_value=None),
                    patch.object(main, "handle_private_send_confirmation", return_value=None),
                    patch.object(main, "handle_private_send_request", return_value=None),
                    patch.object(main, "handle_style_directive_message", return_value=None),
                    patch.object(main, "_describe_cron_from_text", return_value=None),
                    patch.object(main, "_sports_recap_cron_from_text", return_value=None),
                    patch.object(main, "_schedule_cron_from_text", return_value=None),
                    patch.object(main, "_viral_banter_reply", return_value=None),
                    patch.object(main, "_handle_openai_image_intent", return_value=None),
                    patch.object(main, "_handle_image_capability_status", return_value=None),
                    patch.object(main, "handle_command", return_value=None),
                    patch.object(main, "get_persona", return_value=None),
                    patch.object(main, "decatur_behavior_fast_reply", return_value=None),
                    patch.object(main, "_log_owner_quality_intake_if_needed", return_value=None),
                    patch.object(main, "_complex_analysis_preflight_reply", return_value=None),
                    patch.object(main, "classify_reminder_intent", return_value="none"),
                    patch.object(main, "classify_cron_list_intent", return_value=False),
                    patch.object(main, "detect_reminder_edit_intent", return_value=False),
                    patch.object(main, "match_skill", return_value=None),
                    patch.object(main, "detect_user_fact", return_value=None),
                    patch.object(main, "build_light_chat_system_prompt", Mock(side_effect=AssertionError("prompt should not build for trivial chat"))),
                    patch.object(main, "build_system_prompt", Mock(side_effect=AssertionError("prompt should not build for trivial chat"))),
                    patch.object(main, "get_history", Mock(side_effect=AssertionError("history should not load for trivial chat"))),
                    patch.object(main, "extract_and_update_memory", Mock(side_effect=AssertionError("memory extraction should not run for plain chat"))),
                ]
                with ExitStack() as stack:
                    for item in patches:
                        stack.enter_context(item)
                    stack.enter_context(patch.object(main, "get_response", Mock(side_effect=AssertionError("model should not run for trivial chat"))))
                    main.handle_dm("+15550000001", prompt)

                expected_reply = main._fast_chat_reply(prompt)
                self.assertIsNotNone(expected_reply)
                self.assertEqual([("+15550000001", expected_reply, False)], sent)
                self.assertEqual(
                    [
                        ("+15550000001", "user", prompt),
                        ("+15550000001", "assistant", expected_reply),
                    ],
                    saved,
                )

    def test_owner_dm_literal_ping_keeps_fast_path(self):
        for prompt in ("ping",):
            with self.subTest(prompt=prompt):
                sent = []
                saved = []

                with (
                    patch.object(main, "is_owner", return_value=True),
                    patch.object(main, "send_message", lambda recipient, text, is_group=False: sent.append((recipient, text, is_group))),
                    patch.object(main, "save_turn", lambda *args: saved.append(args)),
                    patch.object(main, "_handle_screenshot_issue_log", return_value=None),
                    patch.object(main, "_handle_priority_intake_command", return_value=None),
                    patch.object(main, "_handle_self_status_question", return_value=None),
                    patch.object(main, "_handle_model_status_question", return_value=None),
                    patch.object(main, "handle_private_send_confirmation", return_value=None),
                    patch.object(main, "handle_private_send_request", return_value=None),
                    patch.object(main, "get_persona", return_value="gruden"),
                    patch.object(main, "get_response", Mock(side_effect=AssertionError("model should not run for utility ping"))),
                    patch.object(main, "extract_and_update_memory", Mock(side_effect=AssertionError("memory extraction should not run for utility ping"))),
                ):
                    main.handle_dm("+15550000001", prompt)

                expected_reply = main._fast_chat_reply(prompt)
                self.assertEqual([("+15550000001", expected_reply, False)], sent)
                self.assertEqual(
                    [
                        ("+15550000001", "user", prompt),
                        ("+15550000001", "assistant", expected_reply),
                    ],
                    saved,
                )

    def test_admin_dm_trivial_casual_chat_uses_fast_table(self):
        prompt = "what's up"
        sent = []
        saved = []

        patches = [
            patch.object(main, "send_message", lambda recipient, text, is_group=False: sent.append((recipient, text, is_group)) or True),
            patch.object(main, "save_turn", lambda *args: saved.append(args)),
            patch.object(main, "handle_private_send_confirmation", return_value=None),
            patch.object(main, "check_admin_password", return_value=False),
            patch.object(main, "_handle_priority_intake_command", return_value=None),
            patch.object(main, "_handle_self_status_question", return_value=None),
            patch.object(main, "_handle_model_status_question", return_value=None),
            patch.object(main, "_non_owner_length_rejection", return_value=None),
            patch.object(main, "_viral_banter_reply", return_value=None),
            patch.object(main, "handle_private_send_request", return_value=None),
            patch.object(main, "_describe_cron_from_text", return_value=None),
            patch.object(main, "_sports_recap_cron_from_text", return_value=None),
            patch.object(main, "_schedule_cron_from_text", return_value=None),
            patch.object(main, "_handle_openai_image_intent", return_value=None),
            patch.object(main, "_handle_image_capability_status", return_value=None),
            patch.object(main, "handle_command", Mock(side_effect=AssertionError("command should not run for trivial admin chat"))),
            patch.object(main, "handle_style_directive_message", Mock(side_effect=AssertionError("style handler should not run for trivial admin chat"))),
            patch.object(main, "match_skill", Mock(side_effect=AssertionError("skill matching should not run for trivial admin chat"))),
            patch.object(main, "build_system_prompt", Mock(side_effect=AssertionError("prompt should not build for trivial admin chat"))),
            patch.object(main, "get_history", Mock(side_effect=AssertionError("history should not load for trivial admin chat"))),
            patch.object(main, "get_response", Mock(side_effect=AssertionError("model should not run for trivial admin chat"))),
        ]
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            main._handle_admin_dm("admin", prompt)

        expected_reply = main._fast_chat_reply(prompt)
        self.assertEqual([("admin", expected_reply, False)], sent)
        self.assertEqual(
            [
                ("admin", "user", prompt),
                ("admin", "assistant", expected_reply),
            ],
            saved,
        )

    def test_friend_dm_trivial_casual_chat_uses_fast_table(self):
        prompt = "yo"
        sent = []
        saved = []

        patches = [
            patch.object(main, "send_message", lambda recipient, text, is_group=False: sent.append((recipient, text, is_group)) or True),
            patch.object(main, "save_turn", lambda *args: saved.append(args)),
            patch.object(main, "check_admin_password", return_value=False),
            patch.object(main, "_non_owner_length_rejection", return_value=None),
            patch.object(main, "_viral_banter_reply", return_value=None),
            patch.object(main, "_handle_openai_image_intent", return_value=None),
            patch.object(main, "_handle_image_capability_status", return_value=None),
            patch.object(main, "handle_style_directive_message", Mock(side_effect=AssertionError("style handler should not run for trivial friend chat"))),
            patch.object(main, "is_ufc_fight_card_request", Mock(side_effect=AssertionError("UFC routing should not run for trivial friend chat"))),
            patch.object(main, "get_history", Mock(side_effect=AssertionError("history should not load for trivial friend chat"))),
            patch.object(main, "get_response", Mock(side_effect=AssertionError("model should not run for trivial friend chat"))),
        ]
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            main._handle_friend_dm("friend", prompt)

        expected_reply = main._fast_chat_reply(prompt)
        self.assertEqual([("friend", expected_reply, False)], sent)
        self.assertEqual(
            [
                ("friend", "user", prompt),
                ("friend", "assistant", expected_reply),
            ],
            saved,
        )

    def test_owner_dm_tone_feedback_saves_before_fast_chat_and_model(self):
        sent = []
        saved = []
        style_calls = []

        def fake_style_handler(sender, text, **kwargs):
            style_calls.append((sender, text, kwargs))
            return "Got you. I'll keep it loose."

        with (
            patch.object(main, "is_owner", return_value=True),
            patch.object(main, "send_message", lambda recipient, text, is_group=False: sent.append((recipient, text, is_group))),
            patch.object(main, "save_turn", lambda *args: saved.append(args)),
            patch.object(main, "_handle_screenshot_issue_log", return_value=None),
            patch.object(main, "_handle_priority_intake_command", return_value=None),
            patch.object(main, "_handle_self_status_question", return_value=None),
            patch.object(main, "_handle_model_status_question", return_value=None),
            patch.object(main, "handle_private_send_confirmation", return_value=None),
            patch.object(main, "handle_private_send_request", return_value=None),
            patch.object(main, "get_persona", return_value=None),
            patch.object(main, "handle_style_directive_message", fake_style_handler),
            patch.object(main, "_fast_chat_reply", Mock(side_effect=AssertionError("fast chat should not steal tone feedback"))),
            patch.object(main, "get_response", Mock(side_effect=AssertionError("model should not run for tone feedback"))),
            patch.object(main, "extract_and_update_memory", Mock(side_effect=AssertionError("memory extraction should not run for tone feedback"))),
        ):
            main.handle_dm("+15550000001", "Enough with the less robot thing. Give me more Davos energy. Hang loose and be a chiller")

        expected_reply = "Got you. I'll keep it loose."
        self.assertEqual([("+15550000001", expected_reply, False)], sent)
        self.assertEqual(
            [
                ("+15550000001", "user", "Enough with the less robot thing. Give me more Davos energy. Hang loose and be a chiller"),
                ("+15550000001", "assistant", expected_reply),
            ],
            saved,
        )
        self.assertEqual(1, len(style_calls))
        self.assertTrue(style_calls[0][2]["tone_feedback_only"])

    def test_owner_dm_tone_feedback_with_task_intent_does_not_swallow_reminder(self):
        sent = []
        saved = []
        style_calls = []

        def fake_style_handler(sender, text, **kwargs):
            style_calls.append((sender, text, kwargs))
            return None

        patches = [
            patch.object(main, "is_owner", return_value=True),
            patch.object(main, "send_message", lambda recipient, text, is_group=False: sent.append((recipient, text, is_group))),
            patch.object(main, "save_turn", lambda *args: saved.append(args)),
            patch.object(main, "_handle_screenshot_issue_log", return_value=None),
            patch.object(main, "_handle_priority_intake_command", return_value=None),
            patch.object(main, "_handle_self_status_question", return_value=None),
            patch.object(main, "_handle_model_status_question", return_value=None),
            patch.object(main, "handle_private_send_confirmation", return_value=None),
            patch.object(main, "handle_private_send_request", return_value=None),
            patch.object(main, "handle_style_directive_message", fake_style_handler),
            patch.object(main, "_fast_chat_reply", return_value=None),
            patch.object(main, "_describe_cron_from_text", return_value=None),
            patch.object(main, "_sports_recap_cron_from_text", return_value=None),
            patch.object(main, "_schedule_cron_from_text", return_value=None),
            patch.object(main, "_viral_banter_reply", return_value=None),
            patch.object(main, "_handle_openai_image_intent", return_value=None),
            patch.object(main, "_handle_image_capability_status", return_value=None),
            patch.object(main, "handle_command", return_value=None),
            patch.object(main, "get_persona", return_value=None),
            patch.object(main, "decatur_behavior_fast_reply", return_value=None),
            patch.object(main, "_log_owner_quality_intake_if_needed", return_value=None),
            patch.object(main, "_complex_analysis_preflight_reply", return_value=None),
            patch.object(main, "classify_reminder_intent", return_value="schedule"),
            patch.object(main, "_handle_deterministic_reminder_schedule", return_value="Reminder set."),
            patch.object(main, "get_response", Mock(side_effect=AssertionError("model should not run for reminder schedule"))),
        ]
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            main.handle_dm("+15550000001", "remind me to be more chill tomorrow")

        self.assertEqual([("+15550000001", "Reminder set.", False)], sent)
        self.assertEqual(
            [
                ("+15550000001", "user", "remind me to be more chill tomorrow"),
                ("+15550000001", "assistant", "Reminder set."),
            ],
            saved,
        )
        self.assertEqual(1, len(style_calls))
        self.assertFalse(style_calls[0][2].get("tone_feedback_only", False))
        self.assertFalse(style_calls[0][2]["allow_tone_feedback"])

    def test_fast_vibe_check_does_not_replace_model_chat(self):
        self.assertIsNone(main._fast_chat_reply("Davos why you weird all of a sudden"))
        self.assertIsNone(main._fast_chat_reply("Can you go back to being a chiller?"))

    def test_fast_vibe_check_does_not_steal_task_intents(self):
        self.assertIsNone(main._fast_chat_reply("Davos why you weird when I ask you to list reminders?"))
        self.assertIsNone(main._fast_chat_reply("Davos why you weird when I ask for model status?"))

    def test_fast_chat_copy_has_no_personality_repair_meta(self):
        pools = [
            simple_chat.WHATS_GOOD_REPLIES,
            simple_chat.WEIRD_REPLIES,
            simple_chat.CHILLER_REPLIES,
            simple_chat.FAST_DAY_REPLIES,
            simple_chat.FAST_READY_REPLIES,
            simple_chat.SIMPLE_CHAT_DEFAULT_REPLIES,
        ]

        for reply in [item for pool in pools for item in pool]:
            lower = reply.lower()
            for phrase in META_REPAIR_PHRASES:
                self.assertNotIn(phrase, lower)

        prompts = [
            "What\u2019s up bro",
            "What\u2019s up davos",
            "How is ur day going bro",
            "How you doing now LC",
            "You ready for tmw?",
            "welcome back",
        ]
        for prompt in prompts:
            reply = main._fast_chat_reply(prompt)
            self.assertIsNotNone(reply)
            lower = reply.lower()
            for phrase in META_REPAIR_PHRASES:
                self.assertNotIn(phrase, lower)

        self.assertIsNone(main._fast_chat_reply("Can you go back to being a chiller?"))

    def test_style_feedback_guard_yields_to_task_intents(self):
        self.assertTrue(main._style_feedback_should_yield_to_task_intent("remind me to be more chill tomorrow"))
        self.assertTrue(main._style_feedback_should_yield_to_task_intent("list reminders"))
        self.assertTrue(main._style_feedback_should_yield_to_task_intent("what model status are you on?"))
        self.assertTrue(main._style_feedback_should_yield_to_task_intent("read the logs and be more chill about it"))
        self.assertFalse(main._style_feedback_should_yield_to_task_intent("be way more chill"))
        self.assertFalse(main._style_feedback_should_yield_to_task_intent("stop saying chiller mode restored, we get it"))

    def test_owner_dm_natural_model_question_skips_model(self):
        sent = []
        command_calls = []

        def fake_handle_command(sender, text):
            command_calls.append((sender, text))
            return "Model routing: test"

        with (
            patch.object(main, "is_owner", return_value=True),
            patch.object(main, "send_message", lambda recipient, text, is_group=False: sent.append((recipient, text, is_group))),
            patch.object(main, "_handle_screenshot_issue_log", return_value=None),
            patch.object(main, "_handle_priority_intake_command", return_value=None),
            patch.object(main, "handle_command", fake_handle_command),
            patch.object(main, "get_response", Mock(side_effect=AssertionError("model should not run for model status"))),
        ):
            main.handle_dm("+15550000001", "Which model do you use?")

        self.assertEqual([("+15550000001", "model status")], command_calls)
        self.assertEqual([("+15550000001", "Model routing: test", False)], sent)

    def test_owner_dm_natural_model_request_maps_to_review_only_command(self):
        sent = []
        command_calls = []

        def fake_handle_command(sender, text):
            command_calls.append((sender, text))
            return "Model request logged #1 [YELLOW]"

        with (
            patch.object(main, "is_owner", return_value=True),
            patch.object(main, "send_message", lambda recipient, text, is_group=False: sent.append((recipient, text, is_group))),
            patch.object(main, "_handle_screenshot_issue_log", return_value=None),
            patch.object(main, "_handle_priority_intake_command", return_value=None),
            patch.object(main, "_handle_self_status_question", return_value=None),
            patch.object(main, "_handle_model_status_question", return_value=None),
            patch.object(main, "handle_command", fake_handle_command),
            patch.object(main, "handle_private_send_confirmation", Mock(side_effect=AssertionError("should not reach private-send confirmation"))),
            patch.object(main, "get_response", Mock(side_effect=AssertionError("model should not run for natural model request"))),
        ):
            main.handle_dm("+15550000001", "Try and fall back to Gemini pro for the next reply")

        self.assertEqual(
            [("+15550000001", "model request chat Try and fall back to Gemini pro for the next reply")],
            command_calls,
        )
        self.assertEqual([("+15550000001", "Model request logged #1 [YELLOW]", False)], sent)

    def test_model_options_question_maps_to_options_command(self):
        command_calls = []

        def fake_handle_command(sender, text):
            command_calls.append((sender, text))
            return "Model options: test"

        with patch.object(main, "handle_command", fake_handle_command):
            reply = main._handle_model_status_question("+15550000001", "what are the current routing and model options?")

        self.assertEqual("Model options: test", reply)
        self.assertEqual([("+15550000001", "model options")], command_calls)

    def test_model_power_ranking_maps_to_options_command(self):
        command_calls = []

        def fake_handle_command(sender, text):
            command_calls.append((sender, text))
            return "Model power ranking: test"

        with patch.object(main, "handle_command", fake_handle_command):
            reply = main._handle_model_status_question("+15550000001", "model power rankings")

        self.assertEqual("Model power ranking: test", reply)
        self.assertEqual([("+15550000001", "model options")], command_calls)

    def test_owner_plain_chat_uses_light_prompt_and_short_history(self):
        sent = []
        history_calls = []

        def fake_get_history(sender, limit=20):
            history_calls.append((sender, limit))
            return []

        patches = [
            patch.object(main, "is_owner", return_value=True),
            patch.object(main, "send_message", lambda recipient, text, is_group=False: sent.append((recipient, text, is_group)) or True),
            patch.object(main, "save_turn", lambda *args: None),
            patch.object(main, "_handle_screenshot_issue_log", return_value=None),
            patch.object(main, "_handle_priority_intake_command", return_value=None),
            patch.object(main, "_handle_self_status_question", return_value=None),
            patch.object(main, "_handle_model_status_question", return_value=None),
            patch.object(main, "handle_private_send_confirmation", return_value=None),
            patch.object(main, "handle_private_send_request", return_value=None),
            patch.object(main, "_fast_chat_reply", return_value=None),
            patch.object(main, "_describe_cron_from_text", return_value=None),
            patch.object(main, "_sports_recap_cron_from_text", return_value=None),
            patch.object(main, "_viral_banter_reply", return_value=None),
            patch.object(main, "_handle_openai_image_intent", return_value=None),
            patch.object(main, "_handle_image_capability_status", return_value=None),
            patch.object(main, "handle_command", return_value=None),
            patch.object(main, "get_persona", return_value=None),
            patch.object(main, "decatur_behavior_fast_reply", return_value=None),
            patch.object(main, "_log_owner_quality_intake_if_needed", return_value=None),
            patch.object(main, "_complex_analysis_preflight_reply", return_value=None),
            patch.object(main, "classify_reminder_intent", return_value="none"),
            patch.object(main, "classify_cron_list_intent", return_value=False),
            patch.object(main, "detect_reminder_edit_intent", return_value=False),
            patch.object(main, "match_skill", return_value=None),
            patch.object(main, "detect_user_fact", return_value=None),
            patch.object(main, "build_light_chat_system_prompt", return_value="light prompt"),
            patch.object(main, "build_system_prompt", Mock(side_effect=AssertionError("full prompt should not build for plain chat"))),
            patch.object(main, "get_history", fake_get_history),
            patch.object(main, "extract_and_update_memory", Mock(side_effect=AssertionError("memory extraction should not run for plain chat"))),
        ]
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            get_response = stack.enter_context(patch.object(main, "get_response", return_value="plain reply"))
            main.handle_dm("+15550000001", "lol what are you doing")

        self.assertEqual([("+15550000001", 2)], history_calls)
        self.assertEqual("light prompt", get_response.call_args.args[0])
        self.assertTrue(get_response.call_args.kwargs["simple_chat"])
        self.assertEqual([("+15550000001", "plain reply", False)], sent)

    def test_owner_dm_active_personas_bare_chat_uses_light_persona_prompt(self):
        for persona in ("ATL", "Gruden"):
            with self.subTest(persona=persona):
                sent = []
                prompt_calls = []
                history_calls = []

                def fake_prompt_builder(*, persona=None, user_text="", chat_id=None):
                    prompt_calls.append({"persona": persona, "user_text": user_text, "chat_id": chat_id})
                    return f"light prompt for {persona}"

                def fake_get_history(sender, limit=20):
                    history_calls.append((sender, limit))
                    return []

                patches = [
                    patch.object(main, "is_owner", return_value=True),
                    patch.object(main, "send_message", lambda recipient, text, is_group=False: sent.append((recipient, text, is_group)) or True),
                    patch.object(main, "save_turn", lambda *args: None),
                    patch.object(main, "_handle_screenshot_issue_log", return_value=None),
                    patch.object(main, "_handle_priority_intake_command", return_value=None),
                    patch.object(main, "_handle_self_status_question", return_value=None),
                    patch.object(main, "_handle_model_status_question", return_value=None),
                    patch.object(main, "handle_private_send_confirmation", return_value=None),
                    patch.object(main, "handle_private_send_request", return_value=None),
                    patch.object(main, "get_persona", return_value=persona),
                    patch.object(main, "handle_style_directive_message", return_value=None),
                    patch.object(main, "_fast_chat_reply", return_value=None),
                    patch.object(main, "_describe_cron_from_text", return_value=None),
                    patch.object(main, "_sports_recap_cron_from_text", return_value=None),
                    patch.object(main, "_schedule_cron_from_text", return_value=None),
                    patch.object(main, "_viral_banter_reply", return_value=None),
                    patch.object(main, "_handle_openai_image_intent", return_value=None),
                    patch.object(main, "_handle_image_capability_status", return_value=None),
                    patch.object(main, "handle_command", return_value=None),
                    patch.object(main, "decatur_behavior_fast_reply", return_value=None),
                    patch.object(main, "_log_owner_quality_intake_if_needed", return_value=None),
                    patch.object(main, "_complex_analysis_preflight_reply", return_value=None),
                    patch.object(main, "classify_reminder_intent", return_value="none"),
                    patch.object(main, "classify_cron_list_intent", return_value=False),
                    patch.object(main, "detect_reminder_edit_intent", return_value=False),
                    patch.object(main, "match_skill", return_value=None),
                    patch.object(main, "detect_user_fact", return_value=None),
                    patch.object(main, "build_light_chat_system_prompt", fake_prompt_builder),
                    patch.object(main, "build_system_prompt", Mock(side_effect=AssertionError("full prompt should not build for persona plain chat"))),
                    patch.object(main, "get_history", fake_get_history),
                    patch.object(main, "extract_and_update_memory", Mock(side_effect=AssertionError("memory extraction should not run for persona plain chat"))),
                ]
                with ExitStack() as stack:
                    for item in patches:
                        stack.enter_context(item)
                    get_response = stack.enter_context(patch.object(main, "get_response", return_value=f"{persona} reply"))
                    main.handle_dm("+15550000001", "Yo")

                self.assertEqual([{"persona": persona, "user_text": "Yo", "chat_id": "+15550000001"}], prompt_calls)
                self.assertEqual([("+15550000001", 2)], history_calls)
                self.assertEqual(f"light prompt for {persona}", get_response.call_args.args[0])
                self.assertTrue(get_response.call_args.kwargs["simple_chat"])
                self.assertEqual([("+15550000001", f"{persona} reply", False)], sent)

    def test_self_status_question_skips_model(self):
        sent = []

        with (
            patch.object(main, "is_owner", return_value=True),
            patch.object(main, "send_message", lambda recipient, text, is_group=False: sent.append((recipient, text, is_group)) or True),
            patch.object(main, "_handle_screenshot_issue_log", return_value=None),
            patch.object(main, "_handle_priority_intake_command", return_value=None),
            patch.object(main, "get_response", Mock(side_effect=AssertionError("model should not run for self-status"))),
        ):
            main.handle_dm("+15550000001", "how do you work?")

        self.assertEqual(1, len(sent))
        self.assertIn("polling iMessage bot", sent[0][1])

    def test_handle_message_threads_trace_into_dm_handler(self):
        traces = []

        def fake_handle_dm(sender, text, image_path=None, trace=None):
            self.assertIsNotNone(trace)
            trace.set_route("test_dm")

        with (
            patch.object(main, "is_imessage_reaction", return_value=False),
            patch.object(main, "is_group_chat", return_value=False),
            patch.object(main, "check_rate_limit", return_value=True),
            patch.object(main, "handle_dm", fake_handle_dm),
            patch.object(main, "update_heartbeat", lambda: None),
            patch.object(main, "_log_message_trace", lambda trace, elapsed: traces.append((trace.route, elapsed))),
        ):
            main.handle_message({"sender": "+15550000001", "chat_identifier": "+15550000001", "text": "hello"})

        self.assertEqual(1, len(traces))
        self.assertEqual("test_dm", traces[0][0])


if __name__ == "__main__":
    unittest.main()
