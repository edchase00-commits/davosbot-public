"""Actual handler-to-provider history budgets, not an abbreviated eval harness."""

import copy
import unittest
from unittest.mock import patch

import test_group_context as fixture
from davosbot import brain, group_chat, group_context, main, memory, personality
from test_ollama_prompt_routing import _FakeOllamaResponse


REQUEST = "@Davos draft an invite based on all that."


class GroupContextBudgetTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.GroupDiscussionContextTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.stack.enter_context(patch.object(brain, "BOT_DB_PATH", self.f.db_path))
        self.f.stack.enter_context(patch.object(brain, "_log_bot_event"))
        self.f.stack.enter_context(patch.object(brain, "_ollama_down", False))
        for name in ("MODEL_ROUTE_SIMPLE_CHAT", "MODEL_ROUTE_COMPLEX_REASONING", "MODEL_ROUTE_CODE_REVIEW"):
            self.f.stack.enter_context(patch.object(brain, name, "ollama:gemma3"))
        self.f.stack.enter_context(patch.object(group_chat, "is_approved_user", side_effect=lambda sender: sender in self.f.allowed))
        self.f.stack.enter_context(patch.object(personality, "load_soul", return_value="Synthetic shared Davos persona."))
        self.f.stack.enter_context(patch.object(personality, "load_memory", return_value=""))
        self.f.stack.enter_context(patch.object(personality, "load_persona", return_value=None))
        self.f.stack.enter_context(patch.object(personality, "format_style_directives_for_prompt", return_value=""))
        self.f.stack.enter_context(patch.object(personality, "_current_time_instructions", return_value="Synthetic fixed date."))
        for name in ("build_system_prompt", "build_light_chat_system_prompt"):
            self.f.stack.enter_context(patch.object(main, name, side_effect=getattr(personality, name)))
        self.cloud = self.f.stack.enter_context(patch.object(brain, "_call_gemini", side_effect=AssertionError("Unexpected cloud route")))
        self.agentic = self.f.stack.enter_context(patch.object(brain, "_call_gemini_agentic", side_effect=AssertionError("Unexpected tool route")))
        self.post = self.f.stack.enter_context(patch.object(brain.requests, "post", return_value=_FakeOllamaResponse()))
        self.f.model.side_effect = brain.get_response

    def history(self):
        self.post.assert_called()
        messages = copy.deepcopy(self.post.call_args.kwargs["json"]["messages"])
        history = messages[1:-1]
        self.assertLessEqual(sum(len(turn["content"]) for turn in history), 8000)
        self.assertTrue(all(len(turn["content"]) <= 1600 for turn in history))
        self.assertFalse(self.cloud.called or self.agentic.called)
        return history

    def fill_ambient(self):
        # Select a near-full packet under the existing whole-message chunking,
        # then collect that packet through actual silent message dispatch.
        best = (0, [])
        for width in range(600, 1101, 5):
            rows = [("The Friday picnic plan note " + str(i) + ": " + "Additional venue detail. " * 50)[:width] for i in range(7)]
            group_context._recent.clear()
            for row in rows:
                group_context.remember(fixture.GROUP, fixture.FRIEND, row)
            size = sum(len(turn["content"]) for turn in group_context.quoted_history(fixture.GROUP, REQUEST, sender_allowed=lambda sender: True))
            if size > best[0]:
                best = (size, rows)
        group_context._recent.clear()
        self.assertGreater(best[0], 7800)
        for row in best[1]:
            self.f.route(row, sender=fixture.FRIEND)
        self.post.assert_not_called()
        self.f.send.assert_not_called()
        self.assertEqual([], memory.get_history(fixture.GROUP))

    def test_near_full_older_background_cannot_evict_new_directed_correction(self):
        self.fill_ambient()
        correction = "Correction: the picnic is now SUNDAY, not Friday. " + "The revised plan takes priority. " * 8
        self.f.route("@Davos " + correction)
        self.assertIn("SUNDAY", memory.get_history(fixture.GROUP)[-2]["content"])
        self.f.route(REQUEST)
        self.assertTrue(any("SUNDAY" in turn["content"] for turn in self.f.model.call_args.args[1]))
        final = self.history()
        self.assertTrue(any("SUNDAY" in turn["content"] for turn in final))
        self.assertTrue(any("Quoted recent group discussion" in turn["content"] for turn in final))
        self.assertEqual("local ok", final[-1]["content"])
        self.assertFalse(any("Quoted recent group discussion" in turn["content"] for turn in memory.get_history(fixture.GROUP)))

    def test_newer_ambient_correction_is_still_supplied_when_it_fits(self):
        self.f.route("@Davos The picnic is on Friday at noon.")
        self.f.now += 60
        self.f.route("Correction from the organizer: the picnic is now Sunday at noon.", sender=fixture.FRIEND)
        self.f.route(REQUEST)
        final = self.history()
        quoted = [turn["content"] for turn in final if turn["content"].startswith("Quoted recent group discussion")]
        self.assertTrue(any("now Sunday at noon" in text for text in quoted))
        self.assertTrue(any("Friday at noon" in turn["content"] for turn in final))
        self.assertTrue(any('"minutes_ago": 0' in text for text in quoted))
        self.assertIn("background only", quoted[0])
        self.assertFalse(any("organizer" in turn["content"] for turn in memory.get_history(fixture.GROUP)))

    def test_full_directed_context_is_prioritized_without_growing_provider_budget(self):
        self.f.route("The old picnic plan uses blue lanterns.", sender=fixture.FRIEND)
        for index in range(10):
            memory.save_turn(fixture.GROUP, "user", fixture.OWNER + ": Directed plan " + str(index) + ". " + "More directed detail. " * 60)
        self.f.route(REQUEST)
        final = self.history()
        self.assertTrue(any("Directed plan 9." in turn["content"] for turn in final))
        self.assertFalse(any("blue lanterns" in turn["content"] for turn in final))
        self.assertEqual(REQUEST.removeprefix("@Davos "), self.post.call_args.kwargs["json"]["messages"][-1]["content"])

    def test_existing_long_turn_head_and_tail_survive_actual_dispatch(self):
        text = "Budget is $120; Friday at 7pm; six people. " + "Extra venue planning details. " * 140 + "Do not buy anything; only give a plan."
        memory.save_turn(fixture.GROUP, "user", fixture.OWNER + ": " + text)
        self.f.route("@Davos Can you recap those constraints?")
        final = self.history()
        self.assertTrue(any("Budget is $120; Friday at 7pm; six people." in turn["content"] for turn in final))
        self.assertTrue(any("Do not buy anything; only give a plan." in turn["content"] for turn in final))
        self.assertTrue(any("[middle omitted]" in turn["content"] for turn in final))


if __name__ == "__main__":
    unittest.main()
