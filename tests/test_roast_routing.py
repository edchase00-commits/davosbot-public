import ast
import re
import unittest
from pathlib import Path

from davosbot import simple_chat


ROOT = Path(__file__).resolve().parents[1]


def _load_roast_helpers():
    tree = ast.parse((ROOT / "davosbot" / "main.py").read_text(encoding="utf-8"))
    wanted_assigns = {
        "_ROAST_REQUEST_RE",
        "_ROAST_SEARCH_RE",
        "_FOOD_ROAST_RE",
        "_LIVE_INFO_TOOL_RE",
        "_OWNER_SIDE_EFFECT_TOOL_RE",
        "_SHORT_CHAT_ONLY_RE",
        "_PRIDE_HORNY_BANTER_RE",
        "_VIRAL_MEME_BANTER_RE",
        "_HUMOR_BANTER_RE",
        "_VIRAL_BANTER_REPLIES",
    }
    wanted_funcs = {
        "_is_roast_request",
        "_should_keep_roast_chat_only",
        "_looks_like_plain_chat",
        "_fast_chat_reply",
        "_should_use_limited_web_tools",
        "_should_use_owner_tools",
        "_is_simple_group_chatter",
        "_owner_group_should_use_tools",
        "_stable_reply_choice",
        "_viral_banter_reply",
    }
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id in wanted_assigns for target in node.targets):
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted_funcs:
            nodes.append(node)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"re": re, "_simple_chat": simple_chat}
    exec(compile(module, str(ROOT / "davosbot" / "main.py"), "exec"), namespace)
    return namespace


class RoastRoutingTests(unittest.TestCase):
    def test_roasts_stay_chat_only_instead_of_tool_loop(self):
        helpers = _load_roast_helpers()

        self.assertTrue(helpers["_is_roast_request"]("atl roast my friend"))
        self.assertTrue(helpers["_should_keep_roast_chat_only"]("roast my friend for that fit"))
        self.assertTrue(helpers["_is_simple_group_chatter"]("roast my friend for that fit"))
        self.assertFalse(helpers["_owner_group_should_use_tools"]("roast my friend for that fit", False))

    def test_current_info_roasts_can_still_use_tools(self):
        helpers = _load_roast_helpers()

        self.assertTrue(helpers["_is_roast_request"]("look up today's score and roast ATL"))
        self.assertFalse(helpers["_should_keep_roast_chat_only"]("look up today's score and roast ATL"))
        self.assertTrue(helpers["_owner_group_should_use_tools"]("look up today's score and roast ATL", False))

    def test_food_roasting_does_not_trigger_banter_mode(self):
        helpers = _load_roast_helpers()

        self.assertFalse(helpers["_is_roast_request"]("how long should I roast chicken"))
        self.assertFalse(helpers["_should_keep_roast_chat_only"]("how long should I roast chicken"))

    def test_plain_chat_skips_tool_loop(self):
        helpers = _load_roast_helpers()

        self.assertTrue(helpers["_looks_like_plain_chat"]("what's up"))
        self.assertFalse(helpers["_should_use_owner_tools"]("what's up", False))
        self.assertFalse(helpers["_should_use_owner_tools"]("what should I eat tonight?", False))
        self.assertFalse(helpers["_should_use_limited_web_tools"]("what's up", False))

    def test_trivial_casual_chat_gets_fast_reply(self):
        helpers = _load_roast_helpers()

        self.assertIsNotNone(helpers["_fast_chat_reply"]("yo"))
        self.assertIsNotNone(helpers["_fast_chat_reply"]("gm"))
        self.assertIsNotNone(helpers["_fast_chat_reply"]("welcome back"))
        self.assertIsNotNone(helpers["_fast_chat_reply"]("we missed you"))
        self.assertIsNotNone(helpers["_fast_chat_reply"]("what are you doing"))
        self.assertIsNotNone(helpers["_fast_chat_reply"]("are you alive"))
        self.assertEqual("pong.", helpers["_fast_chat_reply"]("ping"))
        self.assertIsNotNone(helpers["_fast_chat_reply"]("lol"))
        self.assertIsNotNone(helpers["_fast_chat_reply"]("thanks"))
        self.assertIsNotNone(helpers["_fast_chat_reply"]("bet"))
        self.assertIsNone(helpers["_fast_chat_reply"]("yo can you list reminders"))
        self.assertIsNone(helpers["_fast_chat_reply"]("what's the weather"))

    def test_live_info_and_side_effects_still_use_tools(self):
        helpers = _load_roast_helpers()

        self.assertTrue(helpers["_should_use_limited_web_tools"]("what's the weather in Seattle?", False))
        self.assertTrue(helpers["_should_use_owner_tools"]("what's the weather in Seattle?", False))
        self.assertTrue(helpers["_should_use_owner_tools"]("bench 185 x 5 x 3", False))
        self.assertFalse(helpers["_should_use_limited_web_tools"]("bench 185 x 5 x 3", False))
        self.assertTrue(helpers["_should_use_owner_tools"]("schedule a cron every morning at 6:30", False))
        self.assertTrue(helpers["_should_use_owner_tools"]("can you read the logs?", False))

    def test_no_search_suppresses_tool_routes(self):
        helpers = _load_roast_helpers()

        self.assertFalse(helpers["_should_use_limited_web_tools"]("search the web for Mariners news", True))
        self.assertFalse(helpers["_should_use_owner_tools"]("search the web for Mariners news", True))

    def test_computa_pride_horny_prompt_gets_deterministic_banter(self):
        helpers = _load_roast_helpers()

        reply = helpers["_viral_banter_reply"]("computa make these guys super gay and horny")

        self.assertIn("Horny DLC", reply)
        self.assertFalse(helpers["_should_use_owner_tools"]("computa make these guys super gay and horny", False))

    def test_viral_meme_banter_does_not_steal_live_info(self):
        helpers = _load_roast_helpers()

        self.assertIsNone(helpers["_viral_banter_reply"]("make this skibidi bit funny"))
        self.assertIsNone(helpers["_viral_banter_reply"]("search latest skibidi news"))
        self.assertIsNone(helpers["_viral_banter_reply"]("make this skibidi image funny", has_image=True))


if __name__ == "__main__":
    unittest.main()
