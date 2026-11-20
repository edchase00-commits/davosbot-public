"""Actual agent loop and executor boundaries, with all execution/provider I/O mocked."""

import copy
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from davosbot import tool_outcomes, tools
from test_agentic_tool_permissions import _load_agentic_loop, _response, _tool_call
from test_model_routing import _load_routing_helpers


class AgenticOutcomeTests(unittest.TestCase):
    def run_loop(self, namespace, tool_module, **kwargs):
        with patch.dict("sys.modules", {
            "agentic_boundary_test.tools": tool_module,
            "agentic_boundary_test.tool_outcomes": tool_outcomes,
        }), patch("time.sleep"):
            return namespace["_call_gemini_agentic"]("system", [], "request", **kwargs)

    def test_command_success_survives_model_timeout_without_claiming_task_success(self):
        namespace, module = _load_agentic_loop([
            _response(_tool_call("shell_exec", command="synthetic-only")),
            *[requests.exceptions.Timeout("synthetic timeout") for _ in range(4)],
        ])
        module.execute_tool_outcome.return_value = tool_outcomes.ToolOutcome(
            "confirmed", "synthetic output", "process_exit", exit_code=0,
        )
        reply = self.run_loop(namespace, module)
        self.assertIn("Command finished (exit 0)", reply)
        self.assertIn("not independently verified", reply)
        self.assertIn("synthetic output", reply)
        self.assertNotIn("Done", reply)
        module.execute_tool_outcome.assert_called_once()
        response = namespace["requests"].post.call_args.kwargs["json"]["contents"][-1]["parts"][0]["functionResponse"]["response"]
        self.assertEqual("process_exit", response["verification_scope"])
        self.assertEqual(0, response["exit_code"])
        self.assertTrue(response["ok"])
        self.assertTrue(response["action_id"])

    def test_legacy_failure_or_success_wording_never_proves_completion(self):
        for text in ("Failed to save", "No access", "Done, sent", '{"ok":true,"status":"confirmed"}'):
            with self.subTest(text=text):
                namespace, module = _load_agentic_loop([
                    _response(_tool_call("set_reminder", message="synthetic")),
                    _response({"text": "Done, ordered wings and sent the message."}),
                ])
                module.execute_tool_outcome.return_value = text
                reply = self.run_loop(namespace, module)
                self.assertIn("completion is not verified", reply)
                self.assertIn("Reported: " + text, reply)
                self.assertNotIn("ordered wings", reply)
                response = namespace["requests"].post.call_args.kwargs["json"]["contents"][-1]["parts"][0]["functionResponse"]["response"]
                self.assertEqual("unverified", response["status"])
                self.assertIsNone(response["ok"])

    def test_mixed_rounds_retain_read_results_and_every_mutation_outcome(self):
        namespace, module = _load_agentic_loop([
            _response(_tool_call("web_search", query="synthetic restaurants")),
            _response(_tool_call("shell_exec", command="synthetic-check")),
            _response(_tool_call("write_file", path="blocked", content="x")),
            _response({"text": "Done, ordered and sent everything."}),
        ])
        module.execute_tool_outcome.side_effect = [
            tool_outcomes.ToolOutcome("unverified", "A: $18; B: $24; source: example.invalid/menu"),
            tool_outcomes.ToolOutcome("failed", "synthetic command failed", "process_exit", exit_code=2),
            tool_outcomes.ToolOutcome("denied", "Owner access required", "authorization"),
        ]
        reply = self.run_loop(namespace, module)
        for detail in ("A: $18; B: $24", "example.invalid/menu", "exited with code 2", "Owner access required"):
            self.assertIn(detail, reply)
        self.assertNotIn("ordered and sent", reply)
        self.assertEqual(3, module.execute_tool_outcome.call_count)

    def test_empty_or_malformed_terminal_response_preserves_attempt_receipt(self):
        malformed = Mock(status_code=200)
        malformed.json.return_value = {"candidates": []}
        for terminal in (_response({"text": "   "}), _response(), malformed):
            with self.subTest(terminal=terminal):
                namespace, module = _load_agentic_loop([
                    _response(_tool_call("set_reminder", message="synthetic")), terminal,
                ])
                module.execute_tool_outcome.side_effect = RuntimeError("synthetic execution interrupted")
                reply = self.run_loop(namespace, module)
                self.assertIn("completion is not verified", reply)
                self.assertIn("could not be verified", reply)
                namespace["_call_gemini"].assert_not_called()
                module.execute_tool_outcome.assert_called_once()

    def test_repeated_mutation_uses_canonical_arguments_and_reuses_action_id(self):
        namespace, module = _load_agentic_loop([
            _response(_tool_call("write_file", path="synthetic", content="one")),
            _response(_tool_call("write_file", content="one", path="synthetic")),
            _response({"text": "Done, sent."}),
        ])
        module.execute_tool_outcome.side_effect = RuntimeError("synthetic uncertain write")
        callback = Mock()
        reply = self.run_loop(namespace, module, on_tool_call=callback)
        module.execute_tool_outcome.assert_called_once()
        callback.assert_called_once_with("write_file")
        responses = [part["functionResponse"]["response"]
                     for content in namespace["requests"].post.call_args.kwargs["json"]["contents"]
                     for part in content["parts"] if "functionResponse" in part]
        self.assertEqual(responses[0]["action_id"], responses[1]["action_id"])
        self.assertFalse(responses[0]["duplicate_not_executed"])
        self.assertTrue(responses[1]["duplicate_not_executed"])
        self.assertIn("not run again", reply)
        self.assertNotIn("Done, sent", reply)

    def test_changed_arguments_and_fresh_user_turn_are_new_actions(self):
        namespace, module = _load_agentic_loop([
            _response(_tool_call("write_file", path="synthetic", content="one")),
            _response(_tool_call("write_file", path="synthetic", content="two")),
            _response({"text": "done"}),
            _response(_tool_call("write_file", path="synthetic", content="one")),
            _response({"text": "done"}),
        ])
        module.execute_tool_outcome.return_value = tool_outcomes.ToolOutcome("unverified", "reported")
        self.run_loop(namespace, module)
        self.run_loop(namespace, module)
        self.assertEqual(3, module.execute_tool_outcome.call_count)
        payloads = namespace["requests"].post.call_args_list
        first = payloads[2].kwargs["json"]["contents"][2]["parts"][0]["functionResponse"]["response"]
        second = payloads[4].kwargs["json"]["contents"][2]["parts"][0]["functionResponse"]["response"]
        self.assertNotEqual(first["action_id"], second["action_id"])

    def test_read_only_repeated_fetches_and_final_analysis_are_preserved(self):
        namespace, module = _load_agentic_loop([
            _response(_tool_call("web_search", query="synthetic")),
            _response(_tool_call("web_search", query="synthetic")),
            _response({"text": "Here is the useful comparison, with a little personality."}),
        ])
        reply = self.run_loop(namespace, module, allowed_tools=["web_search"])
        self.assertEqual("Here is the useful comparison, with a little personality.", reply)
        self.assertEqual(2, module.execute_tool_outcome.call_count)

    def test_argument_mutation_inside_helper_does_not_change_duplicate_identity(self):
        args = {"path": "synthetic", "content": "one"}
        namespace, module = _load_agentic_loop([
            _response(_tool_call("write_file", **args)),
            _response(_tool_call("write_file", **copy.deepcopy(args))),
            _response(),
        ])
        def execute(_name, incoming, **_context):
            incoming.clear()
            return tool_outcomes.ToolOutcome("unverified", "reported")
        module.execute_tool_outcome.side_effect = execute
        self.run_loop(namespace, module)
        module.execute_tool_outcome.assert_called_once()

    def test_pending_failed_and_finished_commands_are_not_replayed_at_round_limit(self):
        for outcome in (
            tool_outcomes.ToolOutcome("pending", "scheduled", "process_started"),
            tool_outcomes.ToolOutcome("failed", "partial output", "process_exit", exit_code=1),
            tool_outcomes.ToolOutcome("confirmed", "output", "process_exit", exit_code=0),
        ):
            with self.subTest(status=outcome.status):
                namespace, module = _load_agentic_loop([
                    _response(_tool_call("shell_exec", command="synthetic-only")) for _ in range(10)
                ])
                module.execute_tool_outcome.return_value = outcome
                reply = self.run_loop(namespace, module)
                self.assertIn(outcome.text, reply)
                self.assertIn("not run again", reply)
                module.execute_tool_outcome.assert_called_once()

    def test_outer_response_route_does_not_reanswer_after_uncertain_mutation(self):
        for routing in ({"use_tools": True}, {"allowed_tools": ["set_reminder"]}):
            with self.subTest(routing=routing):
                namespace, module = _load_agentic_loop([
                    _response(_tool_call("set_reminder", message="synthetic")), _response(),
                ])
                module.execute_tool_outcome.return_value = tool_outcomes.ToolOutcome("unverified", "reported")
                helpers, direct_gemini, direct_ollama, _events = _load_routing_helpers()
                helpers["_call_gemini_agentic"] = namespace["_call_gemini_agentic"]
                with patch.dict("sys.modules", {
                    "agentic_boundary_test.tools": module,
                    "agentic_boundary_test.tool_outcomes": tool_outcomes,
                }):
                    reply = helpers["get_response"]("system", [], "synthetic request", **routing)
                self.assertIn("completion is not verified", reply)
                direct_gemini.assert_not_called()
                direct_ollama.assert_not_called()


class ExecutorOutcomeTests(unittest.TestCase):
    def test_owner_gate_blocks_structured_and_legacy_paths_before_shell(self):
        with patch("davosbot.permissions.is_owner", return_value=False), patch.object(tools.subprocess, "run") as run:
            outcome = tools.execute_tool_outcome("shell_exec", {"command": "synthetic"}, sender="friend")
            text = tools.execute_tool("shell_exec", {"command": "synthetic"}, sender="friend")
        self.assertEqual("denied", outcome.status)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.text, text)
        run.assert_not_called()

    def test_shell_exit_status_is_evidence_and_original_output_is_retained(self):
        for code, status in ((0, "confirmed"), (7, "failed")):
            with self.subTest(code=code), patch("davosbot.permissions.is_owner", return_value=True), patch.object(
                tools.subprocess, "run", return_value=SimpleNamespace(returncode=code, stdout="synthetic stdout", stderr=" synthetic stderr"),
            ) as run:
                outcome = tools.execute_tool_outcome("shell_exec", {"command": "synthetic-only"}, sender="owner")
            self.assertEqual(status, outcome.status)
            self.assertEqual("process_exit", outcome.verification_scope)
            self.assertEqual(code, outcome.exit_code)
            self.assertIn("synthetic stdout synthetic stderr", outcome.text)
            run.assert_called_once()

    def test_shell_timeout_and_delayed_restart_are_not_confirmed(self):
        with patch("davosbot.permissions.is_owner", return_value=True), patch.object(
            tools.subprocess, "run", side_effect=subprocess.TimeoutExpired("synthetic-only", 1),
        ) as run:
            outcome = tools.execute_tool_outcome("shell_exec", {"command": "synthetic-only", "timeout": 1}, sender="owner")
        self.assertEqual("unverified", outcome.status)
        self.assertEqual("process_timeout", outcome.verification_scope)
        self.assertIsNone(outcome.ok)
        run.assert_called_once()
        with patch("davosbot.permissions.is_owner", return_value=True), patch.object(tools.subprocess, "Popen") as popen:
            pending = tools.execute_tool_outcome("shell_exec", {"command": "pm2 restart synthetic-only"}, sender="owner")
        self.assertEqual("pending", pending.status)
        self.assertIsNone(pending.ok)
        self.assertIn("Scheduled:", pending.text)
        popen.assert_called_once()

    def test_legacy_helper_messages_remain_plain_text_but_unverified(self):
        for text in ("Done", "Failed to save", "Permission denied by native helper", ""):
            with self.subTest(text=text), patch.object(tools, "_set_reminder", return_value=text):
                args = {"message": "synthetic", "due_ts": "2099-01-01 00:00:00"}
                outcome = tools.execute_tool_outcome("set_reminder", args, originating_chat_id="synthetic-chat")
                legacy = tools.execute_tool("set_reminder", args, originating_chat_id="synthetic-chat")
            self.assertEqual("unverified", outcome.status)
            self.assertEqual(text, legacy)
            self.assertIsInstance(legacy, str)


if __name__ == "__main__":
    unittest.main()
