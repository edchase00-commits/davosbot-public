import unittest
from unittest import mock

from davosbot import brain


def _big_system() -> str:
    identity = "DAVOS IDENTITY " + ("who davos is " * 200)
    rules = "\n\n## Voice and boundaries\n" + ("voice rules and safety rules " * 300)
    facts = "\n\n## FACTS\nFACTMARKER " + ("durable fact filler " * 300)
    return identity + rules + facts


class CompactBanterSystemTests(unittest.TestCase):
    def test_compact_is_smaller_keeps_voice_but_drops_bulk_facts(self):
        system = _big_system()
        compact = brain._compact_banter_system(system)
        self.assertLess(len(compact), len(system))
        self.assertLessEqual(len(compact), brain._BANTER_SYSTEM_MAX_CHARS)
        self.assertIn("DAVOS IDENTITY", compact)
        self.assertIn("Voice and boundaries", compact)
        self.assertIn("Fast chat voice", compact)
        self.assertNotIn("FACTMARKER", compact)

    def test_compact_handles_unsectioned_system(self):
        compact = brain._compact_banter_system("plain system prompt " * 400)
        self.assertLessEqual(len(compact), brain._BANTER_SYSTEM_MAX_CHARS)
        self.assertIn("Fast chat voice", compact)


    def test_late_active_voice_and_scoped_style_survive_without_example_leakage(self):
        identity = ("You are Davos.\n\n## Active Persona\n" + "style " * 160 + "LATE_PERSONA")
        scoped = "\n\n## Style Directives\n- #synthetic [chat] SCOPED_VOICE"
        relevant = "\n\n## RELEVANT FACTS\nRELEVANT_SCOPED_FACT"
        bulk = "\n\n## FACTS\nBULK_MEMORY"
        compact = brain._compact_banter_system(identity + scoped + relevant + bulk)
        for text in ("LATE_PERSONA", "SCOPED_VOICE", "RELEVANT_SCOPED_FACT", "Never bypass owner-only"):
            self.assertIn(text, compact)
        self.assertNotIn("BULK_MEMORY", compact)
        self.assertNotIn("Department of Terrible Ideas", compact)
        self.assertNotIn("Banter style examples", compact)

    def test_large_scoped_style_stays_whole(self):
        scoped = "\n\n## Style Directives\n" + ("a scoped style. " * 260) + "END_STYLE"
        light_identity = ("Davos voice " * 106) + "ACTIVE_VOICE_END"
        compact = brain._compact_banter_system(light_identity + scoped)
        self.assertIn("END_STYLE", compact)
        self.assertIn("ACTIVE_VOICE_END", compact)
        self.assertIn("Never bypass owner-only", compact)
        self.assertLessEqual(len(compact), brain._MAX_OLLAMA_SYSTEM_CHARS)


class SimpleChatRetryTests(unittest.TestCase):
    def test_retries_once_on_miss_then_returns_reply(self):
        with mock.patch.object(brain, "_call_ollama", side_effect=[None, "warm reply"]) as call:
            reply = brain._call_ollama_simple_chat("sys", [], "roast me")
        self.assertEqual(reply, "warm reply")
        self.assertEqual(call.call_count, 2)
        self.assertTrue(call.call_args.kwargs["compact_banter"])
        self.assertEqual(call.call_args.kwargs["model"], brain._ollama_simple_chat_model())

    def test_warm_retry_allows_a_slow_reply_without_changing_fast_path(self):
        with mock.patch.object(brain, "OLLAMA_SIMPLE_CHAT_TIMEOUT", 3.5), mock.patch.object(
            brain, "_call_ollama", side_effect=[None, "reply"]
        ) as call:
            reply = brain._call_ollama_simple_chat("sys", [], "test")
        self.assertEqual("reply", reply)
        self.assertEqual([3.5, 7.0], [item.kwargs["timeout"] for item in call.call_args_list])

    def test_retry_never_shortens_explicit_longer_timeout(self):
        with mock.patch.object(brain, "OLLAMA_SIMPLE_CHAT_TIMEOUT", 9), mock.patch.object(
            brain, "_call_ollama", side_effect=[None, "reply"]
        ) as call:
            brain._call_ollama_simple_chat("sys", [], "test")
        self.assertEqual([9, 9], [item.kwargs["timeout"] for item in call.call_args_list])

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
