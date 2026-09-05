import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _TraceStub:
    route = "unknown"

    def __init__(self, **_kwargs):
        pass

    def flag(self, _name):
        pass

    def set_route(self, route):
        self.route = route


def _load_handle_message(overrides):
    tree = ast.parse((ROOT / "davosbot" / "main.py").read_text(encoding="utf-8"))
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "handle_message"
    ]
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "_MessageTrace": _TraceStub,
        "_trace_call": lambda _trace, _phase, fn, *args, **kwargs: fn(*args, **kwargs),
        "_log_message_trace": lambda *_args, **_kwargs: None,
        "_log_quality_signal": lambda *_args, **_kwargs: None,
        "SLOW_MESSAGE_LOG_SECONDS": 999999,
        "check_rate_limit": lambda sender: True,
        "handle_dm": lambda sender, text, image_path=None, **_kwargs: None,
        "handle_group_message": lambda sender, chat_id, text, msg=None, **_kwargs: None,
        "is_imessage_reaction": lambda text, associated_message_type=None, associated_message_guid=None: False,
        "is_at_mentioned": lambda text: False,
        "is_group_chat": lambda chat_id: False,
        "logger": type("Logger", (), {"error": lambda *args, **kwargs: None})(),
        "log_error": lambda *args, **kwargs: None,
        "log_session_error": lambda *args, **kwargs: None,
        "redact_secret": lambda text: text,
        "send_message": lambda recipient, text, is_group=False: None,
        "traceback": type("Traceback", (), {"format_exc": lambda: ""})(),
        "update_heartbeat": lambda: None,
    }
    namespace.update(overrides)
    exec(compile(module, str(ROOT / "davosbot" / "main.py"), "exec"), namespace)
    return namespace["handle_message"]


class GroupRateLimitTests(unittest.TestCase):
    def test_passive_group_chatter_does_not_emit_rate_limit_reply(self):
        calls = []
        handler = _load_handle_message({
            "is_group_chat": lambda chat_id: True,
            "is_at_mentioned": lambda text: False,
            "check_rate_limit": lambda sender: calls.append(("rate", sender)) or False,
            "send_message": lambda recipient, text, is_group=False: calls.append(("send", recipient, is_group)),
            "handle_group_message": lambda sender, chat_id, text, msg=None: calls.append(("group", text)),
        })

        handler({"sender": "friend", "chat_identifier": "chat123", "text": "just chatting"})

        self.assertEqual([], calls)

    def test_mentioned_group_message_can_still_get_rate_limit_reply(self):
        calls = []
        handler = _load_handle_message({
            "is_group_chat": lambda chat_id: True,
            "is_at_mentioned": lambda text: True,
            "check_rate_limit": lambda sender: False,
            "send_message": lambda recipient, text, is_group=False: calls.append((recipient, text, is_group)),
        })

        handler({"sender": "friend", "chat_identifier": "chat123", "text": "@Davos help"})

        self.assertEqual(1, len(calls))
        self.assertEqual("chat123", calls[0][0])
        self.assertTrue(calls[0][2])
        self.assertIn("message limit", calls[0][1])


if __name__ == "__main__":
    unittest.main()
