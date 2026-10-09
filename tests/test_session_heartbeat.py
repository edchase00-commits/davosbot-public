import ast
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def _load_brain_heartbeat_helpers(db_path: str):
    tree = ast.parse((ROOT / "davosbot" / "brain.py").read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "_SESSION_ID":
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in {"touch_session_heartbeat", "update_heartbeat"}:
            nodes.append(node)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "BOT_DB_PATH": db_path,
        "closing": closing,
        "logger": SimpleNamespace(warning=lambda *args, **kwargs: None),
        "sqlite3": sqlite3,
    }
    exec(compile(module, str(ROOT / "davosbot" / "brain.py"), "exec"), namespace)
    return namespace


def _load_main_heartbeat_helpers():
    tree = ast.parse((ROOT / "davosbot" / "main.py").read_text(encoding="utf-8"))
    nodes = []
    wanted_assigns = {"_LAST_SESSION_HEARTBEAT", "_SESSION_HEARTBEAT_INTERVAL"}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id in wanted_assigns for target in node.targets):
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "_check_session_heartbeat":
            nodes.append(node)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    calls = []
    namespace = {
        "time": SimpleNamespace(time=lambda: 0),
        "touch_session_heartbeat": lambda: calls.append("touch"),
    }
    exec(compile(module, str(ROOT / "davosbot" / "main.py"), "exec"), namespace)
    return namespace, calls


class SessionHeartbeatTests(unittest.TestCase):
    def test_touch_session_heartbeat_does_not_increment_messages(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE bot_sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        started_at TEXT NOT NULL DEFAULT (datetime('now')),
                        last_heartbeat TEXT,
                        messages_processed INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        last_error_at TEXT
                    )
                    """
                )
                conn.execute("INSERT INTO bot_sessions (messages_processed) VALUES (0)")
                conn.commit()

            helpers = _load_brain_heartbeat_helpers(db_path)
            helpers["_SESSION_ID"] = 1

            helpers["touch_session_heartbeat"]()

            with closing(sqlite3.connect(db_path)) as conn:
                row = conn.execute(
                    "SELECT last_heartbeat, messages_processed FROM bot_sessions WHERE id = 1"
                ).fetchone()
            self.assertIsNotNone(row[0])
            self.assertEqual(0, row[1])

    def test_message_heartbeat_still_increments_messages(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE bot_sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        started_at TEXT NOT NULL DEFAULT (datetime('now')),
                        last_heartbeat TEXT,
                        messages_processed INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        last_error_at TEXT
                    )
                    """
                )
                conn.execute("INSERT INTO bot_sessions (messages_processed) VALUES (0)")
                conn.commit()

            helpers = _load_brain_heartbeat_helpers(db_path)
            helpers["_SESSION_ID"] = 1

            helpers["update_heartbeat"]()

            with closing(sqlite3.connect(db_path)) as conn:
                row = conn.execute(
                    "SELECT last_heartbeat, messages_processed FROM bot_sessions WHERE id = 1"
                ).fetchone()
            self.assertIsNotNone(row[0])
            self.assertEqual(1, row[1])

    def test_main_loop_heartbeat_is_throttled(self):
        helpers, calls = _load_main_heartbeat_helpers()

        helpers["_check_session_heartbeat"](now=59)
        helpers["_check_session_heartbeat"](now=60)
        helpers["_check_session_heartbeat"](now=119)
        helpers["_check_session_heartbeat"](now=120)

        self.assertEqual(["touch", "touch"], calls)


if __name__ == "__main__":
    unittest.main()
