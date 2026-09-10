import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from davosbot import imessage, main


class NoSearchTextTests(unittest.TestCase):
    def test_directive_removal_preserves_names_filenames_and_punctuation(self):
        cases = {
            "Naples no web search": "Naples",
            "no search Naples": "Naples",
            "notes.csv without search": "notes.csv",
            "no web --help": "--help",
            "no search ...please explain Naples!": "...please explain Naples!",
            "show notes.csv, skip web search": "show notes.csv,",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual((expected, True), main._strip_no_search(text))

    def test_polite_variants_preserve_complete_words(self):
        for text, expected in (
            ("no web search please explain Naples", "please explain Naples"),
            ("Please show Naples don't search", "Please show Naples"),
            ("pls show notes.csv skip search", "pls show notes.csv"),
        ):
            with self.subTest(text=text):
                self.assertEqual((expected, True), main._strip_no_search(text))

    def test_newlines_and_text_without_a_directive_remain_intact(self):
        self.assertEqual(("Naples\nnotes.csv", True), main._strip_no_search("no web search\nNaples\nnotes.csv"))
        self.assertEqual(("Naples\n\nnotes.csv", True), main._strip_no_search("Naples\nno search\nnotes.csv"))
        text = "  Please explain Naples!\nnotes.csv  "
        self.assertEqual((text, False), main._strip_no_search(text))
        self.assertEqual(("", True), main._strip_no_search("no search"))


class InboundWatermarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "chat.db"
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executescript("""
                CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT,
                    is_from_me INTEGER DEFAULT 0, date INTEGER DEFAULT 0, handle_id INTEGER);
                CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
                CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT);
                CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
                CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT, mime_type TEXT);
                CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
                INSERT INTO handle VALUES (1, '+15550000001');
                INSERT INTO chat VALUES (1, '+15550000001');
            """)
        for name, value in (("DB_PATH", str(self.path)), ("_last_rowid", None), ("_message_columns_cache", None)):
            patcher = patch.object(imessage, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def insert_message(self, rowid, text):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO message (ROWID, text, handle_id) VALUES (?, ?, 1)", (rowid, text))
            conn.execute("INSERT INTO chat_message_join VALUES (1, ?)", (rowid,))

    def test_first_message_after_empty_start_is_received_once(self):
        self.assertEqual([], imessage.poll_new_messages())
        self.assertEqual(0, imessage._last_rowid)
        self.assertEqual([], imessage.poll_new_messages())
        self.insert_message(1, "first inbound ask")

        self.assertEqual(["first inbound ask"], [msg["text"] for msg in imessage.poll_new_messages()])
        self.assertEqual(1, imessage._last_rowid)
        self.assertEqual([], imessage.poll_new_messages())

    def test_startup_still_skips_historical_messages(self):
        self.insert_message(5, "historical ask")
        self.assertEqual([], imessage.poll_new_messages())
        self.assertEqual(5, imessage._last_rowid)
        self.insert_message(6, "new ask")
        self.assertEqual(["new ask"], [msg["text"] for msg in imessage.poll_new_messages()])
        self.assertEqual([], imessage.poll_new_messages())

    def test_failed_initial_connection_can_retry_initialization(self):
        with patch.object(imessage, "_get_db", side_effect=sqlite3.OperationalError("temporarily unavailable")):
            self.assertEqual([], imessage.poll_new_messages())
        self.assertIsNone(imessage._last_rowid)
        self.assertEqual([], imessage.poll_new_messages())
        self.insert_message(1, "received after reconnect")
        self.assertEqual([1], [msg["ROWID"] for msg in imessage.poll_new_messages()])


if __name__ == "__main__":
    unittest.main()
