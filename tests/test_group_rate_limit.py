import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

from davosbot.rate_limit_notice import NoticeThrottle


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
        "_remember_unmentioned_group_text": lambda *_args: None,
        "SLOW_MESSAGE_LOG_SECONDS": 999999,
        "check_rate_limit": lambda sender: True,
        "_rate_limit_notice": SimpleNamespace(allow=NoticeThrottle().allow),
        "handle_dm": lambda sender, text, image_path=None, **_kwargs: None,
        "handle_group_message": lambda sender, chat_id, text, msg=None, **_kwargs: None,
        "is_imessage_reaction": lambda text, associated_message_type=None, associated_message_guid=None: False,
        "is_at_mentioned": lambda text: False,
        "_is_group_cron_draft_followup": lambda sender, chat_id, text: False,
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
    def test_rejected_burst_emits_one_notice_and_never_dispatches(self):
        sent, dispatched = [], []
        handler = _load_handle_message({
            "is_group_chat": lambda chat: True,
            "is_at_mentioned": lambda text: True,
            "check_rate_limit": lambda sender: False,
            "send_message": lambda *args, **kwargs: sent.append((args, kwargs)),
            "handle_group_message": lambda *args, **kwargs: dispatched.append(args),
        })
        for row in range(30):
            handler({"sender": "friend", "chat_identifier": "group-a", "text": "@Davos hello", "rowid": row})
        self.assertEqual(1, len(sent))
        self.assertEqual([], dispatched)
        self.assertEqual("group-a", sent[0][0][0])

    def test_sender_destination_and_cooldown_are_independent(self):
        now = [100.0]
        throttle = NoticeThrottle(clock=lambda: now[0])
        self.assertTrue(throttle.allow("friend-a", "group-a"))
        self.assertFalse(throttle.allow("friend-a", "group-a"))
        self.assertTrue(throttle.allow("friend-b", "group-a"))
        self.assertTrue(throttle.allow("friend-a", "group-b"))
        self.assertTrue(throttle.allow("friend-a", "friend-a"))
        now[0] = 399.9
        self.assertFalse(throttle.allow("friend-a", "group-a"))
        now[0] = 400.0
        self.assertTrue(throttle.allow("friend-a", "group-a"))

    def test_concurrent_rejections_reserve_only_one_attempt(self):
        throttle = NoticeThrottle()
        with ThreadPoolExecutor(max_workers=8) as workers:
            accepted = list(workers.map(lambda _: throttle.allow("friend", "group"), range(40)))
        self.assertEqual(1, sum(accepted))

    def test_cache_has_a_fixed_size_and_expires_old_keys(self):
        now = [100.0]
        throttle = NoticeThrottle(max_keys=3, clock=lambda: now[0])
        for sender in ("a", "b", "c", "d"):
            self.assertTrue(throttle.allow(sender, "group"))
        self.assertEqual(3, len(throttle._until))
        self.assertNotIn(("a", "group"), throttle._until)
        now[0] = 401.0
        self.assertTrue(throttle.allow("e", "group"))
        self.assertEqual([("e", "group")], list(throttle._until))

    def test_allowed_request_dispatches_even_during_notice_cooldown(self):
        throttle = NoticeThrottle()
        throttle.allow("friend", "group")
        dispatched, sent = [], []
        handler = _load_handle_message({
            "is_group_chat": lambda chat: True,
            "is_at_mentioned": lambda text: True,
            "_rate_limit_notice": SimpleNamespace(allow=throttle.allow),
            "send_message": lambda *args, **kwargs: sent.append(args),
            "handle_group_message": lambda *args, **kwargs: dispatched.append(args),
        })
        handler({"sender": "friend", "chat_identifier": "group", "text": "@Davos hello"})
        self.assertEqual(1, len(dispatched))
        self.assertEqual([], sent)

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
