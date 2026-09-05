import ast
import logging
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load_command_functions(*names):
    tree = ast.parse((ROOT / "davosbot" / "commands.py").read_text(encoding="utf-8"))
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "re": re,
        "logger": logging.getLogger("test.commands"),
        "get_persona": lambda chat_id: "baby gal",
        "__name__": "davosbot.commands",
        "__package__": "davosbot",
    }
    exec(compile(module, str(ROOT / "davosbot" / "commands.py"), "exec"), namespace)
    return namespace


class GroupTellTests(unittest.TestCase):
    def setUp(self):
        funcs = _load_command_functions("_parse_group_tell", "_format_group_tell")
        self.parse_group_tell = funcs["_parse_group_tell"]
        self.format_group_tell = funcs["_format_group_tell"]
        self.calls = []

    def _style_modules(self, reply="Chapman, baby, they said you finna get smoked in gawlf. Don't shoot the messenger."):
        def fake_get_response(system, history, prompt, use_tools=True, sender="", originating_chat_id=""):
            self.calls.append(
                {
                    "system": system,
                    "history": history,
                    "prompt": prompt,
                    "use_tools": use_tools,
                    "sender": sender,
                    "originating_chat_id": originating_chat_id,
                }
            )
            return reply

        return patch.dict(
            sys.modules,
            {
                "davosbot.brain": types.SimpleNamespace(get_response=fake_get_response),
                "davosbot.personality": types.SimpleNamespace(
                    build_system_prompt=lambda persona=None, user_text="": f"persona={persona}; user={user_text}"
                ),
            },
        )

    def test_group_tell_parses_plain_and_multiword_targets(self):
        self.assertEqual(
            ("chapman", "he finna get smoked in gawlf"),
            self.parse_group_tell("@Davos tell chapman he finna get smoked in gawlf"),
        )
        self.assertEqual(
            ("Hansi Flick", "rotate the squad"),
            self.parse_group_tell('@davos tell "Hansi Flick" rotate the squad'),
        )
        self.assertEqual(
            ("Hansi Flick", "rotate the squad"),
            self.parse_group_tell("@davos tell Hansi Flick: rotate the squad"),
        )

    def test_group_tell_rejects_chatwide_targets(self):
        self.assertIsNone(self.parse_group_tell("@Davos tell everyone hello"))
        self.assertIsNone(self.parse_group_tell("@Davos tell the chat hello"))
        self.assertIsNone(self.parse_group_tell("@Davos tell us hello"))

    def test_group_tell_uses_active_persona_without_tools_or_private_send_claim(self):
        with self._style_modules():
            reply = self.format_group_tell(
                "Chapman",
                "he finna get smoked in gawlf",
                chat_id="group-chat-guid",
                sender="+15550000001",
            )

        self.assertIn("Chapman", reply)
        self.assertNotIn("sent a private text", reply.lower())
        self.assertEqual(1, len(self.calls))
        self.assertFalse(self.calls[0]["use_tools"])
        self.assertEqual("+15550000001", self.calls[0]["sender"])
        self.assertEqual("group-chat-guid", self.calls[0]["originating_chat_id"])
        self.assertIn("do not claim any private action happened", self.calls[0]["prompt"])

    def test_group_tell_has_safe_fallback_when_styling_fails(self):
        def boom(*args, **kwargs):
            raise RuntimeError("model down")

        with patch.dict(
            sys.modules,
            {
                "davosbot.brain": types.SimpleNamespace(get_response=boom),
                "davosbot.personality": types.SimpleNamespace(
                    build_system_prompt=lambda persona=None, user_text="": "unused"
                ),
            },
        ):
            reply = self.format_group_tell("Chapman", "hello", chat_id="group-chat-guid", sender="+15550000001")

        self.assertEqual("Chapman, they said hello. Don't shoot the messenger.", reply)

    def test_group_ping_is_local_command(self):
        funcs = _load_command_functions("handle_group_command")
        funcs["is_owner"] = lambda sender: True
        funcs["_parse_group_tell"] = lambda text: None

        reply = funcs["handle_group_command"]("+15550000001", "group-chat-guid", "@Davos ping")

        self.assertEqual("pong \u2014 routing confirmed", reply)

    def test_group_model_options_routes_to_local_command(self):
        funcs = _load_command_functions("handle_group_command")
        funcs["is_owner"] = lambda sender: True
        funcs["_parse_group_tell"] = lambda text: None
        funcs["_cmd_model"] = lambda text, sender: f"model::{text}::{sender}"

        reply = funcs["handle_group_command"]("+15550000001", "group-chat-guid", "@Davos model options")

        self.assertEqual("model::model options::+15550000001", reply)


if __name__ == "__main__":
    unittest.main()
