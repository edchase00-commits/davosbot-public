import ast
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from davosbot import simple_chat


ROOT = Path(__file__).resolve().parents[1]


class _ClosingConnection:
    def __init__(self, *args, **kwargs):
        self._conn = sqlite3.connect(*args, **kwargs)

    def __enter__(self):
        self._conn.__enter__()
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._conn.__exit__(exc_type, exc, tb)
        finally:
            self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _load_group_error_helpers(is_owner_func=lambda sender: sender == "owner"):
    tree = ast.parse((ROOT / "davosbot" / "main.py").read_text(encoding="utf-8"))
    wanted_assigns = {
        "_GROUP_ERROR_INTAKE_RE",
        "_CHANGE_LOG_MAINTENANCE_RE",
        "_ROAST_REQUEST_RE",
        "_ROAST_SEARCH_RE",
        "_FOOD_ROAST_RE",
        "_LIVE_INFO_TOOL_RE",
        "_OWNER_SIDE_EFFECT_TOOL_RE",
        "_SHORT_CHAT_ONLY_RE",
    }
    wanted_funcs = {
        "_is_group_error_intake",
        "_is_roast_request",
        "_should_keep_roast_chat_only",
        "_looks_like_plain_chat",
        "_should_use_owner_tools",
        "_is_simple_group_chatter",
        "_owner_group_should_use_tools",
        "_log_group_error_intake_if_needed",
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
    namespace = {
        "BOT_DB_PATH": "",
        "_simple_chat": simple_chat,
        "json": json,
        "re": __import__("re"),
        "sqlite3": type("_SQLite", (), {"connect": _ClosingConnection}),
        "is_owner": is_owner_func,
        "redact_secret": lambda text: text.replace("secret-token", "[redacted]"),
    }
    exec(compile(module, str(ROOT / "davosbot" / "main.py"), "exec"), namespace)
    return namespace


class GroupErrorIntakeTests(unittest.TestCase):
    def test_error_intake_detects_natural_group_bug_phrasing(self):
        helpers = _load_group_error_helpers()

        self.assertTrue(helpers["_is_group_error_intake"]("can you log/contextualize the game error"))
        self.assertTrue(helpers["_is_group_error_intake"]("g error"))
        self.assertTrue(helpers["_is_group_error_intake"]("contextualize conversation for this failure"))
        self.assertFalse(helpers["_is_group_error_intake"]("game tonight?"))

    def test_owner_only_error_intake_logs_redacted_metadata(self):
        helpers = _load_group_error_helpers()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            helpers["BOT_DB_PATH"] = db_path
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
                conn.commit()
            finally:
                conn.close()

            reply = helpers["_log_group_error_intake_if_needed"](
                "owner",
                "1234567890abcdef1234567890abcdef",
                "log/contextualize game error with secret-token",
            )

            self.assertIn("Logged group error intake #1 [YELLOW]", reply)
            conn = sqlite3.connect(db_path)
            try:
                request, reason = conn.execute("SELECT request, reason FROM change_log").fetchone()
            finally:
                conn.close()
            self.assertIn("[GROUP-ERROR YELLOW]", request)
            self.assertIn("[redacted]", request)
            self.assertNotIn("secret-token", request)
            metadata = json.loads(reason)
            self.assertEqual("group_error_intake", metadata["source"])
            self.assertEqual("abcdef", metadata["chat_id_tail"])
            self.assertIn("game", metadata["tags"])

    def test_non_owner_error_intake_does_not_log(self):
        helpers = _load_group_error_helpers(is_owner_func=lambda _sender: False)

        self.assertIsNone(helpers["_log_group_error_intake_if_needed"]("friend", "chat", "g error"))

    def test_log_update_command_bypasses_group_error_intake(self):
        helpers = _load_group_error_helpers()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            helpers["BOT_DB_PATH"] = db_path
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
                conn.commit()
            finally:
                conn.close()

            reply = helpers["_log_group_error_intake_if_needed"](
                "owner",
                "1234567890abcdef1234567890abcdef",
                "update log 136 to a summary of what this issue is",
            )

            self.assertIsNone(reply)
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(0, count)

    def test_single_letter_group_chatter_skips_tools(self):
        helpers = _load_group_error_helpers()

        self.assertFalse(helpers["_owner_group_should_use_tools"]("g", skip_search=False))
        self.assertFalse(helpers["_owner_group_should_use_tools"]("repeat after me g", skip_search=False))
        self.assertFalse(helpers["_owner_group_should_use_tools"]("search this", skip_search=True))
        self.assertTrue(helpers["_owner_group_should_use_tools"]("search the web for this", skip_search=False))


if __name__ == "__main__":
    unittest.main()
