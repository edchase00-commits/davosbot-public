"""Private canaries never enter dispatcher logs; tool behavior stays intact."""

import unittest
from unittest.mock import patch

from davosbot import tools, tool_outcomes
from test_agentic_tool_permissions import _load_agentic_loop, _response, _tool_call


CANARY = "synthetic-private-canary"


class ToolLogMetadataTests(unittest.TestCase):
    def test_arbitrary_nested_values_and_unknown_keys_are_not_log_metadata(self):
        for name in ("shell_exec", "read_file", "sqlite_query", "log_workout", "write_file", CANARY):
            with self.subTest(name=name):
                args = {"command": CANARY, "query": CANARY, "content": {CANARY: [CANARY]}, CANARY: CANARY}
                metadata = tools._safe_tool_args_for_log(name, args)
                self.assertEqual(4, metadata["argument_count"])
                self.assertNotIn(CANARY, str(metadata))
                self.assertEqual(CANARY, args["command"])

    def test_private_send_drops_unknown_fields_but_preserves_existing_safe_receipt_metadata(self):
        args = {"recipient": "+15550000001", "message": CANARY, CANARY: CANARY,
                "scheduled_time_utc": CANARY}
        metadata = tools._safe_tool_args_for_log("send_imessage", args)
        self.assertNotIn(CANARY, str(metadata))
        self.assertNotIn("+15550000001", str(metadata))
        self.assertEqual(len(CANARY), metadata["message_len"])
        self.assertEqual("***-***-0001", metadata["recipient_masked"])
        self.assertTrue(metadata["message_hash"])

    def test_dispatch_logs_no_argument_result_or_exception_content_but_returns_original_data(self):
        with patch("davosbot.permissions.is_owner", return_value=True), patch.object(
            tools, "_read_file", return_value=CANARY,
        ) as read, self.assertLogs("davosbot.tools", level="INFO") as logs:
            result = tools.execute_tool("read_file", {"path": CANARY}, sender=CANARY)
        self.assertEqual(CANARY, result)
        read.assert_called_once_with(CANARY)
        self.assertNotIn(CANARY, "\n".join(logs.output))
        with patch("davosbot.permissions.is_owner", return_value=True), patch.object(
            tools, "_read_file", side_effect=RuntimeError(CANARY),
        ), self.assertLogs("davosbot.tools", level="INFO") as logs:
            outcome = tools.execute_tool_outcome("read_file", {"path": CANARY})
        self.assertIn(CANARY, outcome.text)
        self.assertEqual("unverified", outcome.status)
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNotIn(CANARY, "\n".join(logs.output))

    def test_denial_and_unknown_tool_names_do_not_echo_user_controlled_identity(self):
        with patch("davosbot.permissions.is_owner", return_value=False), patch.object(tools, "_read_file") as read, \
             self.assertLogs("davosbot.tools", level="INFO") as logs:
            denied = tools.execute_tool_outcome("read_file", {"path": CANARY}, sender=CANARY)
            unknown = tools.execute_tool_outcome(CANARY, {CANARY: CANARY})
        self.assertEqual("denied", denied.status)
        self.assertEqual("failed", unknown.status)
        read.assert_not_called()
        self.assertNotIn(CANARY, "\n".join(logs.output))

    def test_actual_agent_loop_keeps_model_result_and_receipt_but_only_logs_metadata(self):
        namespace, module = _load_agentic_loop([
            _response(_tool_call("write_file", path=CANARY, content=CANARY)),
            _response(_tool_call(CANARY, **{CANARY: CANARY})),
            _response({"text": "Done"}),
        ])
        module.execute_tool_outcome.return_value = tool_outcomes.ToolOutcome("unverified", CANARY)
        with patch.dict("sys.modules", {"agentic_boundary_test.tools": module,
                                       "agentic_boundary_test.tool_outcomes": tool_outcomes}):
            reply = namespace["_call_gemini_agentic"]("system", [], "synthetic", sender="owner")
        self.assertIn(CANARY, reply)
        self.assertEqual(1, module.execute_tool_outcome.call_count)
        for calls in (namespace["logger"].info.call_args_list, namespace["logger"].warning.call_args_list):
            self.assertNotIn(CANARY, str(calls))
        payload = namespace["requests"].post.call_args.kwargs["json"]
        self.assertIn(CANARY, str(payload))


if __name__ == "__main__":
    unittest.main()
