import unittest
from unittest.mock import patch

from davosbot import commands, memory
from davosbot.status_intent import is_billing_status_request
import test_native_command_history as fixtures


class NaturalBillingStatusTests(unittest.TestCase):
    setUp = fixtures.NativeCommandHistoryTests.setUp
    route = fixtures.NativeCommandHistoryTests.route
    clear_history = fixtures.NativeCommandHistoryTests.clear_history

    def test_owner_natural_spend_queries_use_real_billing_and_remain_in_context(self):
        for request in (
            "What is my Gemini spend?", "How much have I spent on Gemini so far?",
            "Please show my API usage today.", "What's the bot's Gemini cost?",
        ):
            with self.subTest(request=request):
                self.clear_history()
                self.route(request)
                self.model.assert_not_called()
                self.assertIn("Input: 100 tokens", self.send.call_args.args[1])
                self.assertEqual(request, memory.get_history(fixtures.OWNER)[0]["content"])
                self.route("What does that total include?")
                self.assertIn("Input: 100 tokens", self.model.call_args.args[1][-1]["content"])

    def test_alias_uses_the_same_command_permission_as_literal_billing(self):
        for sender in (fixtures.OWNER, fixtures.ADMIN, fixtures.FRIEND):
            with self.subTest(sender=sender):
                self.assertEqual(commands.handle_command(sender, "billing"),
                                 commands.handle_command(sender, "What is my Gemini spend?"))

    def test_pricing_explanations_and_mutation_requests_are_not_status_aliases(self):
        for request in (
            "What does Gemini cost?", "Explain API billing.", "Reduce my Gemini spend.",
            "Show my API usage and delete the history.", "What is my Gemini spend last month?",
            "How much did my friend spend on Gemini?", "My Gemini spend is high.",
        ):
            with self.subTest(request=request):
                self.assertFalse(is_billing_status_request(request))
        with patch.object(commands, "_cmd_billing") as billing:
            self.route("What does Gemini cost?")
        billing.assert_not_called()


if __name__ == "__main__":
    unittest.main()
