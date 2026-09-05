"""Synthetic Apple Messages database fixtures; no runtime/config imports."""

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


NOW = 1788566400.0
APPLE_EPOCH = 978307200
OWNER = "+15550000001"
FRIEND = "+15550000002"
GROUP = "a" * 32


class SourceFixture:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "synthetic-chat.sqlite"
        self.bot_path = self.directory / "synthetic-inbox.sqlite"
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE message (
                    ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT,
                    attributedBody BLOB, is_from_me INTEGER DEFAULT 0,
                    date INTEGER, handle_id INTEGER,
                    cache_has_attachments INTEGER DEFAULT 0,
                    associated_message_type INTEGER DEFAULT 0,
                    associated_message_guid TEXT
                );
                CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
                CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT);
                CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
                CREATE TABLE attachment (
                    ROWID INTEGER PRIMARY KEY, filename TEXT,
                    mime_type TEXT, transfer_state INTEGER DEFAULT 5
                );
                CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
            """)
            conn.executemany("INSERT INTO handle VALUES (?, ?)", [(1, OWNER), (2, FRIEND)])
            conn.executemany("INSERT INTO chat VALUES (?, ?)", [(1, OWNER), (2, FRIEND), (3, GROUP)])

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            yield conn

    def add_message(self, rowid, text="synthetic request", *, guid=None,
                    sender_id=1, chat_id=1, timestamp=NOW, from_me=False,
                    attachment=False, attributed_body=None, reaction=False):
        date = int((timestamp - APPLE_EPOCH) * 1_000_000_000) if timestamp is not None else None
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO message
                   (ROWID,guid,text,attributedBody,is_from_me,date,handle_id,
                    cache_has_attachments,associated_message_type)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (rowid, guid or f"synthetic-guid-{rowid}", text, attributed_body,
                 int(from_me), date, sender_id, int(attachment), 2000 if reaction else 0),
            )
            if chat_id is not None:
                conn.execute("INSERT INTO chat_message_join VALUES (?, ?)", (chat_id, rowid))

    def add_join(self, rowid, chat_id=1):
        with self.connect() as conn:
            conn.execute("INSERT INTO chat_message_join VALUES (?, ?)", (chat_id, rowid))

    def add_image(self, rowid, *, present=True, attachment_id=None):
        attachment_id = attachment_id or rowid
        image = self.directory / f"synthetic-image-{attachment_id}.png"
        if present:
            image.write_bytes(b"synthetic image bytes; no provider is invoked")
        with self.connect() as conn:
            conn.execute("INSERT INTO attachment VALUES (?, ?, 'image/png', 5)", (attachment_id, str(image)))
            conn.execute("INSERT INTO message_attachment_join VALUES (?, ?)", (rowid, attachment_id))
        return image
