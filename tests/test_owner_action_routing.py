"""Owner requests through real dispatch; providers and external effects are mocked."""

import unittest
from contextlib import ExitStack
from unittest.mock import patch

from davosbot import commands, food_order, image_conversation, main, tool_outcomes, tools
from test_agentic_tool_permissions import _load_agentic_loop, _response, _tool_call
from test_model_routing import _load_routing_helpers


OWNER = "+15550000001"


class OwnerActionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Keep command/intake/intent classifiers real. Isolate their stateful
        # endpoints so a wrong route is observable without performing its action.
        for module in (main, commands):
            self.stack.enter_context(patch.object(module, "is_owner", lambda sender: sender == OWNER))
            self.stack.enter_context(patch.object(module, "is_admin", lambda sender: sender == OWNER))
        for name in ("get_persona", "match_skill", "detect_user_fact"):
            self.stack.enter_context(patch.object(main, name, return_value=None))
        for name in ("save_turn", "extract_and_update_memory", "_log_quality_signal"):
            self.stack.enter_context(patch.object(main, name))
        for name in ("build_system_prompt", "build_light_chat_system_prompt"):
            self.stack.enter_context(patch.object(main, name, return_value="synthetic system"))
        self.stack.enter_context(patch.object(main, "handle_style_directive_message", return_value=None))
        self.stack.enter_context(patch.object(main, "_log_owner_quality_intake_if_needed", return_value=None))
        self.stack.enter_context(patch.object(main, "handle_private_send_confirmation", return_value=None))
        self.stack.enter_context(patch.object(main, "handle_private_send_request", return_value=None))
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
        self.repair = self.stack.enter_context(patch.object(commands, "_cmd_self_repair_intake", return_value="synthetic repair intake"))
        self.scan = self.stack.enter_context(patch.object(main, "scan_image", side_effect=AssertionError("unexpected image scan")))
        self.history = []
        self.history_loader = self.stack.enter_context(patch.object(main, "get_history", side_effect=lambda _sender, limit=20: self.history[-limit:]))
        self.model = self.stack.enter_context(patch.object(main, "get_response", return_value="synthetic answer"))
        self.send = self.stack.enter_context(patch.object(main, "send_message", return_value=True))

    def route(self, text, history=()):
        self.history = list(history)
        for mock in (self.model, self.send, self.repair, self.scan):
            mock.reset_mock()
        main.handle_dm(OWNER, text)

    def assert_model_route(self, text, *, use_tools, history=()):
        self.route(text, history)
        self.model.assert_called_once()
        args, options = self.model.call_args
        self.assertEqual(text, args[2])
        self.assertEqual(list(history), args[1])
        self.assertEqual(use_tools, options["use_tools"])
        self.assertEqual(OWNER, options["originating_chat_id"])
        self.repair.assert_not_called()
        self.scan.assert_not_called()
        self.assertEqual("synthetic answer", self.send.call_args.args[1])

    def test_analyzing_supplied_logs_does_not_request_a_screenshot_or_log_a_repair(self):
        for text in (
            "Analyze this sales log: Monday revenue=100 cost=75; Tuesday revenue=120 cost=90. What is margin?",
            "Review the call log below:\n09:00,4 minutes\n09:30,6 minutes",
            "Read this log: startup complete; connection refused. Explain what failed.",
        ):
            with self.subTest(text=text):
                self.assert_model_route(text, use_tools=text.startswith("Read "))

    def test_reading_a_log_file_uses_existing_file_tools(self):
        self.assert_model_route("Read the application log file at /tmp/demo.log and summarize the startup error.", use_tools=True)

    def test_save_and_export_requests_reach_existing_file_tools(self):
        self.assert_model_route("Save these rows as totals.csv:\nrevenue,cost\n100,75", use_tools=True)
        history = [{"role": "user", "content": "revenue,cost\n100,75"},
                   {"role": "assistant", "content": "Revenue is 100, cost 75, margin 25%."}]
        for text in ("Please export the table above to a CSV file.", "Can you save that as summary.txt?"):
            with self.subTest(text=text):
                self.assert_model_route(text, use_tools=True, history=history)

    def test_file_howto_and_inline_formatting_do_not_become_file_actions(self):
        for text in ("How do I export a CSV from Excel?", "Don't save anything, just format this CSV inline: a,b / 1,2"):
            with self.subTest(text=text):
                self.assert_model_route(text, use_tools=False)

    def test_new_file_requests_reach_executor_with_context_and_preserve_no_search(self):
        history = [{"role": "user", "content": "name,total\nExample,42"},
                   {"role": "assistant", "content": "The table has one row and a total of 42."}]
        for text in ("save that as totals.csv", "no search export the table to totals.csv"):
            with self.subTest(text=text):
                namespace, module = _load_agentic_loop([
                    _response(_tool_call("write_file", path="totals.csv", content="name,total\nExample,42")),
                    _response({"text": "Saved."}),
                ])
                module.execute_tool_outcome.return_value = tool_outcomes.ToolOutcome(
                    "unverified", "Synthetic file write result",
                )
                routes, gemini, ollama, _events = _load_routing_helpers()
                routes["_call_gemini_agentic"] = namespace["_call_gemini_agentic"]
                self.model.side_effect = routes["get_response"]
                with patch.dict("sys.modules", {
                    "agentic_boundary_test.tools": module,
                    "agentic_boundary_test.tool_outcomes": tool_outcomes,
                }):
                    self.route(text, history)
                module.execute_tool_outcome.assert_called_once_with(
                    "write_file", {"path": "totals.csv", "content": "name,total\nExample,42"},
                    sender=OWNER, originating_chat_id=OWNER,
                )
                payload = namespace["requests"].post.call_args_list[0].kwargs["json"]
                self.assertEqual(history[0]["content"], payload["contents"][0]["parts"][0]["text"])
                advertised = {entry["name"] for entry in payload["tools"][0]["functionDeclarations"]}
                self.assertEqual(not text.startswith("no search"), "web_search" in advertised)
                self.assertIn("Synthetic file write result", self.send.call_args.args[1])
                self.assertNotEqual("Saved.", self.send.call_args.args[1])
                gemini.assert_not_called()
                ollama.assert_not_called()

    def test_literal_weather_questions_request_live_evidence(self):
        for text in ("Will it rain in Seattle tomorrow?", "Is it snowing in Boston?", "Will it be cold in Seattle tonight?"):
            with self.subTest(text=text):
                self.assert_model_route(text, use_tools=True)

    def test_weather_explanations_and_supplied_plans_stay_conversational(self):
        for text in (
            "Why does it rain? Explain condensation simply.",
            "Plan only, don't order: pickup is $18 each, delivery is $23. Six people and a $120 cap. Choose one.",
            "Analyze this CSV:\nrevenue,cost\n100,75",
        ):
            with self.subTest(text=text):
                self.assert_model_route(text, use_tools="Analyze this CSV" in text)
        history = [{"role": "user", "content": "Pickup costs $18 per person. Six people, Friday 7pm, $120 cap."},
                   {"role": "assistant", "content": "Pickup costs $108."}]
        self.assert_model_route("Correction: Saturday, eight people, $150 cap. Keep 7pm.", use_tools=False, history=history)

    def test_no_search_weather_question_keeps_lookup_disabled(self):
        self.route("no search will it rain in Seattle tomorrow?")
        self.model.assert_called_once()
        self.assertFalse(self.model.call_args.kwargs["use_tools"])
        self.assertIsNone(self.model.call_args.kwargs["allowed_tools"])
        self.assertIn("do not search the web", self.model.call_args.args[0])

    def test_explicit_log_analysis_and_repair_still_reaches_repair_intake(self):
        for text in ("Analyze this log and fix yourself", "Analyze this log then record the bug"):
            with self.subTest(text=text):
                self.assertFalse(commands.is_log_analysis_request(text))
                self.assertTrue(commands._looks_like_self_repair_intake(text))

    def test_existing_explicit_screenshot_repair_still_requests_its_missing_image(self):
        for text in ("analyze this and log", "Analyze this image and log the issue"):
            with self.subTest(text=text):
                self.route(text)
                self.model.assert_not_called()
                self.assertIn("I need the screenshot/image", self.send.call_args.args[1])
        self.route("fix yourself: your last answer ignored the numbers")
        self.repair.assert_called_once()
        self.model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
