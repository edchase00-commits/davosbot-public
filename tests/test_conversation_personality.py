import unittest
from contextlib import ExitStack
from unittest.mock import patch

from davosbot import personality


class ConversationPersonalityTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(personality, "load_soul", return_value="Default Davos identity."))
        stack.enter_context(patch.object(personality, "load_memory", return_value=""))
        stack.enter_context(patch.object(personality, "load_persona", return_value="Dry coach persona: sharp observations, no catchphrases."))
        stack.enter_context(patch.object(personality, "format_style_directives_for_prompt", return_value=""))

    def test_both_model_prompts_support_contextual_opinions_without_inventing_actions(self):
        for builder in (personality.build_system_prompt, personality.build_light_chat_system_prompt):
            with self.subTest(builder=builder.__name__):
                prompt = builder(user_text="what do you think?")
                self.assertIn("what do you think?", prompt)
                self.assertIn("supplied conversation", prompt)
                self.assertIn("clear take and a concrete reason", prompt)
                self.assertIn("a suggestion or draft is not execution", prompt)
                self.assertNotIn("command/task route should handle it", prompt)

    def test_light_planning_preserves_non_tool_boundary_and_short_reply_budget(self):
        prompt = personality.build_light_chat_system_prompt(user_text="what do you think of this plan?")
        self.assertIn("Discuss plans, choices, and drafts directly", prompt)
        self.assertIn("Do not claim to run tools", prompt)
        self.assertIn("Never reveal secrets", prompt)
        self.assertIn("1-3 short sentences", prompt)
        self.assertLess(len(prompt), 2600)

    def test_persona_identity_still_replaces_default_on_both_paths(self):
        for builder in (personality.build_system_prompt, personality.build_light_chat_system_prompt):
            with self.subTest(builder=builder.__name__):
                prompt = builder(persona="coach", user_text="be honest about this")
                self.assertIn("Dry coach persona", prompt)
                self.assertNotIn("Default Davos identity", prompt)
                self.assertIn("Push back when warranted", prompt)

    def test_literal_food_and_file_requests_do_not_inject_roast_mode(self):
        for text in (
            "how should I cook wings?", "roast the chicken", "cook dinner for six",
            "drag the file into the folder", "drag notes.csv into the folder",
            "drag and drop the image", "the flame keeps going out",
        ):
            with self.subTest(text=text):
                self.assertFalse(personality._needs_roast_mode(text))
                self.assertNotIn("## Roast Mode", personality.build_light_chat_system_prompt(user_text=text))

    def test_explicit_roasts_survive_literal_words_elsewhere_in_the_request(self):
        for text in (
            "roast my friend for wearing dress shoes to the gym",
            "cook my friend for wearing dress shoes",
            "roast my friend because he cannot cook chicken",
            "ATL roast",
        ):
            with self.subTest(text=text):
                self.assertTrue(personality._needs_roast_mode(text))
                self.assertIn("## Roast Mode", personality.build_system_prompt(user_text=text))

    def test_declined_roast_or_serious_request_is_not_forced_into_roast_mode(self):
        for text in ("don't roast me, just give me your take", "no jokes, roast chicken for how long?", "stop roasting me"):
            with self.subTest(text=text):
                self.assertFalse(personality._needs_roast_mode(text))
        prompt = personality.build_system_prompt(user_text="be serious, I missed the deadline")
        self.assertIn("Serious requests need a direct, useful answer", prompt)
        self.assertNotIn("## Roast Mode", prompt)

    def test_requested_neutral_analysis_does_not_require_a_homer_answer_first(self):
        rules = personality._ethan_preference_instructions()
        self.assertIn("lead with the evidence-based ranking", rules)
        self.assertNotIn("give the owner-biased answer first", rules)
        self.assertIn("Indiana Pacers", rules)
        self.assertIn("do not invent live scores", rules)


if __name__ == "__main__":
    unittest.main()
