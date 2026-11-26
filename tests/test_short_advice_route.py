import unittest
from unittest.mock import patch

from davosbot import brain, simple_chat
from davosbot.failure_copy import DIRECT_CHAT_FAILURE_REPLY


class ShortAdviceRouteTests(unittest.TestCase):
    def test_judgment_is_distinct_from_banter(self):
        for prompt in ("What should I make for thanksgiving", "Which should we pick?",
                       "What would you choose?", "Should I roast the salmon?", "Suggest a side"):
            with self.subTest(prompt=prompt):
                self.assertTrue(simple_chat.looks_like_short_advice(prompt))
        for prompt in ("yo", "roast me", "you good?", "lol what are you doing", "get the weather"):
            with self.subTest(prompt=prompt):
                self.assertFalse(simple_chat.looks_like_short_advice(prompt))

    def test_local_advice_keeps_context_voice_and_no_banter_or_cloud(self):
        context = [{"role": "user", "content": "Turkey is already brining."}]
        answer = "Make green beans with browned butter. Bright, fast, and they won't compete for oven space."
        with (patch.object(brain, "_call_ollama", return_value=answer) as local,
              patch.object(brain, "_call_gemini", side_effect=AssertionError("unexpected provider")),
              patch.object(brain, "_call_gemini_agentic", side_effect=AssertionError("unexpected tools"))):
            self.assertEqual(answer, brain.get_response("Keep it sharp.", context,
                                                       "What should I make for thanksgiving", short_advice=True))
        self.assertIn("specific recommendation", local.call_args.args[0])
        self.assertIn("Never bypass owner-only", local.call_args.args[0])
        self.assertIn("Keep it sharp", local.call_args.args[0])
        self.assertEqual(context, local.call_args.args[1])
        self.assertFalse(local.call_args.kwargs.get("compact_banter", False))
        self.assertEqual(7.0, local.call_args.kwargs["timeout"])

    def test_local_advice_does_not_route_to_cloud_on_miss(self):
        for response in (None, brain._OLLAMA_TRUNCATED_SENTINEL, brain._OLLAMA_CONTEXT_LIMIT_SENTINEL):
            with (patch.object(brain, "_call_ollama", return_value=response),
                  patch.object(brain, "_call_gemini", side_effect=AssertionError("unexpected provider"))):
                self.assertEqual(DIRECT_CHAT_FAILURE_REPLY, brain.get_response(
                    "sys", [], "What should I make for thanksgiving", short_advice=True))


if __name__ == "__main__":
    unittest.main()
