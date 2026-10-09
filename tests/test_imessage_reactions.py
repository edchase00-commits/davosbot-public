import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from davosbot import imessage


def _create_chat_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                text TEXT,
                is_from_me INTEGER DEFAULT 0,
                date INTEGER DEFAULT 0,
                handle_id INTEGER,
                associated_message_type INTEGER DEFAULT 0,
                associated_message_guid TEXT
            );
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT);
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT, mime_type TEXT);
            CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
            """
        )
        conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, '+15550000001')")
        conn.execute("INSERT INTO chat (ROWID, chat_identifier) VALUES (1, '+15550000001')")
        conn.execute("INSERT INTO message (ROWID, text, handle_id) VALUES (1, 'old', 1)")
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
        conn.execute(
            "INSERT INTO message (ROWID, text, handle_id, associated_message_type, associated_message_guid) VALUES (2, ?, 1, 2000, 'abc')",
            ('Loved "old"',),
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 2)")
        conn.execute("INSERT INTO message (ROWID, text, handle_id) VALUES (3, 'real ask', 1)")
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 3)")
        conn.execute("INSERT INTO message (ROWID, text, handle_id) VALUES (4, NULL, 1)")
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 4)")
        conn.execute("INSERT INTO attachment (ROWID, filename, mime_type) VALUES (1, '~/pic.HEIC', NULL)")
        conn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (4, 1)")
        conn.commit()
    finally:
        conn.close()


class ImessageReactionTests(unittest.TestCase):
    def test_poll_filters_reactions_and_finds_image_by_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "chat.db"
            _create_chat_db(db_path)
            imessage._last_rowid = 1
            imessage._message_columns_cache = None

            with patch.object(imessage, "DB_PATH", str(db_path)):
                rows = imessage.poll_new_messages()

        texts = [row.get("text") for row in rows]
        self.assertEqual(["real ask", None], texts)
        self.assertTrue(rows[1]["image_path"].endswith("pic.HEIC"))

    def test_find_recent_image_attachment_scopes_to_chat_and_sender(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "chat.db"
            image_path = Path(tmp) / "recent.png"
            image_path.write_bytes(b"png")
            other_path = Path(tmp) / "other.png"
            other_path.write_bytes(b"png")
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE message (
                        ROWID INTEGER PRIMARY KEY,
                        text TEXT,
                        is_from_me INTEGER DEFAULT 0,
                        date INTEGER DEFAULT 0,
                        handle_id INTEGER
                    );
                    CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
                    CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT);
                    CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
                    CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT, mime_type TEXT);
                    CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
                    """
                )
                conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, '+15550000001')")
                conn.execute("INSERT INTO handle (ROWID, id) VALUES (2, '+15550000002')")
                conn.execute("INSERT INTO chat (ROWID, chat_identifier) VALUES (1, 'chat-a')")
                conn.execute("INSERT INTO chat (ROWID, chat_identifier) VALUES (2, 'chat-b')")
                conn.execute("INSERT INTO message (ROWID, text, handle_id) VALUES (10, NULL, 2)")
                conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 10)")
                conn.execute("INSERT INTO attachment (ROWID, filename, mime_type) VALUES (10, ?, 'image/png')", (str(other_path),))
                conn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (10, 10)")
                conn.execute("INSERT INTO message (ROWID, text, handle_id) VALUES (11, NULL, 1)")
                conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 11)")
                conn.execute("INSERT INTO attachment (ROWID, filename, mime_type) VALUES (11, ?, 'image/png')", (str(image_path),))
                conn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (11, 11)")
                conn.execute("INSERT INTO message (ROWID, text, handle_id) VALUES (12, NULL, 1)")
                conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (2, 12)")
                conn.execute("INSERT INTO attachment (ROWID, filename, mime_type) VALUES (12, ?, 'image/png')", (str(other_path),))
                conn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (12, 12)")
                conn.commit()
            finally:
                conn.close()

            with patch.object(imessage, "DB_PATH", str(db_path)):
                found = imessage.find_recent_image_attachment("chat-a", sender="+15550000001")

        self.assertEqual(str(image_path), found)


if __name__ == "__main__":
    unittest.main()
