import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from davosbot import personality
class PersonalityPromptTests(unittest.TestCase):
    def test_core_voice_allows_normal_curse_word_banter(self):
        rules = personality._core_behavior_instructions().lower()

        self.assertIn("bitch", rules)
        self.assertIn("ordinary curse words", rules)
        self.assertIn("normal curse word", rules)
        self.assertIn("protected-class slurs", rules)
        self.assertIn("owner/admin harmless roast", rules)
        self.assertIn("never call the owner or users `my g`", rules)
        self.assertIn("as a large language model", rules)
        self.assertIn("you are davosbot in this chat", rules)
        self.assertIn("one-off style asks", rules)
        self.assertIn("apply tone/persona corrections silently", rules)
        self.assertIn("decatur behavior/style is not ambient", rules)
        self.assertIn("requested sequence exactly", rules)

    def test_roast_requests_get_dedicated_style_instructions(self):
        prompt = personality.build_system_prompt(
            user_text="atl roast my friend for that outfit"
        ).lower()

        self.assertIn("roast mode", prompt)
        self.assertIn("be sharper", prompt)
        self.assertIn("atl/atlanta as the roast target", prompt)
        self.assertIn("not as a hidden persona switch", prompt)
        self.assertIn("do not spam emojis", prompt)

    def test_work_queue_questions_include_self_knowledge(self):
        prompt = personality.build_system_prompt(
            user_text="what can ship safe cleanup do?"
        ).lower()

        self.assertIn("your own codebase and architecture", prompt)
        self.assertIn("ship safe cleanup", prompt)
        self.assertIn("green/yellow/red", prompt)
        self.assertIn("does not edit files", prompt)

    def test_fix_yourself_questions_include_self_repair_knowledge(self):
        prompt = personality.build_system_prompt(
            user_text="what does fix yourself do?"
        ).lower()

        self.assertIn("fix yourself", prompt)
        self.assertIn("review-only repair rows", prompt)
        self.assertIn("does not", prompt)
        self.assertIn("auto-deploy", prompt)

    def test_sports_preferences_and_live_score_honesty_are_centralized(self):
        rules = personality._ethan_preference_instructions().lower()

        self.assertIn("unc tar heels", rules)
        self.assertIn("fc barcelona", rules)
        self.assertIn("indiana pacers", rules)
        self.assertIn("seattle mariners", rules)
        self.assertIn("anti-arsenal", rules)
        self.assertIn("lamine yamal", rules)
        self.assertIn("tyrese haliburton", rules)
        self.assertIn("do not invent live scores", rules)

    def test_message_relevant_memory_is_promoted_before_bulk_facts(self):
        with TemporaryDirectory() as tmp:
            soul = Path(tmp) / "SOUL.md"
            soul.write_text(
                "You are DavosBot.\n\n"
                "## FACTS — treat these as ground truth\n"
                "A live SOUL file can have its own facts-style heading.\n",
                encoding="utf-8",
            )
            memory = Path(tmp) / "MEMORY.md"
            memory.write_text(
                "## Old stuff\n"
                + ("unrelated memory line\n" * 400)
                + "\n## Decatur behavior\n"
                + "When the owner explicitly invokes Decatur, keep the saved Decatur behavior sharp and specific.\n"
                + "\n## More stuff\n"
                + ("another unrelated memory line\n" * 400),
                encoding="utf-8",
            )

            with (
                patch.object(personality, "SOUL_PATH", str(soul)),
                patch.object(personality, "MEMORY_PATH", str(memory)),
            ):
                prompt = personality.build_system_prompt(user_text="what happened to Decatur behavior?")

        self.assertIn("RELEVANT FACTS - highest priority", prompt)
        self.assertIn("When the owner explicitly invokes Decatur", prompt)
        bulk_facts_marker = "## FACTS — treat these as ground truth"
        self.assertIn(bulk_facts_marker, prompt)
        self.assertLess(
            prompt.index("RELEVANT FACTS - highest priority"),
            prompt.rindex(bulk_facts_marker),
        )

    def test_light_chat_prompt_uses_relevant_memory_without_bulk_facts(self):
        with TemporaryDirectory() as tmp:
            soul = Path(tmp) / "SOUL.md"
            soul.write_text(
                "You are DavosBot, the owner's funny friend in iMessage.\n"
                "- Be spontaneous, warm, and a little ridiculous on casual pings.\n"
                "- Never answer greetings like a dead status monitor.\n",
                encoding="utf-8",
            )
            memory = Path(tmp) / "MEMORY.md"
            memory.write_text(
                "## Old stuff\n"
                + ("unrelated memory line\n" * 400)
                + "\n## Pacers\n"
                + "the owner likes the Pacers and Tyrese Haliburton.\n",
                encoding="utf-8",
            )

            with (
                patch.object(personality, "SOUL_PATH", str(soul)),
                patch.object(personality, "MEMORY_PATH", str(memory)),
            ):
                prompt = personality.build_light_chat_system_prompt(user_text="pacers lol")

        self.assertIn("Plain Chat Mode", prompt)
        self.assertIn("Default Personality", prompt)
        self.assertIn("the owner's funny friend", prompt)
        self.assertIn("dead status monitor", prompt)
        self.assertIn("the owner likes the Pacers", prompt)
        self.assertIn("RELEVANT FACTS - highest priority", prompt)
        self.assertNotIn("## FACTS", prompt)
        self.assertNotIn("unrelated memory line", prompt)
        self.assertLess(len(prompt), 2600)

    def test_decatur_behavior_trigger_is_prompted_and_enforced_for_atl(self):
        prompt = personality.build_system_prompt(
            persona="ATL",
            user_text="I'm giving Decatur behavior",
        )

        self.assertIn("ATL Decatur Behavior Trigger", prompt)
        self.assertIn(personality.DECATUR_BEHAVIOR_EMOJIS, prompt)
        self.assertEqual(
            "Say less.\n" + personality.DECATUR_BEHAVIOR_EMOJIS,
            personality.enforce_decatur_behavior_reply("Say less.", "ATL", "Decatur behavior"),
        )

    def test_explicit_decatur_behavior_survives_persona_state_loss(self):
        prompt = personality.build_system_prompt(
            persona=None,
            user_text="Decatur behavior",
        )

        self.assertIn("ATL Decatur Behavior Trigger", prompt)
        self.assertEqual(
            "normal reply\n" + personality.DECATUR_BEHAVIOR_EMOJIS,
            personality.enforce_decatur_behavior_reply("normal reply", None, "Decatur behavior"),
        )

    def test_decatur_behavior_does_not_leak_on_ambient_decatur_mentions(self):
        prompt = personality.build_system_prompt(
            persona=None,
            user_text="tell me about Decatur",
        )

        self.assertNotIn("ATL Decatur Behavior Trigger", prompt)
        self.assertEqual(
            "normal reply",
            personality.enforce_decatur_behavior_reply("normal reply", None, "tell me about Decatur"),
        )

    def test_decatur_behavior_fast_reply_uses_latest_saved_pack(self):
        reply = personality.decatur_behavior_fast_reply(None, "what is Decatur behavior?")

        self.assertIn("Chile, Decatur behavior", reply)
        self.assertIn("Ring camera soundtrack", reply)
        self.assertTrue(reply.endswith(personality.DECATUR_BEHAVIOR_EMOJIS))
        self.assertEqual("💣🔫🥷🏿💥🚨🚔👮‍♂️🫃🏿🧜🏿‍♂️", personality.DECATUR_BEHAVIOR_EMOJIS)

    def test_decatur_behavior_action_fast_reply_has_atl_bit(self):
        reply = personality.decatur_behavior_fast_reply(None, "I'm giving Decatur behavior")

        self.assertIn("Chile, yes", reply)
        self.assertIn("who car is this", reply)
        self.assertTrue(reply.endswith(personality.DECATUR_BEHAVIOR_EMOJIS))

    def test_decatur_behavior_emoji_query_returns_full_saved_pack(self):
        reply = personality.decatur_behavior_fast_reply(
            "ATL",
            "from now on if we mention Decatur behavior in ATL persona use these emojis",
        )

        self.assertEqual(
            "Decatur behavior emojis:\n💣🔫🥷🏿💥🚨🚔👮‍♂️🫃🏿🧜🏿‍♂️",
            reply,
        )

    def test_stale_model_memory_fact_is_suppressed(self):
        with TemporaryDirectory() as tmp:
            memory = Path(tmp) / "MEMORY.md"
            memory.write_text(
                "# Memory\n"
                "- the owner likes the Pacers.\n"
                "- Bot runs on Gemma 3 via Ollama with Gemini 2.5 Flash as fallback.\n",
                encoding="utf-8",
            )

            with patch.object(personality, "MEMORY_PATH", str(memory)):
                prompt = personality.build_system_prompt(user_text="what models do you use?")

        self.assertIn("the owner likes the Pacers", prompt)
        self.assertNotIn("Gemini 2.5 Flash as fallback", prompt)
        self.assertIn("Runtime model truth", prompt)
        self.assertIn("BOT_SELF_KNOWLEDGE.md", prompt)

    def test_stale_model_soul_fact_is_suppressed(self):
        with TemporaryDirectory() as tmp:
            soul = Path(tmp) / "SOUL.md"
            soul.write_text(
                "You are DavosBot.\n"
                "- Powered by Gemma 3 via Ollama locally. When Ollama is down, you fall back to Gemini 2.5 Flash.\n",
                encoding="utf-8",
            )

            with patch.object(personality, "SOUL_PATH", str(soul)):
                prompt = personality.build_system_prompt(user_text="what models do you use?")

        self.assertIn("You are DavosBot", prompt)
        self.assertNotIn("Powered by Gemma 3", prompt)
        self.assertNotIn("Gemini 2.5 Flash", prompt)
        self.assertIn("Runtime model truth", prompt)

    def test_missing_soul_uses_tracked_example_fallback(self):
        with TemporaryDirectory() as tmp:
            missing_soul = str(Path(tmp) / "SOUL.md")
            example = Path(tmp) / "SOUL.example.md"
            example.write_text("hello {owner_name}", encoding="utf-8")

            with (
                patch.object(personality, "SOUL_PATH", missing_soul),
                patch.object(personality, "_SOUL_EXAMPLE_MD", example),
            ):
                self.assertEqual("hello the owner", personality.load_soul())


if __name__ == "__main__":
    unittest.main()
