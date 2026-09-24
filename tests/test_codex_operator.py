import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


codex_operator = _load_script("codex_operator", ROOT / "scripts" / "codex_operator.py")
mcp_server = _load_script("davosbot_mcp_server", ROOT / "scripts" / "davosbot_mcp_server.py")


class CodexOperatorTests(unittest.TestCase):
    def test_tool_specs_expose_safe_codex_surface(self):
        names = {tool["name"] for tool in codex_operator.tool_specs_for_mcp()}

        self.assertIn("sync_status", names)
        self.assertIn("queue_status", names)
        self.assertIn("change_log", names)
        self.assertIn("quick_smoke", names)
        self.assertIn("repo_guard", names)
        self.assertIn("public_sync_dry_run", names)
        self.assertIn("maintenance_report", names)
        self.assertIn("quality_sweep", names)

    def test_operator_suppresses_local_config_warning_noise(self):
        self.assertEqual("1", os.environ.get("DAVOSBOT_SUPPRESS_CONFIG_WARNINGS"))

    def test_change_log_tool_redacts_secret_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "davosbot.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE change_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request TEXT NOT NULL,
                        reason TEXT,
                        created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO change_log (request, reason) VALUES (?, ?)",
                    ("fix api_key=sk-supersecret1234567890abcdef leak", "token=ghp_supersecret1234567890abcdef"),
                )
                conn.commit()
            finally:
                conn.close()

            result = codex_operator.run_tool("change_log", {"db": str(db_path)})

        self.assertTrue(result.ok)
        self.assertIn("api_key=[redacted]", result.text)
        self.assertIn("token=[redacted]", result.text)
        self.assertNotIn("supersecret", result.text)

    def test_repo_guard_fails_when_worktree_is_dirty(self):
        guard = codex_operator.OperatorResult(True, "clean")
        status = codex_operator.OperatorResult(True, " M scripts/codex_operator.py\n")
        with patch.object(codex_operator, "_run", side_effect=[guard, status]):
            result = codex_operator.run_tool("repo_guard")

        self.assertFalse(result.ok)
        self.assertIn("dirty (1 file(s))", result.text)
        self.assertEqual(1, result.data["dirty_count"])

    def test_repo_guard_treats_empty_git_status_as_clean(self):
        guard = codex_operator.OperatorResult(True, "")
        status = codex_operator.OperatorResult(True, "")
        with patch.object(codex_operator, "_run", side_effect=[guard, status]):
            result = codex_operator.run_tool("repo_guard")

        self.assertTrue(result.ok)
        self.assertIn("worktree: clean", result.text)
        self.assertEqual(0, result.data["dirty_count"])

    def test_repo_guard_does_not_call_unavailable_status_clean(self):
        with patch.object(codex_operator, "_run", side_effect=[
            codex_operator.OperatorResult(True, ""), codex_operator.OperatorResult(False, "git failed")
        ]):
            result = codex_operator.run_tool("repo_guard")
        self.assertFalse(result.ok)
        self.assertIn("worktree: unavailable", result.text)
        self.assertIsNone(result.data["dirty_count"])

    def test_maintenance_failure_reaches_operator_and_mcp(self):
        from scripts import maintenance_diagnostics
        for smoke_ok, errors, inbox_ok in ((False, 1, True), (True, 0, False)):
            failed = maintenance_diagnostics.MaintenanceResult(ROOT / "report.md", smoke_ok, errors, inbox_ok)
            with self.subTest(inbox_ok=inbox_ok), patch.object(maintenance_diagnostics, "collect_diagnostics", return_value=failed):
                result = codex_operator.run_tool("maintenance_report")
                response = mcp_server.handle_request({
                    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "maintenance_report", "arguments": {}},
                })
            self.assertFalse(result.ok)
            self.assertEqual(errors, result.data["recent_error_count"])
            self.assertEqual(inbox_ok, result.data["inbox_ok"])
            self.assertTrue(response["result"]["isError"])

    def test_quality_sweep_tool_uses_resolved_project_python(self):
        with patch.object(codex_operator, "_project_python", return_value="/tmp/davos-python"), patch.object(
            codex_operator,
            "_run",
            return_value=codex_operator.OperatorResult(True, "ok"),
        ) as run_mock:
            result = codex_operator.run_tool("quality_sweep", {"mode": "full"})

        self.assertTrue(result.ok)
        self.assertEqual(["/tmp/davos-python", "scripts/quality_sweep.py", "--mode", "full"], run_mock.call_args[0][0])

    def test_mcp_tools_list_and_call(self):
        list_response = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertIn("tools", list_response["result"])

        with patch.object(mcp_server.codex_operator, "run_tool", return_value=codex_operator.OperatorResult(True, "ok")):
            call_response = mcp_server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "quick_smoke", "arguments": {}},
                }
            )

        self.assertFalse(call_response["result"]["isError"])
        self.assertEqual("ok", call_response["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
