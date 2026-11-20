"""Final lookup promises need public reported results or an honest no-result reply."""

import unittest
from unittest.mock import patch

from davosbot import action_grounding, terminal_lookup, tool_outcomes
from test_agentic_tool_permissions import _load_agentic_loop, _response, _tool_call
from test_model_routing import _load_routing_helpers


HORSES = "Google Emerald Downs and give us all the horses for every race today and the odds"
PACKAGES = "Do you know if DHL or Fort Worth FedEx has delivered yet"
WAIT_REPLY = "Checking on that now. Hang tight."
SEARCH_REPLY = "Checking now. I'll search for the latest tracking updates on your irons from FedEx and your other incoming package from DHL."


class TerminalLookupResultTests(unittest.TestCase):
    def test_historical_terminal_placeholders_are_not_background_work(self):
        for prompt, reply in ((HORSES, WAIT_REPLY), (PACKAGES, SEARCH_REPLY),
                              ("Search for today's weather", "I'm searching now."),
                              (PACKAGES, "I will search next week."),
                              (PACKAGES, "I'll search tomorrow."),
                              (PACKAGES, "I'll check on Monday."),
                              (PACKAGES, "I'll check in 30 minutes."),
                              (PACKAGES, "I'll check at 10am.")):
            with self.subTest(prompt=prompt):
                self.assertTrue(terminal_lookup.is_terminal_lookup_placeholder(prompt, reply))
                self.assertEqual(terminal_lookup.NO_LOOKUP_RESULT,
                                 action_grounding.ground_unexecuted_reply(prompt, reply))

    def test_substantive_results_advice_negation_and_future_plans_survive(self):
        for prompt, reply in (
            (HORSES, "Checking now. The track is dark today, according to its official calendar."),
            (HORSES, "Checking now: the track is closed today."),
            (HORSES, "I found no race card for today."),
            (PACKAGES, "Check the carrier tracking page for the current status."),
            (PACKAGES, "I haven't searched; I don't have the tracking number."),
            (PACKAGES, "I can search if you provide a tracking number."),
            (PACKAGES, "I'll check after you send the tracking number."),
            (PACKAGES, "I'll search tomorrow when you ask again."),
            (PACKAGES, "Checking tracking requires a tracking number."),
            (PACKAGES, "I'll search. Do you have the tracking number?"),
            ("Google the weather", "The forecast is available at https://weather.example.test/today"),
            ("Write a story about a tracking search", WAIT_REPLY),
            ("Can you draft a reply about today's weather?", "I'll search now."),
            ("Translate this tracking update", WAIT_REPLY),
            ("Explain the phrase 'checking now'", WAIT_REPLY),
            (PACKAGES, 'The message says "Checking now."'),
            ("How would you search for today's races?", "I'll search now."),
            ("Do not search for today's races", "Searching now."),
            ("Check this poem", "Checking now."),
            ("What are you doing?", "I'm searching now."),
        ):
            with self.subTest(prompt=prompt, reply=reply):
                self.assertFalse(terminal_lookup.is_terminal_lookup_placeholder(prompt, reply))

    def test_all_plain_provider_paths_report_no_lookup_result(self):
        for route in ("local", "gemini", "advanced", "down_fallback", "tool_fallback", "restricted_fallback", "image"):
            with self.subTest(route=route):
                helpers, _, _, _ = _load_routing_helpers(
                    gemini_reply=SEARCH_REPLY, ollama_reply=SEARCH_REPLY,
                    simple_route="gemini:example" if route == "gemini" else "ollama:gemma3",
                )
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
                self.assertEqual(terminal_lookup.NO_LOOKUP_RESULT,
                                 helpers["get_response"]("system", [], PACKAGES, **args))

    def run_loop(self, responses, outcome=None, allowed=None):
        namespace, module = _load_agentic_loop(responses)
        if outcome is not None:
            module.execute_tool_outcome.return_value = outcome
        with patch.dict("sys.modules", {
            "agentic_boundary_test.tools": module,
            "agentic_boundary_test.tool_outcomes": tool_outcomes,
        }):
            reply = namespace["_call_gemini_agentic"]("system", [], HORSES, allowed_tools=allowed)
        return reply, module

    def test_agentic_no_tool_placeholder_does_not_launch_a_search(self):
        reply, module = self.run_loop([_response({"text": WAIT_REPLY})], allowed=["web_search"])
        self.assertEqual(terminal_lookup.NO_LOOKUP_RESULT, reply)
        module.execute_tool_outcome.assert_not_called()

    def test_agentic_public_tool_output_survives_a_terminal_placeholder(self):
        for status in ("confirmed", "failed", "denied", "pending", "unverified"):
            with self.subTest(status=status):
                outcome = tool_outcomes.ToolOutcome(status, "Synthetic public result or failure detail", "synthetic_scope")
                reply, module = self.run_loop([
                    _response(_tool_call("web_search", query="today's race card")),
                    _response({"text": WAIT_REPLY}),
                ], outcome, ["web_search"])
                self.assertIn(f"status: {status}; scope: synthetic_scope", reply)
                self.assertIn("Reported: Synthetic public result or failure detail", reply)
                self.assertNotIn("Hang tight", reply)
                module.execute_tool_outcome.assert_called_once()

    def test_public_lookup_fallback_does_not_newly_expose_private_reads(self):
        for private_tool in ("read_file", "list_chats", "list_crons", "query_workout"):
            trace = tool_outcomes.ToolTrace(user_msg=HORSES)
            trace.record(private_tool, tool_outcomes.ToolOutcome("confirmed", "PRIVATE SYNTHETIC DATA"), None)
            self.assertEqual(terminal_lookup.NO_LOOKUP_RESULT, trace.reply(WAIT_REPLY))
            trace.record("get_weather", tool_outcomes.ToolOutcome("unverified", "No provider configured."), None)
            result = trace.reply(WAIT_REPLY)
            self.assertNotIn("PRIVATE", result)
            self.assertIn("No provider configured.", result)
            self.assertIn("status: unverified", result)

    def test_real_results_and_mutation_receipts_remain_unchanged(self):
        trace = tool_outcomes.ToolTrace(user_msg=HORSES)
        trace.record("web_search", tool_outcomes.ToolOutcome("unverified", "Synthetic result"), None)
        substantive = "According to the track calendar, no races are scheduled today."
        self.assertEqual(substantive, trace.reply(substantive))
        trace.record("write_file", tool_outcomes.ToolOutcome("failed", "Synthetic failure", "synthetic_scope"), "write")
        result = trace.reply(WAIT_REPLY)
        self.assertIn("write file: failed", result)
        self.assertIn("Synthetic failure", result)
        self.assertNotIn("background", result)

    def test_empty_and_long_public_results_are_bounded_without_success_inference(self):
        trace = tool_outcomes.ToolTrace(user_msg=HORSES)
        trace.record("web_search", tool_outcomes.ToolOutcome("unverified", ""), None)
        self.assertIn("No result text was returned.", trace.reply(WAIT_REPLY))
        trace.record("web_search", tool_outcomes.ToolOutcome("unverified", "x" * 2000), None)
        result = trace.reply(WAIT_REPLY)
        self.assertIn("remaining output omitted", result)
        self.assertLess(len(result), 1500)

    def test_public_failure_error_attribution_and_secret_redaction_are_retained(self):
        trace = tool_outcomes.ToolTrace(user_msg=HORSES)
        trace.record("web_search", tool_outcomes.ToolOutcome(
            "failed", "Request failed https://example.test/?token=synthetic-secret", "transport", error="timeout"), None)
        result = trace.reply(WAIT_REPLY)
        self.assertIn("status: failed; scope: transport", result)
        self.assertIn("Error: timeout", result)
        self.assertNotIn("synthetic-secret", result)
        self.assertIn("[redacted]", result)


if __name__ == "__main__":
    unittest.main()
