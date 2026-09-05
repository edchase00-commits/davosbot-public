"""Real DM/group routes and agent loop; all tools, providers and sends are mocked."""

import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from davosbot import commands, image_conversation, main, market, tool_outcomes, tools
from test_agentic_tool_permissions import _load_agentic_loop, _response, _tool_call
from test_model_routing import _load_routing_helpers


OWNER = "+15550000001"
ADMIN = "+15550000002"
FRIEND = "+15550000003"
GROUP = "0123456789abcdef0123456789abcdef"


class NoWebRouteTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in (
            "_get_buffered_image", "_handle_screenshot_issue_log", "_handle_priority_intake_command",
            "_handle_self_status_question", "_handle_model_status_question", "_handle_natural_model_request",
            "handle_private_send_confirmation", "handle_private_send_request", "get_persona",
            "handle_style_directive_message", "_fast_chat_reply", "_describe_cron_from_text",
            "handle_group_command", "handle_group_persona_editor_command", "_non_owner_length_rejection",
            "_viral_banter_reply", "_log_group_error_intake_if_needed", "_log_owner_quality_intake_if_needed",
            "_sports_recap_cron_from_text", "_schedule_cron_from_text", "_cancel_cron_from_text",
            "_handle_openai_image_intent", "_handle_image_capability_status", "_complex_analysis_preflight_reply",
            "decatur_behavior_fast_reply", "match_skill", "detect_user_fact", "format_style_directives_for_prompt",
        ):
            self.stack.enter_context(patch.object(main, name, return_value=None))
        for module in (main, commands):
            self.stack.enter_context(patch.object(module, "is_owner", lambda sender: sender == OWNER))
            self.stack.enter_context(patch.object(module, "is_admin", lambda sender: sender in {OWNER, ADMIN}))
        for name in ("is_owner_in_chat", "is_gc_enabled", "is_approved_user", "can_user_do"):
            self.stack.enter_context(patch.object(main, name, return_value=True))
        for name in ("_is_rate_limited", "_is_injection_attempt", "looks_like_tone_feedback",
                     "classify_cron_list_intent", "detect_reminder_edit_intent", "_handle_image_caption",
                     "check_admin_password"):
            self.stack.enter_context(patch.object(main, name, return_value=False))
        self.stack.enter_context(patch.object(main, "classify_reminder_intent", return_value="none"))
        self.stack.enter_context(patch.object(main, "get_tool_uses_today", return_value=0))
        self.stack.enter_context(patch.object(main, "get_history", return_value=[]))
        for name in ("save_turn", "extract_and_update_memory", "log_tool_use"):
            self.stack.enter_context(patch.object(main, name))
        for name in ("build_system_prompt", "build_light_chat_system_prompt"):
            self.stack.enter_context(patch.object(main, name, return_value="synthetic system"))
        self.stack.enter_context(patch.object(main, "enforce_decatur_behavior_reply", side_effect=lambda reply, *args: reply))
        self.stack.enter_context(patch.object(commands, "handle_club_command", return_value=None))
        self.stack.enter_context(patch.object(commands, "_looks_like_self_repair_intake", return_value=False))
        self.stack.enter_context(patch("davosbot.food_order.handle_food_order", return_value=None))
        self.stack.enter_context(patch.object(tools, "_edit_cron_from_text", return_value=None))
        self.stack.enter_context(patch.object(image_conversation, "begin_message", return_value=False))
        self.stack.enter_context(patch.object(image_conversation, "path_for_followup", return_value=None))
        self.stack.enter_context(patch.object(image_conversation, "forget"))
        self.stack.enter_context(patch.dict(main._image_buffer, {}, clear=True))
        self.stack.enter_context(patch.dict(main._text_buffer, {}, clear=True))
        self.market_query = self.stack.enter_context(patch.object(main, "handle_market_query", return_value=None))
        self.command_card = self.stack.enter_context(patch.object(commands, "get_ufc_fight_card", return_value="synthetic card"))
        self.direct_card = self.stack.enter_context(patch.object(main, "get_ufc_fight_card", return_value="synthetic card"))
        self.model = self.stack.enter_context(patch.object(main, "get_response", return_value="synthetic answer"))
        self.send = self.stack.enter_context(patch.object(main, "send_message", return_value=True))

    def route(self, text, *, sender=OWNER, group=False, image=False):
        self.model.reset_mock()
        self.send.reset_mock()
        self.market_query.reset_mock()
        self.command_card.reset_mock()
        self.direct_card.reset_mock()
        if group:
            main.handle_group_message(sender, GROUP, "@Davos " + text,
                                      msg={"image_path": "synthetic-image.png"} if image else None)
        else:
            main.handle_dm(sender, text, image_path="synthetic-image.png" if image else None)

    def test_owner_no_search_keeps_local_actions_and_server_origin(self):
        for group in (False, True):
            for image in (False, True):
                with self.subTest(group=group, image=image):
                    self.route("no search write a CSV file named counts.csv", group=group, image=image)
                    self.model.assert_called_once()
                    options = self.model.call_args.kwargs
                    self.assertFalse(options["use_tools"])
                    self.assertIn("write_file", options["allowed_tools"])
                    self.assertIn("shell_exec", options["allowed_tools"])
                    self.assertNotIn("web_search", options["allowed_tools"])
                    self.assertNotIn("get_weather", options["allowed_tools"])
                    self.assertEqual(GROUP if group else OWNER, options["originating_chat_id"])
                    self.assertIn("do not search the web", self.model.call_args.args[0])

    def test_owner_default_action_still_gets_full_tools(self):
        for group in (False, True):
            with self.subTest(group=group):
                self.route("write a CSV file named counts.csv", group=group)
                self.assertTrue(self.model.call_args.kwargs["use_tools"])
                self.assertIsNone(self.model.call_args.kwargs["allowed_tools"])
                self.assertNotIn(main._NO_WEB_INFORMATION_INSTRUCTION, self.model.call_args.args[0])

    def test_actual_no_search_agent_loop_rejects_web_hallucination_and_runs_local_action(self):
        for group in (False, True):
            with self.subTest(group=group):
                namespace, module = _load_agentic_loop([
                    _response(_tool_call("web_search", query="forbidden lookup")),
                    _response(_tool_call("write_file", path="synthetic", content="one")),
                    _response({"text": "Done"}),
                ])
                helpers, direct_gemini, direct_ollama, _events = _load_routing_helpers()
                helpers["_call_gemini_agentic"] = namespace["_call_gemini_agentic"]
                self.model.side_effect = helpers["get_response"]
                with patch.dict("sys.modules", {
                    "agentic_boundary_test.tools": module,
                    "agentic_boundary_test.tool_outcomes": tool_outcomes,
                }):
                    self.route("no search write a CSV file named counts.csv", group=group)
                module.execute_tool_outcome.assert_called_once_with(
                    "write_file", {"path": "synthetic", "content": "one"}, sender=OWNER,
                    originating_chat_id=GROUP if group else OWNER,
                )
                advertised = namespace["requests"].post.call_args_list[0].kwargs["json"]["tools"][0]["functionDeclarations"]
                self.assertNotIn("web_search", {item["name"] for item in advertised})
                self.assertIn("not available", self.send.call_args.args[1])
                direct_gemini.assert_not_called()
                direct_ollama.assert_not_called()

    def test_no_search_live_question_has_constraint_and_no_tools(self):
        for group in (False, True):
            with self.subTest(group=group):
                self.route("no search what is the weather in Seattle?", group=group)
                self.assertFalse(self.model.call_args.kwargs["use_tools"])
                self.assertIsNone(self.model.call_args.kwargs["allowed_tools"])
                self.assertIn("without supplied evidence", self.model.call_args.args[0])

    def test_nonowners_never_gain_owner_tools_and_keep_normal_restricted_search(self):
        for sender in (ADMIN, FRIEND):
            for group in (False, True):
                for skip in (False, True):
                    with self.subTest(sender=sender, group=group, skip=skip):
                        self.route(("no search " if skip else "") + "weather in Seattle", sender=sender, group=group)
                        options = self.model.call_args.kwargs
                        self.assertFalse(options.get("use_tools", False))
                        expected = ["web_search", "get_weather"] if not skip and (sender == ADMIN or group) else None
                        self.assertEqual(expected, options.get("allowed_tools"))

    def test_market_fast_lookup_obeys_no_search_for_all_existing_roles(self):
        self.market_query.side_effect = market.handle_market_query
        with patch.object(market, "get_market_data", return_value="synthetic quote") as lookup:
            for sender in (OWNER, ADMIN, FRIEND):
                for group in (False, True):
                    for skip in (False, True):
                        with self.subTest(sender=sender, group=group, skip=skip):
                            lookup.reset_mock()
                            self.route(("no search " if skip else "") + "how's NVDA?", sender=sender, group=group)
                            if skip:
                                lookup.assert_not_called()
                                self.model.assert_called_once()
                            else:
                                lookup.assert_called_once()
                                self.model.assert_not_called()

    def test_ufc_shortcut_obeys_no_search_for_all_existing_roles(self):
        for sender in (OWNER, ADMIN, FRIEND):
            for group in (False, True):
                for skip in (False, True):
                    with self.subTest(sender=sender, group=group, skip=skip):
                        self.route(("no search " if skip else "") + "ufc card", sender=sender, group=group)
                        calls = self.command_card.call_count + self.direct_card.call_count
                        self.assertEqual(0 if skip else 1, calls)
                        self.assertEqual(1 if skip else 0, self.model.call_count)

    def test_native_market_commands_yield_without_search_and_default_queries_still_run(self):
        with patch.object(market, "get_market_data", return_value="synthetic quote") as lookup:
            for text in ("market", "market movers", "quote NVDA"):
                for sender in (OWNER, ADMIN):
                    with self.subTest(text=text, sender=sender):
                        lookup.reset_mock()
                        self.route("no search " + text, sender=sender)
                        lookup.assert_not_called()
                        self.model.assert_called_once()
                        self.route(text, sender=sender)
                        lookup.assert_called_once()
                        self.model.assert_not_called()

    def test_no_search_retains_local_market_commands_and_owner_alert_gate(self):
        with patch.object(market, "market_status_summary", return_value="synthetic status"), \
             patch.object(market, "market_help", return_value="synthetic help"), \
             patch.object(market, "set_market_alerts_enabled", return_value="synthetic alert change") as setter, \
             patch.object(market, "get_market_data") as lookup:
            for text in ("market status", "market help", "market alerts off"):
                self.route("no search " + text)
                self.model.assert_not_called()
            setter.assert_called_once_with(False)
            self.route("no search market alerts on", sender=ADMIN)
            self.assertIn("owner-only", self.send.call_args.args[1])
            setter.assert_called_once_with(False)
            lookup.assert_not_called()

    def test_no_search_keeps_local_market_status_in_groups_and_friend_dm(self):
        self.market_query.side_effect = market.handle_market_query
        with patch.object(market, "market_status_summary", return_value="synthetic local status") as status, \
             patch.object(market, "get_market_data") as lookup:
            for sender in (OWNER, ADMIN, FRIEND):
                for group in (False, True):
                    with self.subTest(sender=sender, group=group):
                        self.route("no search market status", sender=sender, group=group)
                        self.model.assert_not_called()
                        self.assertEqual("synthetic local status", self.send.call_args.args[1])
            self.assertEqual(6, status.call_count)
            lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
