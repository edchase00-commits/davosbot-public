"""Historical action-claim shapes replayed with synthetic actors and providers."""

import unittest
from unittest.mock import patch

from davosbot import action_grounding, tool_outcomes
from test_model_routing import _load_routing_helpers
from test_agentic_tool_permissions import _load_agentic_loop, _response, _tool_call
import test_actor_capability_context as actor_context


class UnexecutedActionTruthTests(unittest.TestCase):
    def test_clear_unsupported_fulfillment_and_verification_are_replaced(self):
        cases = (
            ("Can you put in a wings order?", "Absolutely. Ordering 50 vindictive wings with extra ghost pepper sauce."),
            ("Order dinner", "I placed the order."),
            ("Buy the tickets", "Your ticket order has been placed."),
            ("Can you order dinner?", "I'll order the wings now."),
            ("Text the group", "I've sent your message."),
            ("Make the cron use rotating themes", "Done, updated the cron with rotating themes."),
            ("Remove the source from the morning quote", "Stripped the source from the intro."),
            ("Verify it was saved", "I verified it was saved."),
            ("Run a test of the cron", "I tested the cron and it works."),
            ("Change the settings", "The settings are now saved."),
        )
        for prompt, claim in cases:
            with self.subTest(prompt=prompt, claim=claim):
                actual = action_grounding.ground_unexecuted_reply(prompt, claim)
                self.assertNotEqual(claim, actual)
                self.assertTrue("confirmation" in actual or "haven't verified" in actual or "test execution result" in actual)

    def test_plans_quotes_hypotheticals_refusals_and_in_reply_work_survive(self):
        cases = (
            ("Can you order wings?", "I can help you prepare a wings order. Which restaurant?"),
            ("Can you order wings?", "I haven't placed an order. Here is the restaurant menu."),
            ("Can you order wings?", "If I placed an order, I would need a confirmation first."),
            ("How does ordering work?", "Ordering food can require an account and payment details."),
            ("What does this mean?", 'The phrase "I ordered the food" describes a completed purchase.'),
            ("Write a dialogue about ordering wings", 'I ordered the food. It should arrive soon.'),
            ("Translate this sentence", "I placed the order."),
            ("Draft a reply", "I sent your message."),
            ("Order these alphabetically", "I ordered the list in alphabetical order."),
            ("Fix this sentence", "I fixed the wording: The event starts at noon."),
            ("Did you explain this?", "I sent the explanation above."),
            ("What is the best wing sauce?", "Buffalo is a safe starting point; get ranch on the side."),
            ("What should I get for dinner?", "Order 10 wings with buffalo sauce."),
            ("How do I update this?", "Save the settings, then check the resulting file."),
            ("Can you apologize on my behalf?", "Who should I send the apology to?"),
            ("Tell them a happy message", "Sending you some good vibes!"),
            ("Tell him we miss him", "Come back before we send the squad to drag you home!"),
            ("What does this source mean?", "I checked the source text you provided; it describes a return policy."),
        )
        for prompt, text in cases:
            with self.subTest(prompt=prompt):
                self.assertEqual(text, action_grounding.ground_unexecuted_reply(prompt, text))

    def test_plain_routes_cannot_emit_purchase_claim_even_when_provider_ignores_context(self):
        claim = "Absolutely. Ordering 50 wings with extra hot sauce."
        for route in ("local", "gemini", "advanced", "down_fallback", "tool_fallback", "restricted_fallback", "image"):
            with self.subTest(route=route):
                helpers, cloud, local, _ = _load_routing_helpers(
                    gemini_reply=claim, ollama_reply=claim,
                    simple_route="gemini:example" if route == "gemini" else "ollama:gemma3",
                )
                prompt = "Can you put in a wings order?"
                args = {}
                if route == "advanced":
                    helpers["_owner_advanced_direct_route"] = lambda *args: ("complex_reasoning", "example")
                if route == "down_fallback":
                    helpers["_ollama_down"] = True
                if route in ("tool_fallback", "restricted_fallback"):
                    helpers["_call_gemini_agentic"].side_effect = None
                    helpers["_call_gemini_agentic"].return_value = None
                    args = {"use_tools": True} if route == "tool_fallback" else {"allowed_tools": ["web_search"]}
                if route == "image":
                    args = {"image_path": "synthetic.png"}
                answer = helpers["get_response"]("system", [], prompt, **args)
                self.assertIn("don't have confirmation", answer)
                self.assertNotIn("Ordering 50", answer)

    def _run_agentic(self, responses, *, tools=None, outcome=None, prompt="Can you place a wings order?"):
        namespace, tool_module = _load_agentic_loop(responses)
        if outcome is not None:
            tool_module.execute_tool_outcome.return_value = outcome
        with patch.dict("sys.modules", {
            "agentic_boundary_test.tools": tool_module,
            "agentic_boundary_test.tool_outcomes": tool_outcomes,
        }):
            result = namespace["_call_gemini_agentic"]("system", [], prompt, allowed_tools=tools)
        return result, tool_module, namespace

    def test_agentic_no_call_and_search_only_do_not_prove_an_order(self):
        claim = "I placed the order."
        for responses in (
            [_response({"text": claim})],
            [_response(_tool_call("web_search", query="wing restaurant")), _response({"text": claim})],
        ):
            answer, module, _ = self._run_agentic(responses, tools=["web_search"])
            self.assertIn("don't have confirmation", answer)
            self.assertLessEqual(module.execute_tool_outcome.call_count, 1)

    def test_empty_tool_inventory_plain_fallback_is_guarded(self):
        namespace, module = _load_agentic_loop([])
        namespace["_call_gemini"].return_value = "Ordering 50 wings."
        with patch.dict("sys.modules", {
            "agentic_boundary_test.tools": module,
            "agentic_boundary_test.tool_outcomes": tool_outcomes,
        }):
            answer = namespace["_call_gemini_agentic"]("system", [], "Order wings", allowed_tools=[])
        self.assertIn("don't have confirmation", answer)
        module.execute_tool_outcome.assert_not_called()

    def test_real_mutation_receipts_keep_success_failure_pending_and_uncertainty(self):
        for status in ("confirmed", "failed", "denied", "pending", "unverified"):
            with self.subTest(status=status):
                outcome = tool_outcomes.ToolOutcome(status, "Synthetic result", "saved_settings")
                answer, module, _ = self._run_agentic([
                    _response(_tool_call("write_file", path="settings.json", content="{}")),
                    _response({"text": "I placed the order."}),
                ], outcome=outcome)
                self.assertIn("Synthetic result", answer)
                self.assertNotIn("order", answer)
                self.assertNotIn("don't have confirmation", answer)
                module.execute_tool_outcome.assert_called_once()

    def test_actual_readback_can_support_verification_but_failed_readback_cannot(self):
        claim = "I verified the cron settings were saved."
        for status in ("confirmed", "failed", "denied", "unverified", "pending"):
            trace = tool_outcomes.ToolTrace(user_msg="Verify the cron settings")
            trace.record("list_crons", tool_outcomes.ToolOutcome(status, "synthetic saved job"), None)
            answer = trace.reply(claim)
            if status == "confirmed":
                self.assertEqual(claim, answer)
            else:
                self.assertIn("haven't verified", answer)
        trace = tool_outcomes.ToolTrace(user_msg="Update the cron settings")
        trace.record("list_crons", tool_outcomes.ToolOutcome("confirmed", "synthetic old job"), None)
        self.assertIn("don't have confirmation", trace.reply("I updated the cron settings."))

    def test_readback_is_not_execution_and_verification_scope_must_match(self):
        for tool in (None, "web_search", "read_file", "list_crons"):
            for claim in ("I tested the cron script.", "I tested the script and all tests passed.", "I verified the test suite passed."):
                with self.subTest(tool=tool, claim=claim):
                    trace = tool_outcomes.ToolTrace(user_msg="Verify the script works")
                    if tool:
                        trace.record(tool, tool_outcomes.ToolOutcome("confirmed", "synthetic readback"), None)
                    self.assertIn("test execution result", trace.reply(claim))
        trace = tool_outcomes.ToolTrace(user_msg="Verify the cron was saved")
        trace.record("list_reminders", tool_outcomes.ToolOutcome("confirmed", "synthetic reminders"), None)
        self.assertIn("haven't verified", trace.reply("I checked the saved cron."))
        trace.record("list_crons", tool_outcomes.ToolOutcome("confirmed", "synthetic cron"), None)
        self.assertEqual("I checked the saved cron.", trace.reply("I checked the saved cron."))


class HistoricalDispatchActionTruthTests(unittest.TestCase):
    setUp = actor_context.ActorCapabilityContextTests.setUp
    route = actor_context.ActorCapabilityContextTests.route

    def test_friend_group_wings_request_does_not_send_the_providers_false_order(self):
        claim = "Absolutely. Ordering 50 wings with extra ghost pepper sauce."
        self.local.return_value = claim
        self.cloud.return_value = claim
        self.route("can you put in a wings order?", sender=actor_context.FRIEND, group=True)
        self.local.assert_called_once()
        self.agentic.assert_not_called()
        self.assertIn("don't have confirmation", self.send.call_args.args[1])
        self.assertNotIn("Ordering 50", self.send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
