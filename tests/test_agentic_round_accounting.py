"""Real agent loop plus reopened SQLite accounting; providers and tools are mocked."""

import ast
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from davosbot import billing, tool_outcomes
from davosbot.db import connect_bot_db
from test_agentic_tool_permissions import ROOT, _load_agentic_loop, _response, _tool_call


def _with_usage(response, usage):
    response.json.return_value["usageMetadata"] = usage
    return response


def _usage(prompt=10, candidates=2, total=12):
    return {"promptTokenCount": prompt, "candidatesTokenCount": candidates, "totalTokenCount": total}


class AgenticRoundAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agentic-accounting-")
        self.addCleanup(self.temporary.cleanup)
        self.db_path = str(Path(self.temporary.name) / "usage.sqlite")
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("""CREATE TABLE gemini_usage (
                id INTEGER PRIMARY KEY, timestamp TEXT DEFAULT (datetime('now')),
                prompt_tokens INTEGER NOT NULL, candidates_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL, source TEXT NOT NULL
            )""")

    def load(self, responses):
        namespace, module = _load_agentic_loop(responses)
        tree = ast.parse((ROOT / "davosbot" / "brain.py").read_text(encoding="utf-8"))
        helper = ast.Module(body=[node for node in tree.body
                                 if isinstance(node, ast.FunctionDef) and node.name == "_log_gemini_usage"],
                            type_ignores=[])
        namespace.update(BOT_DB_PATH=self.db_path, connect_bot_db=connect_bot_db)
        exec(compile(helper, str(ROOT / "davosbot" / "brain.py"), "exec"), namespace)
        return namespace, module

    def run_loop(self, namespace, module):
        with patch.dict("sys.modules", {
            "agentic_boundary_test.tools": module,
            "agentic_boundary_test.tool_outcomes": tool_outcomes,
        }), patch("time.sleep"):
            return namespace["_call_gemini_agentic"]("system", [], "synthetic", sender="owner")

    def rows(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute("SELECT prompt_tokens, candidates_tokens, total_tokens, source FROM gemini_usage ORDER BY id").fetchall()

    def test_every_tool_and_terminal_round_persists_once_even_when_tool_is_deduplicated(self):
        namespace, module = self.load([
            _with_usage(_response(_tool_call("write_file", path="synthetic", content="one")), _usage(10, 2, 12)),
            _with_usage(_response(_tool_call("write_file", path="synthetic", content="one")), _usage(20, 3, 23)),
            _with_usage(_response({"text": "Done"}), _usage(30, 4, 34)),
        ])
        self.run_loop(namespace, module)
        self.assertEqual([(10, 2, 12, "agentic"), (20, 3, 23, "agentic"), (30, 4, 34, "agentic")], self.rows())
        module.execute_tool_outcome.assert_called_once()
        with patch.object(billing, "BOT_DB_PATH", self.db_path):
            summary = billing.get_gemini_usage_summary("all")
        self.assertEqual((60, 9, 69, 3), (summary.prompt_tokens, summary.candidates_tokens, summary.total_tokens, summary.calls))

    def test_empty_and_malformed_terminal_still_record_reported_usage(self):
        for content in ({"candidates": []}, {"candidates": [{}]}, {"candidates": [{"content": {"parts": []}}]},
                        {"candidates": [{"content": {"parts": [{"text": " "}]}}]}):
            with self.subTest(content=content):
                before = len(self.rows())
                response = Mock(status_code=200)
                response.json.return_value = {**content, "usageMetadata": _usage()}
                namespace, module = self.load([response])
                self.run_loop(namespace, module)
                self.assertEqual(before + 1, len(self.rows()))
                self.assertEqual((10, 2, 12, "agentic"), self.rows()[-1])
                module.execute_tool_outcome.assert_not_called()

    def test_model_timeout_after_tool_does_not_lose_or_duplicate_prior_usage(self):
        namespace, module = self.load([
            _with_usage(_response(_tool_call("write_file", path="synthetic", content="one")), _usage()),
            *[requests.exceptions.Timeout("synthetic") for _ in range(4)],
        ])
        reply = self.run_loop(namespace, module)
        self.assertIn("completion is not verified", reply)
        self.assertEqual([(10, 2, 12, "agentic")], self.rows())
        module.execute_tool_outcome.assert_called_once()

    def test_transient_retry_only_records_received_successful_response(self):
        transient = Mock(status_code=503)
        namespace, module = self.load([transient, _with_usage(_response({"text": "answer"}), _usage())])
        self.assertEqual("answer", self.run_loop(namespace, module))
        self.assertEqual([(10, 2, 12, "agentic")], self.rows())
        transient.json.assert_not_called()

    def test_missing_or_invalid_usage_does_not_create_fake_zero_tokens_or_log_values(self):
        for usage in (None, [], {}, "private-canary", {
            "promptTokenCount": "private-canary", "candidatesTokenCount": False,
            "totalTokenCount": -5, "private-canary": "private-canary",
        }):
            with self.subTest(usage=usage):
                namespace, module = self.load([_with_usage(_response({"text": "answer"}), usage)])
                self.assertEqual("answer", self.run_loop(namespace, module))
                self.assertEqual([], self.rows())
                warning = str(namespace["logger"].warning.call_args_list)
                self.assertIn("unreported", warning)
                self.assertNotIn("private-canary", warning)
        namespace, module = self.load([_response({"text": "answer"})])
        self.run_loop(namespace, module)
        self.assertEqual([], self.rows())

    def test_partial_counters_preserve_known_values_as_marked_lower_bounds(self):
        namespace, module = self.load([_with_usage(_response({"text": "answer"}), {
            "promptTokenCount": 50, "candidatesTokenCount": "private-canary",
        })])
        self.assertEqual("answer", self.run_loop(namespace, module))
        self.assertEqual([(50, 0, 0, "agentic_partial")], self.rows())
        self.assertNotIn("private-canary", str(namespace["logger"].warning.call_args_list))
        namespace, module = self.load([_with_usage(_response({"text": "answer"}), _usage(0, 0, 0))])
        self.run_loop(namespace, module)
        self.assertEqual((0, 0, 0, "agentic"), self.rows()[-1])

    def test_actual_budget_observes_persisted_first_round_before_second_request(self):
        namespace, module = self.load([
            _with_usage(_response(_tool_call("write_file", path="synthetic", content="one")), _usage(1_000_000, 0, 1_000_000)),
            _with_usage(_response({"text": "must not be requested"}), _usage()),
        ])
        namespace["check_gemini_budget"] = billing.check_gemini_budget
        with patch.object(billing, "BOT_DB_PATH", self.db_path), patch.object(billing, "GEMINI_ENABLED", True), \
             patch.object(billing, "GEMINI_DAILY_ALERT_USD", 0), patch.object(billing, "GEMINI_DAILY_BUDGET_USD", 0.2), \
             patch.object(billing, "_maybe_send_gemini_budget_alert"):
            reply = self.run_loop(namespace, module)
        self.assertIn("completion is not verified", reply)
        namespace["requests"].post.assert_called_once()
        module.execute_tool_outcome.assert_called_once()
        self.assertEqual([(1_000_000, 0, 1_000_000, "agentic")], self.rows())

    def test_budget_is_rechecked_before_transient_retry(self):
        namespace, module = self.load([Mock(status_code=503), _response({"text": "must not be requested"})])
        namespace["check_gemini_budget"] = Mock(side_effect=[
            SimpleNamespace(allowed=True), SimpleNamespace(allowed=False, reason="synthetic budget block"),
        ])
        self.assertIsNone(self.run_loop(namespace, module))
        namespace["requests"].post.assert_called_once()
        module.execute_tool_outcome.assert_not_called()
        self.assertEqual([], self.rows())

    def test_budget_block_during_later_retry_retains_action_receipt(self):
        namespace, module = self.load([
            _with_usage(_response(_tool_call("write_file", path="synthetic", content="one")), _usage()),
            Mock(status_code=503), _response({"text": "must not be requested"}),
        ])
        namespace["check_gemini_budget"] = Mock(side_effect=[
            SimpleNamespace(allowed=True), SimpleNamespace(allowed=True),
            SimpleNamespace(allowed=False, reason="synthetic budget block"),
        ])
        reply = self.run_loop(namespace, module)
        self.assertIn("completion is not verified", reply)
        self.assertEqual(2, namespace["requests"].post.call_count)
        module.execute_tool_outcome.assert_called_once()
        self.assertEqual([(10, 2, 12, "agentic")], self.rows())


if __name__ == "__main__":
    unittest.main()
