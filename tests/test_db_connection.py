import unittest
from unittest.mock import patch

from davosbot import db


class FakeConnection:
    def __init__(self):
        self.closed = False
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exited = True
        return False

    def close(self):
        self.closed = True


class BotDbConnectionTests(unittest.TestCase):
    def test_connect_bot_db_closes_after_normal_exit(self):
        conn = FakeConnection()
        with patch.object(db.sqlite3, "connect", return_value=conn):
            with db.connect_bot_db("test.db") as yielded:
                self.assertIs(yielded, conn)

        self.assertTrue(conn.entered)
        self.assertTrue(conn.exited)
        self.assertTrue(conn.closed)

    def test_connect_bot_db_closes_after_exception(self):
        conn = FakeConnection()
        with self.assertRaises(RuntimeError):
            with patch.object(db.sqlite3, "connect", return_value=conn):
                with db.connect_bot_db("test.db"):
                    raise RuntimeError("boom")

        self.assertTrue(conn.entered)
        self.assertTrue(conn.exited)
        self.assertTrue(conn.closed)


if __name__ == "__main__":
    unittest.main()
