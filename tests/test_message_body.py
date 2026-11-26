import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from davosbot import imessage
from davosbot.message_body import MAX_BODY_BYTES, decode_attributed_body


def archive(text, *, width=None, byteorder="little"):
    raw = text.encode("utf-8")
    size = len(raw)
    if width is None:
        width = 0 if size < 128 else 2 if size < 65536 else 4
    length = bytes([size]) if not width else bytes([0x81 if width == 2 else 0x82]) + size.to_bytes(width, byteorder)
    signature = b"streamtyped" if byteorder == "little" else b"typedstream"
    return (b"\x04\x0b" + signature + b"\x81\xe8\x03\x84\x01@\x84\x84\x84\x19NSMutableAttributedString"
            b"\x00\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85"
            b"\x92\x84\x84\x84\x08NSString\x01\x95\x84\x01+" + length + raw
            + b"\x86\x84\x02iI\x01\x01metadata must never become a command")


class MessageBodyTests(unittest.TestCase):
    def test_body_preserves_emoji_linebreaks_mentions_and_phone_format(self):
        text = "@Davos grant +1 (206) 555-0123\nplease 🙂\ufffc"
        self.assertEqual(text, decode_attributed_body(archive(text)))

    def test_lengths_are_byte_counts_without_truncation(self):
        for text in ("a" * 127, "é" * 64, "🙂" * 205, "x" * 65536):
            for byteorder in ("little", "big"):
                with self.subTest(size=len(text.encode()), byteorder=byteorder):
                    self.assertEqual(text, decode_attributed_body(archive(text, byteorder=byteorder)))

    def test_unknown_truncated_or_invalid_archives_fail_closed(self):
        valid = archive("hello")
        for bad in (None, "hello", b"hello", b"x" * (MAX_BODY_BYTES + 1),
                    valid[:20], valid.replace(b"\x05hello", b"\x7fhello"),
                    valid.replace(b"hello", b"hell\xff"), archive("hi\x00there"),
                    valid.replace(b"\x05hello", b"\x83hello"),
                    valid.replace(b"hello\x86", b"hello!"),
                    valid.replace(b"NSString", b"BadClass")):
            with self.subTest(type=type(bad).__name__):
                self.assertIsNone(decode_attributed_body(bad))

    def test_poll_rich_text_plain_text_reactions_and_malformed_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.db"
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.executescript("""
                    CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB,
                        is_from_me INTEGER DEFAULT 0, date INTEGER DEFAULT 0, handle_id INTEGER,
                        associated_message_type INTEGER DEFAULT 0, associated_message_guid TEXT);
                    CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
                    CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT);
                    CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
                    CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT, mime_type TEXT);
                    CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
                    INSERT INTO handle VALUES (1, '+15550000001');
                    INSERT INTO chat VALUES (1, '+15550000001');
                """)
                rows = [(2, None, archive("@Davos status"), 0, None),
                        (3, "plain wins", archive("wrong"), 0, None),
                        (4, None, archive('Loved "test"'), 2000, "reaction"),
                        (5, None, b"broken", 0, None),
                        (6, None, b"broken with image", 0, None),
                        (7, "", archive("long " + "a" * 820), 0, None)]
                conn.executemany("INSERT INTO message (ROWID,text,attributedBody,associated_message_type,associated_message_guid,handle_id) VALUES (?,?,?,?,?,1)", rows)
                conn.executemany("INSERT INTO chat_message_join VALUES (1,?)", [(row[0],) for row in rows])
                conn.execute("INSERT INTO attachment VALUES (1, '~/photo.HEIC', NULL)")
                conn.execute("INSERT INTO message_attachment_join VALUES (6,1)")
            with patch.object(imessage, "DB_PATH", str(path)), patch.object(imessage, "_last_rowid", 1), patch.object(imessage, "_message_columns_cache", None):
                messages = imessage.poll_new_messages()
                self.assertEqual([2, 3, 6, 7], [row["ROWID"] for row in messages])
                self.assertEqual("@Davos status", messages[0]["text"])
                self.assertEqual("plain wins", messages[1]["text"])
                self.assertTrue(messages[2]["image_path"].endswith("photo.HEIC"))
                self.assertEqual(825, len(messages[3]["text"]))
                self.assertTrue(all("attributed_body" not in row for row in messages))
                self.assertEqual([], imessage.poll_new_messages())


if __name__ == "__main__":
    unittest.main()
