"""Native owner fact acknowledgments require durable storage and delivery."""

from contextlib import closing, contextmanager
import sqlite3
import unittest
from unittest.mock import patch

from davosbot import brain, main, memory
from davosbot.db import connect_bot_db
import test_native_command_history as native_fixture


REQUEST = "My color is blue."
EXPECTED_FACT = ("my_color", "blue", "dm")


class FactSaveReceiptTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixture.NativeCommandHistoryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        with closing(sqlite3.connect(self.fixture.db_path)) as conn:
            conn.execute("""
                CREATE TABLE user_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL, value TEXT NOT NULL,
                    source TEXT DEFAULT 'self',
                    timestamp TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.commit()
        self.fixture.stack.enter_context(patch.object(brain, "BOT_DB_PATH", self.fixture.db_path))
        self.fixture.stack.enter_context(patch.object(main, "detect_user_fact", brain.detect_user_fact))
        self.store = self.fixture.stack.enter_context(patch.object(main, "store_user_fact", wraps=brain.store_user_fact))

    def facts(self):
        with closing(sqlite3.connect(self.fixture.db_path)) as conn:
            return conn.execute("SELECT key, value, source FROM user_facts ORDER BY id").fetchall()

    def history(self):
        return memory.get_history(native_fixture.OWNER)

    def assert_unconfirmed(self):
        self.fixture.model.assert_not_called()
        self.store.assert_called_once_with(*EXPECTED_FACT[:2], source="dm")
        self.fixture.send.assert_called_once()
        reply = self.fixture.send.call_args.args[1]
        self.assertIn("couldn't confirm", reply)
        self.assertNotIn("noted that", reply)
        self.assertNotIn("didn't save", reply)
        self.assertEqual([
            {"role": "user", "content": REQUEST},
            {"role": "assistant", "content": reply},
        ], self.history())

    def test_failed_write_does_not_claim_or_remember_success(self):
        with patch.object(brain, "connect_bot_db", side_effect=sqlite3.OperationalError("synthetic private database detail")), \
             patch.object(brain.logger, "warning") as warning:
            self.fixture.route(REQUEST)
        self.assertEqual([], self.facts())
        self.assert_unconfirmed()
        warning.assert_called_once()
        self.assertNotIn("synthetic private database detail", str(warning.call_args))

    def test_failed_commit_rolls_back_without_claiming_success(self):
        original = ("my_color", "green", "dm")
        with connect_bot_db(self.fixture.db_path) as conn:
            conn.execute("INSERT INTO user_facts (key, value, source) VALUES (?, ?, ?)", original)

        @contextmanager
        def fail_commit(path):
            with connect_bot_db(path) as conn:
                yield conn
                raise sqlite3.OperationalError("synthetic commit failure")

        with patch.object(brain, "connect_bot_db", side_effect=fail_commit):
            self.fixture.route(REQUEST)
        self.assertEqual([original], self.facts())
        self.assert_unconfirmed()

    def test_failed_readback_preserves_committed_fact_without_retry_or_false_failure(self):
        calls = 0

        @contextmanager
        def fail_readback(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError("synthetic readback failure")
            with connect_bot_db(path) as conn:
                yield conn

        with patch.object(brain, "connect_bot_db", side_effect=fail_readback):
            self.fixture.route(REQUEST)
        self.assertEqual(2, calls)
        self.assertEqual([EXPECTED_FACT], self.facts())
        self.assert_unconfirmed()

    def test_changed_readback_does_not_claim_success_or_overwrite_concurrent_change(self):
        calls = 0

        @contextmanager
        def change_before_readback(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                with connect_bot_db(path) as conn:
                    conn.execute("UPDATE user_facts SET value = 'green'")
            with connect_bot_db(path) as conn:
                yield conn

        with patch.object(brain, "connect_bot_db", side_effect=change_before_readback):
            self.fixture.route(REQUEST)
        self.assertEqual(2, calls)
        self.assertEqual([("my_color", "green", "dm")], self.facts())
        self.assert_unconfirmed()

    def test_success_is_committed_and_reopened_before_acknowledgment(self):
        def send(recipient, reply):
            self.assertEqual(native_fixture.OWNER, recipient)
            self.assertEqual([EXPECTED_FACT], self.facts())
            self.assertIn("noted that my_color: blue", reply)
            return True

        self.fixture.send.side_effect = send
        with patch.object(brain, "connect_bot_db", wraps=connect_bot_db) as database:
            self.fixture.route(REQUEST)
        self.assertEqual(2, database.call_count)
        self.store.assert_called_once_with(*EXPECTED_FACT[:2], source="dm")
        self.fixture.model.assert_not_called()
        reply = self.fixture.send.call_args.args[1]
        self.assertEqual([
            {"role": "user", "content": REQUEST},
            {"role": "assistant", "content": reply},
        ], self.history())
        self.fixture.send.side_effect = None
        self.fixture.route("What did you note?")
        self.fixture.model.assert_called_once()
        self.assertEqual(reply, self.fixture.model.call_args.args[1][-1]["content"])
        self.store.assert_called_once()

    def test_failed_delivery_preserves_fact_without_false_history_or_second_send(self):
        for raised in (False, True):
            with self.subTest(raised=raised):
                self.fixture.clear_history()
                self.store.reset_mock()
                self.fixture.send.side_effect = RuntimeError("synthetic send failure") if raised else None
                self.fixture.send.return_value = False
                before = len(self.facts())
                self.fixture.route(REQUEST)
                self.assertEqual(before + 1, len(self.facts()))
                self.store.assert_called_once()
                self.fixture.send.assert_called_once()
                self.fixture.model.assert_not_called()
                self.assertFalse(any(row["role"] == "assistant" for row in self.history()))
                self.fixture.send.side_effect = None
                self.fixture.send.return_value = True
                self.fixture.route("What did you note?")
                self.fixture.model.assert_called_once()
                self.store.assert_called_once()
                self.assertEqual(before + 1, len(self.facts()))
                self.assertFalse(any(row["role"] == "assistant" for row in self.fixture.model.call_args.args[1]))

    def test_history_failure_after_send_does_not_retry_or_rollback_fact(self):
        for failed_call in (1, 2):
            with self.subTest(failed_call=failed_call):
                self.fixture.clear_history()
                self.store.reset_mock()
                before = len(self.facts())
                calls = 0

                def save(*args):
                    nonlocal calls
                    calls += 1
                    if calls == failed_call:
                        raise sqlite3.OperationalError("synthetic private history detail")
                    return memory.save_turn(*args)

                with patch.object(main, "save_turn", side_effect=save), \
                     patch.object(main.logger, "warning") as warning:
                    self.fixture.route(REQUEST)
                self.assertEqual(before + 1, len(self.facts()))
                self.store.assert_called_once()
                self.fixture.send.assert_called_once()
                self.fixture.model.assert_not_called()
                self.assertFalse(any(row["role"] == "assistant" for row in self.history()))
                warning.assert_called_once()
                self.assertNotIn("synthetic private history detail", str(warning.call_args))

    def test_nonowner_and_group_requests_never_reach_native_fact_writer(self):
        for sender, chat in (
            (native_fixture.ADMIN, None), (native_fixture.FRIEND, None),
            (native_fixture.OWNER, native_fixture.GROUP),
            (native_fixture.ADMIN, native_fixture.GROUP),
            (native_fixture.FRIEND, native_fixture.GROUP),
        ):
            with self.subTest(sender=sender, chat=chat):
                self.fixture.route(("@Davos " if chat else "") + REQUEST, sender=sender, chat=chat)
        self.store.assert_not_called()
        self.assertEqual([], self.facts())

    def test_private_send_routes_keep_precedence_over_fact_ingestion(self):
        self.fixture.confirmation.return_value = "Synthetic private confirmation."
        self.fixture.route(REQUEST)
        self.fixture.confirmation.return_value = None
        self.fixture.private_send.return_value = "Synthetic private-send request."
        self.fixture.route(REQUEST)
        self.store.assert_not_called()
        self.fixture.model.assert_not_called()
        self.assertEqual([], self.facts())
        self.assertEqual([], self.history())


if __name__ == "__main__":
    unittest.main()
