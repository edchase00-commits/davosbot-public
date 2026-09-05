import ast
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import requests
from davosbot import tool_outcomes


ROOT = Path(__file__).resolve().parents[1]


def _response(*parts):
    response = Mock(status_code=200)
    response.json.return_value = {
        "candidates": [{"content": {"role": "model", "parts": list(parts)}}],
    }
    return response


def _tool_call(name, **args):
    return {"functionCall": {"name": name, "args": args}}


def _load_agentic_loop(responses):
    """Exercise the real loop with no provider, config, or runtime DB access."""
    tree = ast.parse((ROOT / "davosbot" / "brain.py").read_text(encoding="utf-8"))
    names = {
        "_call_gemini_agentic", "_safe_tool_result_for_log", "_record_agentic_usage",
    }
    module = ast.Module(
        body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names],
        type_ignores=[],
    )
    namespace = {
        "__package__": "agentic_boundary_test",
        "check_gemini_budget": lambda _source: SimpleNamespace(allowed=True),
        "_call_gemini": Mock(return_value="Direct reply"),
        "_fit_history_for_model": lambda history: history,
        "requests": SimpleNamespace(post=Mock(side_effect=responses), exceptions=requests.exceptions),
        "GEMINI_URL": "https://example.invalid/generate",
        "GEMINI_API_KEY": "test-placeholder",
        "_close_response": lambda response: response.close(),
        "_log_gemini_usage": Mock(),
        "logger": Mock(),
        "redact_secret": str,
        "_is_large_prompt_error": lambda _status, _body: False,
    }
    exec(compile(module, str(ROOT / "davosbot" / "brain.py"), "exec"), namespace)
    tool_module = ModuleType("agentic_boundary_test.tools")
    tool_module.TOOL_DEFINITIONS = [
        {"name": name, "parameters": {"type": "object", "properties": {}}}
        for name in ("web_search", "set_reminder", "write_file", "shell_exec")
    ]
    tool_module.execute_tool_outcome = Mock(return_value=tool_outcomes.ToolOutcome("unverified", "Search result"))
    return namespace, tool_module


class AgenticToolBoundaryTests(unittest.TestCase):
    def run_loop(self, namespace, tool_module, **kwargs):
        with patch.dict("sys.modules", {
            "agentic_boundary_test.tools": tool_module,
            "agentic_boundary_test.tool_outcomes": tool_outcomes,
        }):
            return namespace["_call_gemini_agentic"]("system", [], "request", **kwargs)

    def test_restricted_loop_rejects_unadvertised_mutation_and_can_continue_search(self):
        namespace, tool_module = _load_agentic_loop([
            _response(_tool_call("set_reminder", message="unauthorized", due_ts="2030-01-01 00:00:00")),
            _response(_tool_call("web_search", query="wing restaurants")),
            _response({"text": "Found restaurants"}),
        ])
        callback = Mock()

        reply = self.run_loop(
            namespace, tool_module, allowed_tools=["web_search"],
            sender="friend", originating_chat_id="group", on_tool_call=callback,
        )

        self.assertIn("not allowed", reply)
        self.assertIn("Search result", reply)
        tool_module.execute_tool_outcome.assert_called_once_with(
            "web_search", {"query": "wing restaurants"}, sender="friend", originating_chat_id="group",
        )
        callback.assert_called_once_with("web_search")
        payload = namespace["requests"].post.call_args.kwargs["json"]
        denial = payload["contents"][2]["parts"][0]["functionResponse"]
        self.assertEqual("set_reminder", denial["name"])
        self.assertTrue(denial["response"]["result"].startswith("Permission denied"))

    def test_allowlist_entry_missing_from_definitions_is_not_authorized(self):
        namespace, tool_module = _load_agentic_loop([
            _response(_tool_call("invented_tool")), _response({"text": "Unavailable"}),
        ])

        self.assertIn("not allowed", self.run_loop(
            namespace, tool_module, allowed_tools=["web_search", "invented_tool"],
        ))
        tool_module.execute_tool_outcome.assert_not_called()

    def test_default_full_inventory_rejects_unknown_tool(self):
        namespace, tool_module = _load_agentic_loop([
            _response(_tool_call("invented_tool")), _response({"text": "Unavailable"}),
        ])

        self.assertIn("not allowed", self.run_loop(namespace, tool_module))
        tool_module.execute_tool_outcome.assert_not_called()

    def test_rejection_receipt_survives_empty_final_response(self):
        namespace, tool_module = _load_agentic_loop([
            _response(_tool_call("set_reminder")), _response(),
        ])

        reply = self.run_loop(namespace, tool_module, allowed_tools=["web_search"])
        self.assertIn("not allowed", reply)
        self.assertNotIn("Done", reply)
        tool_module.execute_tool_outcome.assert_not_called()

    def test_full_inventory_passes_sender_to_existing_executor_permission_gate(self):
        namespace, tool_module = _load_agentic_loop([
            _response(_tool_call("write_file", path="blocked.py", content="pass")),
            _response({"text": "Owner access required"}),
        ])
        tool_module.execute_tool_outcome.return_value = tool_outcomes.ToolOutcome(
            "denied", "Permission denied — write_file is restricted to the owner.", "authorization",
        )

        self.assertIn("Permission denied", self.run_loop(namespace, tool_module, sender="friend"))
        tool_module.execute_tool_outcome.assert_called_once_with(
            "write_file", {"path": "blocked.py", "content": "pass"},
            sender="friend", originating_chat_id="",
        )

    def test_empty_or_unknown_only_allowlist_uses_direct_model_without_tools(self):
        for allowlist in ([], ["invented_tool"]):
            with self.subTest(allowed_tools=allowlist):
                namespace, tool_module = _load_agentic_loop([])

                self.assertEqual("Direct reply", self.run_loop(namespace, tool_module, allowed_tools=allowlist))
                namespace["_call_gemini"].assert_called_once_with("system", [], "request", image_path=None)
                namespace["requests"].post.assert_not_called()
                tool_module.execute_tool_outcome.assert_not_called()


if __name__ == "__main__":
    unittest.main()
