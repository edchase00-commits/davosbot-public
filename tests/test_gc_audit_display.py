import ast
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_chat_audit_summary():
    tree = ast.parse((ROOT / "davosbot" / "commands.py").read_text(encoding="utf-8"))
    wanted = {"_chat_audit_summary", "_stale_chat_audit_rows", "_format_stale_chat_rows"}
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "sqlite3": sqlite3,
        "DB_PATH": "",
        "MAC_MINI_APPLE_ID": "bot@example.com",
    }
    exec(compile(module, str(ROOT / "davosbot" / "commands.py"), "exec"), namespace)
    return namespace


class GroupChatAuditDisplayTests(unittest.TestCase):
    def _make_chat_db(self, db_path: str) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE chat (
                    ROWID INTEGER PRIMARY KEY,
                    chat_identifier TEXT,
                    guid TEXT,
                    account_id TEXT,
                    account_login TEXT,
                    last_addressed_handle TEXT,
                    room_name TEXT,
                    display_name TEXT
                )
                """
            )
            conn.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)")
            conn.execute("CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER)")
            conn.execute(
                """
                INSERT INTO chat (
                    ROWID, chat_identifier, guid, account_id, account_login,
                    last_addressed_handle, room_name, display_name
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    1,
                    "okchat",
                    "iMessage;+;uuid;chatok",
                    "uuid-like",
                    "bot@example.com",
                    "bot@example.com",
                    "",
                    "Cole GC",
                ),
            )
            conn.execute(
                """
                INSERT INTO chat (
                    ROWID, chat_identifier, guid, account_id, account_login,
                    last_addressed_handle, room_name, display_name
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    2,
                    "stalechat",
                    "iMessage;+;uuid;chatstale",
                    "old-uuid",
                    "old@example.com",
                    "old@example.com",
                    "Old Thread",
                    "",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_chat_audit_summary_marks_ok_and_stale(self):
        helpers = _load_chat_audit_summary()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "chat.db")
            self._make_chat_db(db_path)
            helpers["DB_PATH"] = db_path

            ok = helpers["_chat_audit_summary"]("okchat")
            stale = helpers["_chat_audit_summary"]("stalechat")

        self.assertEqual("OK", ok["status"])
        self.assertFalse(ok["stale"])
        self.assertEqual("Cole GC", ok["label"])
        self.assertEqual("STALE", stale["status"])
        self.assertTrue(stale["stale"])
        self.assertEqual("Old Thread", stale["label"])

    def test_chat_audit_summary_marks_missing_enabled_chat(self):
        helpers = _load_chat_audit_summary()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "chat.db")
            self._make_chat_db(db_path)
            helpers["DB_PATH"] = db_path

            missing = helpers["_chat_audit_summary"]("missingchat")

        self.assertEqual("MISSING", missing["status"])
        self.assertTrue(missing["stale"])

    def test_stale_chat_rows_filter_to_stale_and_missing(self):
        helpers = _load_chat_audit_summary()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "chat.db")
            self._make_chat_db(db_path)
            helpers["DB_PATH"] = db_path

            rows = helpers["_stale_chat_audit_rows"](["okchat", "stalechat", "missingchat"])

        self.assertEqual(["stalechat", "missingchat"], [row["chat_id"] for row in rows])

    def test_stale_chat_rows_preview_mentions_confirmation_command(self):
        helpers = _load_chat_audit_summary()
        text = helpers["_format_stale_chat_rows"]([
            {
                "chat_id": "a" * 32,
                "label": "Old Thread",
                "status": "STALE",
                "detail": "recreate this group from the Mac Mini Apple ID",
            }
        ])

        self.assertIn("Stale group-chat routing warnings", text)
        self.assertIn("Old Thread", text)
        self.assertIn("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", text)

    def test_startup_gc_audit_logs_do_not_expose_raw_handles(self):
        from davosbot import config
        from davosbot import group_chat

        chat_id = "83ebe9a629cb4d509946f84fcae65247"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "chat.db"
            state_path = root / "gc_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "enabled_chats": [chat_id],
                        "approved_users": [],
                        "personas": {chat_id: "default"},
                        "group_personas": {},
                    }
                ),
                encoding="utf-8",
            )
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE chat (
                        ROWID INTEGER PRIMARY KEY,
                        chat_identifier TEXT,
                        guid TEXT,
                        account_id TEXT,
                        account_login TEXT,
                        last_addressed_handle TEXT,
                        room_name TEXT,
                        display_name TEXT
                    )
                    """
                )
                conn.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)")
                conn.execute("CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER)")
                conn.execute(
                    """
                    INSERT INTO chat (
                        ROWID, chat_identifier, guid, account_id, account_login,
                        last_addressed_handle, room_name, display_name
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        1,
                        chat_id,
                        "iMessage;+;uuid;chat-guid-123456",
                        "private-account-guid",
                        "bot@example.com",
                        "bot@example.com",
                        "Cole GC",
                        "",
                    ),
                )
                conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, '+13369700454')")
                conn.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (1, 1)")
                conn.commit()
            finally:
                conn.close()

            old_state_file = group_chat._STATE_FILE
            old_state = dict(group_chat._state)
            old_db_path = config.DB_PATH
            old_apple_id = config.MAC_MINI_APPLE_ID
            try:
                group_chat._STATE_FILE = state_path
                group_chat._state = group_chat._fresh_state()
                config.DB_PATH = db_path
                config.MAC_MINI_APPLE_ID = "bot@example.com"

                with self.assertLogs("davosbot.group_chat", level="INFO") as captured:
                    group_chat.audit_group_chats()
            finally:
                group_chat._STATE_FILE = old_state_file
                group_chat._state = old_state
                config.DB_PATH = old_db_path
                config.MAC_MINI_APPLE_ID = old_apple_id

        logs = "\n".join(captured.output)
        self.assertIn("participants=1", logs)
        self.assertIn("chat:...e65247", logs)
        self.assertNotIn(chat_id, logs)
        self.assertNotIn("+13369700454", logs)
        self.assertNotIn("bot@example.com", logs)
        self.assertNotIn("private-account-guid", logs)


if __name__ == "__main__":
    unittest.main()
