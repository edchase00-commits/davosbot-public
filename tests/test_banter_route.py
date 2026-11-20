import unittest
from unittest import mock

from davosbot import brain


def _big_system() -> str:
    identity = "DAVOS IDENTITY " + ("who davos is " * 200)
    rules = "\n\n## Voice and boundaries\n" + ("voice rules and safety rules " * 300)
    facts = "\n\n## FACTS\nFACTMARKER " + ("durable fact filler " * 300)
    return identity + rules + facts


class CompactBanterSystemTests(unittest.TestCase):
    def test_compact_is_smaller_keeps_voice_and_examples_drops_facts(self):
        system = _big_system()
        compact = brain._compact_banter_system(system)
        self.assertLess(len(compact), len(system))
        self.assertLessEqual(len(compact), brain._BANTER_SYSTEM_MAX_CHARS)
        self.assertIn("DAVOS IDENTITY", compact)
        self.assertIn("Voice and boundaries", compact)
        self.assertIn("Banter style examples", compact)
        self.assertNotIn("FACTMARKER", compact)

    def test_compact_handles_unsectioned_system(self):
        compact = brain._compact_banter_system("plain system prompt " * 400)
        self.assertLessEqual(len(compact), brain._BANTER_SYSTEM_MAX_CHARS)
        self.assertIn("Banter style examples", compact)


class SimpleChatRetryTests(unittest.TestCase):
    def test_retries_once_on_miss_then_returns_reply(self):
        with mock.patch.object(brain, "_call_ollama", side_effect=[None, "warm reply"]) as call:
            reply = brain._call_ollama_simple_chat("sys", [], "roast me")
        self.assertEqual(reply, "warm reply")
        self.assertEqual(call.call_count, 2)
        self.assertTrue(call.call_args.kwargs["compact_banter"])
        self.assertEqual(call.call_args.kwargs["model"], brain._ollama_simple_chat_model())

    def test_sentinel_returned_without_retry(self):
        with mock.patch.object(
            brain, "_call_ollama", side_effect=[brain._OLLAMA_TRUNCATED_SENTINEL]
        ) as call:
            reply = brain._call_ollama_simple_chat("sys", [], "roast me")
        self.assertEqual(reply, brain._OLLAMA_TRUNCATED_SENTINEL)
        self.assertEqual(call.call_count, 1)

    def test_two_misses_return_none(self):
        with mock.patch.object(brain, "_call_ollama", side_effect=[None, None]) as call:
            reply = brain._call_ollama_simple_chat("sys", [], "roast me")
        self.assertIsNone(reply)
        self.assertEqual(call.call_count, 2)


if __name__ == "__main__":
    unittest.main()
