import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from davosbot import commands
from davosbot import memory
def _init_user_facts(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE user_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                source TEXT DEFAULT 'self',
                timestamp TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


class LongTermMemoryTests(unittest.TestCase):
    def test_owner_memory_items_are_searchable_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "bot.db")
            _init_user_facts(db_path)

            item_id = memory.add_owner_memory_item(
                "Remember this API key sk-abcdefghijklmnopqrstuvwxyz1234 for the project",
                db_path=db_path,
            )
            rows = memory.search_owner_memory_items("project", db_path=db_path)

        self.assertEqual(1, item_id)
        self.assertEqual(1, len(rows))
        self.assertIn("project", rows[0]["text"])
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz1234", rows[0]["text"])
        self.assertIn("[redacted-openai-key]", rows[0]["text"])

    def test_memory_note_command_stores_private_note_without_touching_memory_md(self):
        with (
            patch.object(commands, "check_action_permission", return_value=None),
            patch.object(commands, "add_owner_memory_item", return_value=42) as add_note,
        ):
            reply = commands._cmd_memory("memory note The sportsbook dashboard needs PayPal import", sender="owner")

        self.assertIn("Saved private memory note #42", reply)
        self.assertIn("not injected into group chats", reply)
        add_note.assert_called_once_with(
            "The sportsbook dashboard needs PayPal import",
            source="owner_manual",
        )

    def test_memory_search_command_formats_matches(self):
        rows = [{"id": 7, "timestamp": "2026-05-20 01:02:03", "text": "PayPal CSV first"}]
        with (
            patch.object(commands, "check_action_permission", return_value=None),
            patch.object(commands, "search_owner_memory_items", return_value=rows) as search,
        ):
            reply = commands._cmd_memory("memory search PayPal", sender="owner")

        self.assertIn("Private memory matches", reply)
        self.assertIn("#7 (2026-05-20): PayPal CSV first", reply)
        search.assert_called_once_with("PayPal", limit=5)


if __name__ == "__main__":
    unittest.main()
