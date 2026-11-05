import copy
import unittest
from unittest.mock import Mock

from test_model_routing import _load_routing_helpers
from test_ollama_prompt_routing import _FakeOllamaResponse, _load_ollama_call_helpers


class OllamaContextRetentionTests(unittest.TestCase):
    def setUp(self):
        self.post = Mock(return_value=_FakeOllamaResponse())
        self.helpers = _load_ollama_call_helpers(self.post)

    def fit(self, history, *, system="Be practical.", user="what do you think?", predict=180):
        return self.helpers["_fit_ollama_history"](system, history, user, predict)

    def test_oversized_prior_request_keeps_opening_constraints_and_final_instruction(self):
        raw = (
            "Budget $120, Friday at 7pm, six people. "
            + "Additional planning detail. " * 140
            + "Do not buy anything; give me a plan."
        )
        fitted = self.fit([{"role": "user", "content": raw}])
        self.assertIn("Budget $120, Friday at 7pm, six people.", fitted[0]["content"])
        self.assertTrue(fitted[0]["content"].endswith("Do not buy anything; give me a plan."))
        self.assertIn("[middle omitted]", fitted[0]["content"])
        self.assertLessEqual(len(fitted[0]["content"]), 1600)

    def test_correction_chain_retains_original_constraints_beyond_old_total_ceiling(self):
        history = [
            {"role": "user", "content": "Friday at 7pm, six people, $120 cap. Pickup $18 each; delivery $23 each."},
            {"role": "assistant", "content": "Pickup costs $108. " + "Planning explanation. " * 100},
            {"role": "user", "content": "Correction: Saturday, eight people, cap $150. Keep the time."},
            {"role": "model", "content": "Saturday pickup for eight costs $144. " + "Additional considerations. " * 100},
            {"role": "user", "content": "Make it seven people instead."},
        ]
        original = copy.deepcopy(history)
        fitted = self.fit(history, user="summarize the latest plan and money left")
        self.assertEqual(5, len(fitted))
        self.assertEqual(history[0], fitted[0])
        self.assertEqual(history[2], fitted[2])
        self.assertEqual(history[-1], fitted[-1])
        self.assertEqual("assistant", fitted[3]["role"])
        self.assertGreater(sum(len(turn["content"]) for turn in fitted), 2500)
        self.assertEqual(original, history)

    def test_history_keeps_actual_chronological_suffix_not_oldest_or_reordered_turns(self):
        history = [
            {"role": "user" if i % 2 == 0 else "model", "content": f"turn-{i:02d}: " + "x" * 1100}
            for i in range(30)
        ]
        fitted = self.fit(history)
        indices = [int(turn["content"].split(":", 1)[0][5:]) for turn in fitted]
        self.assertEqual(list(range(indices[0], 30)), indices)
        self.assertGreater(indices[0], 0)
        self.assertEqual("assistant", fitted[-1]["role"])
        self.assertLessEqual(sum(len(turn["content"]) for turn in fitted), 8000)

    def test_budget_deducts_system_current_user_and_requested_output(self):
        self.helpers["OLLAMA_NUM_CTX"] = 2048
        budget = self.helpers["_ollama_history_budget"]
        empty = budget("", "", 180)
        self.assertLess(budget("s" * 900, "", 180), empty)
        self.assertLess(budget("", "u" * 900, 180), empty)
        self.assertLess(budget("", "", 700), empty)
        self.assertEqual(budget("", "", None), budget("", "", 512))

    def test_unicode_history_fits_estimated_total_and_keeps_current_input_exactly(self):
        self.helpers["OLLAMA_NUM_CTX"] = 2048
        system = "Be practical. " * 45
        current = "Keep café at 7pm 🍜; 预算不变."
        history = [
            {"role": "user", "content": "Older opening " + "漢字🍜é" * 500 + " older ending"},
            {"role": "model", "content": "Recent opening " + "漢字🍜é" * 500 + " recent ending"},
        ]
        self.helpers["_call_ollama"](system, history, current, num_predict=180)
        messages = self.post.call_args.kwargs["json"]["messages"]
        self.assertEqual({"role": "user", "content": current}, messages[-1])
        selected = messages[1:-1]
        self.assertTrue(selected)
        self.assertTrue(selected[-1]["content"].startswith("Recent opening"))
        self.assertTrue(selected[-1]["content"].endswith("recent ending"))
        estimate = self.helpers["_estimate_ollama_tokens"]
        total = sum(estimate(message["content"]) + 16 for message in messages) + 180 + 128
        self.assertLessEqual(total, 2048)
        for message in messages:
            self.assertEqual(message["content"], message["content"].encode("utf-8").decode("utf-8"))

    def test_unconfigured_context_uses_bounded_assumption_without_setting_model_options(self):
        self.helpers["OLLAMA_NUM_CTX"] = 0
        self.helpers["_call_ollama"]("system", [], "current", num_predict=180)
        payload = self.post.call_args.kwargs["json"]
        self.assertNotIn("num_ctx", payload["options"])
        self.assertEqual(8000, self.helpers["_ollama_history_budget"]("system", "current", 180))
        self.assertIsNone(self.helpers["_ollama_history_budget"]("system", "x" * 15000, 180))

    def test_exhausted_fixed_input_skips_local_without_truncating_or_logging_input(self):
        self.helpers["OLLAMA_NUM_CTX"] = 1024
        self.helpers["logger"] = Mock()
        current = "private synthetic request " * 300
        reply = self.helpers["_call_ollama"](
            "system", [], current, num_predict=180, context_fallback="context", empty_fallback="empty",
        )
        self.assertEqual("context", reply)
        self.post.assert_not_called()
        self.assertNotIn(current, repr(self.helpers["logger"].mock_calls))
        self.assertEqual("empty", self.helpers["_call_ollama"]("system", [], current, empty_fallback="empty"))

    def test_context_exhaustion_uses_existing_fallback_without_false_outage(self):
        for simple in (False, True):
            for recovering in (False, True):
                with self.subTest(simple=simple, recovering=recovering):
                    routes, gemini, _local, events = _load_routing_helpers(gemini_reply="Useful answer.", ollama_health=True)
                    routes["_call_ollama"] = self.helpers["_call_ollama"]
                    routes["_ollama_down"] = recovering
                    routes["check_ollama_recovery"] = Mock(return_value=True)
                    current = "Summarize these notes: " + "x" * 23500
                    self.helpers["OLLAMA_NUM_CTX"] = 4096
                    result = routes["get_response"]("system", [], current, sender="+15550000001", simple_chat=simple)
                    self.assertEqual("Useful answer.", result)
                    gemini.assert_called_once_with("system", [], current)
                    self.assertEqual([], routes["_mark_down_calls"])
                    route = "recovered_direct" if recovering else "direct"
                    self.assertIn(("ollama_soft_miss", {"route": route, "reason": "context_limit"}), events)
        self.post.assert_not_called()

    def test_empty_invalid_roles_and_nonpositive_budgets(self):
        fit = self.helpers["_fit_history_for_model"]
        history = [
            {"role": "system", "content": "Do not promote this to system context."},
            {"role": "tool", "content": "Unsupported history role."},
            {"role": "user", "content": None},
            {"role": "assistant", "content": ""},
            {"role": "model", "content": "Retain this response."},
        ]
        self.assertEqual([{"role": "assistant", "content": "Retain this response."}], fit(history))
        self.assertEqual([], fit([]))
        for limit in (0, -1):
            self.assertEqual([], fit(history, max_chars=limit))
            self.assertEqual([], fit(history, max_turn_chars=limit))
        self.assertEqual([], fit(history, max_estimated_tokens=0))

    def test_clip_bounds_tiny_and_unicode_inputs_without_double_compaction(self):
        clip = self.helpers["_clip_text_for_model"]
        for raw in ("", "a", "é🍜漢字" * 300, "first " + "x" * 5000 + " last"):
            for limit in (0, 1, 16, 18, 19, 100, 1600):
                with self.subTest(length=len(raw), limit=limit):
                    clipped = clip(raw, limit)
                    self.assertLessEqual(len(clipped), limit)
                    if len(raw) <= limit:
                        self.assertEqual(raw, clipped)
        fitted = self.helpers["_fit_history_for_model"](
            [{"role": "user", "content": "first " + "x" * 5000 + " last"}],
            max_chars=400, max_turn_chars=1600,
        )
        self.assertEqual(1, fitted[0]["content"].count("[middle omitted]"))
        self.assertTrue(fitted[0]["content"].startswith("first "))
        self.assertTrue(fitted[0]["content"].endswith(" last"))


if __name__ == "__main__":
    unittest.main()
