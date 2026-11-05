import ast
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from davosbot.inbox import MessageInbox, InboxSourceError, inbox_health, initialize_schema
from davosbot.inbox_workers import InboxWorkerError
from inbox_fixtures import SourceFixture, NOW, OWNER, FRIEND
from test_message_body import archive


class InboxLoopTests(unittest.TestCase):
    def test_intake_hold_reports_error_without_suppressing_existing_timers(self):
        # Execute only the real loop, never startup/configuration or send code.
        source = Path(__file__).resolve().parents[1] / "davosbot" / "main.py"
        main = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body
                    if isinstance(node, ast.FunctionDef) and node.name == "_run_main_loop")
        loop = next(node for node in main.body if isinstance(node, ast.While))
        code = compile(ast.Module(body=[loop], type_ignores=[]), str(source), "exec")
        timers = ("_check_reminders", "_check_scheduled_tasks", "_check_cron_jobs",
                  "check_ollama_recovery", "_check_session_heartbeat")
        for failing_method in ("poll", "raise_if_failed"):
            with self.subTest(failing_method=failing_method):
                class StopProbe(BaseException):
                    pass
                inbox = Mock()
                workers = Mock()
                target = inbox if failing_method == "poll" else workers
                getattr(target, failing_method).side_effect = (OSError("synthetic intake hold") if failing_method == "poll"
                                                              else [None, OSError("synthetic intake hold")])
                called = []
                namespace = {name: (lambda name=name: called.append(name)) for name in timers}
                namespace.update(inbox=inbox, workers=workers, logger=Mock(),
                                 InboxWorkerError=InboxWorkerError,
                                 traceback=SimpleNamespace(format_exc=lambda: "synthetic intake hold"),
                                 redact_secret=lambda value: value, send_owner_alert=Mock(),
                                 time=SimpleNamespace(time=lambda: NOW, sleep=Mock(side_effect=StopProbe)),
                                 _LAST_MAIN_LOOP_ALERT=0, _MAIN_LOOP_ALERT_INTERVAL=300, POLL_INTERVAL=1)
                with self.assertRaises(StopProbe):
                    exec(code, namespace)
                self.assertEqual(list(timers), called)
                namespace["logger"].error.assert_called_once()
                namespace["send_owner_alert"].assert_called_once()
                if failing_method == "poll":
                    workers.wake.assert_not_called()


class InboxRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = SourceFixture(temporary.name)
        self.clock = [NOW]
        self.inbox = self.restart()

    def restart(self):
        return MessageInbox(self.source.path, self.source.bot_path, now=lambda: self.clock[0])

    def states(self):
        with closing(sqlite3.connect(self.source.bot_path)) as conn:
            return {row[0]: (row[1], row[2]) for row in conn.execute("SELECT source_rowid,state,reason FROM inbound_messages")}

    def initialize(self):
        self.assertEqual(0, self.inbox.poll())

    def test_empty_first_start_and_first_new_message(self):
        self.initialize()
        self.assertEqual(0, self.inbox.poll())
        self.source.add_message(1)
        self.assertEqual(1, self.inbox.poll())
        seen = []
        self.assertEqual(1, self.inbox.dispatch_ready(seen.append))
        self.assertEqual([1], [m["ROWID"] for m in seen])
        self.assertEqual(0, self.inbox.dispatch_ready(seen.append))

    def test_initial_cutover_skips_history_only_once(self):
        self.source.add_message(8, "historical")
        self.initialize()
        self.source.add_message(9, "arrived during downtime")
        restored = self.restart()
        self.assertEqual(1, restored.poll())
        seen = []
        restored.dispatch_ready(seen.append)
        self.assertEqual([9], [m["ROWID"] for m in seen])

    def test_ingested_but_unclaimed_batch_survives_restart(self):
        self.initialize()
        for rowid in (1, 2, 3):
            self.source.add_message(rowid)
        self.inbox.poll()
        self.inbox.dispatch_ready(lambda _: None, limit=1)
        restored = self.restart()
        restored.poll()
        seen = []
        restored.dispatch_ready(seen.append)
        self.assertEqual([2, 3], [m["ROWID"] for m in seen])
        self.assertEqual("handler_returned", self.states()[1][0])

    def test_claim_without_acknowledgement_is_not_replayed(self):
        self.initialize()
        self.source.add_message(1)
        self.source.add_message(2)
        self.inbox.poll()
        self.assertEqual(1, self.inbox.claim_next()["ROWID"])
        restored = self.restart()
        seen = []
        restored.dispatch_ready(seen.append)
        self.assertEqual([2], [m["ROWID"] for m in seen])
        self.assertEqual(("uncertain", "interrupted"), self.states()[1])

    def test_exception_after_effect_does_not_repeat_effect(self):
        self.initialize()
        self.source.add_message(1)
        self.inbox.poll()
        effects = []
        def interrupted(message):
            effects.append(message["guid"])
            raise SystemExit("synthetic process death")
        with self.assertRaises(SystemExit):
            self.inbox.dispatch_ready(interrupted)
        self.restart().dispatch_ready(effects.append)
        self.assertEqual(["synthetic-guid-1"], effects)
        self.assertEqual(("uncertain", "dispatch_exception"), self.states()[1])

    def test_ack_write_failure_does_not_repeat_effect(self):
        self.initialize()
        self.source.add_message(1)
        self.inbox.poll()
        effects = []
        with patch.object(self.inbox, "finish", side_effect=sqlite3.OperationalError("synthetic write failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.inbox.dispatch_ready(effects.append)
        self.restart().dispatch_ready(effects.append)
        self.assertEqual(1, len(effects))
        self.assertEqual(("uncertain", "interrupted"), self.states()[1])

    def test_handler_returned_never_claims_action_success(self):
        self.initialize()
        self.source.add_message(1)
        self.inbox.poll()
        self.inbox.dispatch_ready(lambda _: False)
        health = inbox_health(self.source.bot_path, now=NOW)
        self.assertEqual({"handler_returned": 1}, health["counts"])
        self.assertNotIn("success", json.dumps(health))

    def test_intake_transaction_failure_rolls_back_rows_and_cursor(self):
        self.initialize()
        self.source.add_message(1)
        with closing(sqlite3.connect(self.source.bot_path)) as conn, conn:
            conn.execute("CREATE TRIGGER synthetic_failure BEFORE UPDATE OF cursor_rowid ON inbound_source BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.inbox.poll()
        self.assertEqual({}, self.states())
        with closing(sqlite3.connect(self.source.bot_path)) as conn, conn:
            self.assertEqual(0, conn.execute("SELECT cursor_rowid FROM inbound_source").fetchone()[0])
            conn.execute("DROP TRIGGER synthetic_failure")
        self.assertEqual(1, self.inbox.poll())

    def test_delayed_join_is_retried_after_higher_cursor(self):
        self.initialize()
        self.source.add_message(1, chat_id=None)
        self.source.add_message(2, sender_id=2, chat_id=2)
        self.inbox.poll()
        seen = []
        self.inbox.dispatch_ready(seen.append)
        self.assertEqual([2], [m["ROWID"] for m in seen])
        self.source.add_join(1)
        self.inbox.poll()
        self.inbox.dispatch_ready(seen.append)
        self.assertEqual([2, 1], [m["ROWID"] for m in seen])

    def test_attachment_join_and_file_are_retried_before_caption(self):
        self.initialize()
        self.source.add_message(1, "scan this screenshot", attachment=True)
        self.inbox.poll()
        seen = []
        self.assertEqual(0, self.inbox.dispatch_ready(seen.append))
        image = self.source.add_image(1, present=False)
        self.assertEqual(0, self.inbox.dispatch_ready(seen.append))
        image.write_bytes(b"synthetic readable image")
        self.assertEqual(1, self.inbox.dispatch_ready(seen.append))
        self.assertEqual("scan this screenshot", seen[0]["text"])
        self.assertEqual(str(image), seen[0]["image_path"])

    def test_known_chat_fifo_waits_but_other_chat_can_run(self):
        self.initialize()
        self.source.add_message(1, "caption", attachment=True)
        self.source.add_message(2, "following same chat")
        self.source.add_message(3, "different chat", sender_id=2, chat_id=2)
        self.inbox.poll()
        seen = []
        self.inbox.dispatch_ready(seen.append)
        self.assertEqual([3], [m["ROWID"] for m in seen])
        self.source.add_image(1)
        self.inbox.dispatch_ready(seen.append)
        self.assertEqual([3, 1, 2], [m["ROWID"] for m in seen])

    def test_expired_incomplete_row_unblocks_chat_without_dispatch(self):
        self.initialize()
        self.source.add_message(1, attachment=True)
        self.source.add_message(2)
        self.inbox.poll()
        self.assertIsNone(self.inbox.claim_next())
        self.clock[0] += 121
        seen = []
        self.inbox.dispatch_ready(seen.append)
        self.assertEqual([2], [m["ROWID"] for m in seen])
        self.assertEqual(("held", "attachment_not_ready"), self.states()[1])

    def test_late_attachment_cannot_retrigger_completed_command(self):
        self.initialize()
        self.source.add_message(1, "synthetic command")
        self.inbox.poll()
        seen = []
        self.inbox.dispatch_ready(seen.append)
        self.source.add_image(1)
        self.inbox.poll()
        self.restart().dispatch_ready(seen.append)
        self.assertEqual(1, len(seen))

    def test_rich_text_and_reactions_keep_existing_rules(self):
        self.initialize()
        self.source.add_message(1, None, attributed_body=archive("@Davos what's up?"))
        self.source.add_message(2, 'Loved "that"', reaction=True)
        self.source.add_message(3, None, attributed_body=b"invalid body")
        self.inbox.poll()
        seen = []
        self.inbox.dispatch_ready(seen.append)
        self.assertEqual(["@Davos what's up?"], [m["text"] for m in seen])
        self.assertEqual(("ignored", "reaction"), self.states()[2])
        self.assertEqual("pending", self.states()[3][0])

    def test_poll_error_propagates_and_health_exposes_it(self):
        self.initialize()
        with self.source.connect() as conn:
            conn.execute("DROP TABLE message")
        with self.assertRaises(sqlite3.Error):
            self.inbox.poll()
        self.assertEqual("source_or_ledger_unavailable", inbox_health(self.source.bot_path)["source_error"])

    def test_source_database_is_read_only(self):
        with self.inbox._source() as source:
            with self.assertRaises(sqlite3.OperationalError):
                source.execute("DELETE FROM message")

    def test_health_does_not_create_a_database(self):
        path = self.source.directory / "absent.sqlite"
        self.assertFalse(inbox_health(path)["available"])
        self.assertFalse(path.exists())

    def test_health_contains_counts_without_message_or_origin_content(self):
        self.initialize()
        secret = "a synthetic secret that must stay in Apple source"
        self.source.add_message(1, secret)
        self.inbox.poll()
        self.inbox.dispatch_ready(lambda _: None)
        serialized = json.dumps(inbox_health(self.source.bot_path, now=NOW))
        for value in (secret, OWNER, FRIEND, "synthetic-guid-1"):
            self.assertNotIn(value, serialized)
        with closing(sqlite3.connect(self.source.bot_path)) as conn:
            self.assertNotIn(secret, "\n".join(conn.iterdump()))

    def test_additive_schema_is_idempotent_and_preserves_other_tables(self):
        with closing(sqlite3.connect(self.source.bot_path)) as conn, conn:
            conn.execute("CREATE TABLE unrelated (value TEXT)")
            conn.execute("INSERT INTO unrelated VALUES ('keep me')")
        initialize_schema(self.source.bot_path)
        initialize_schema(self.source.bot_path)
        with closing(sqlite3.connect(self.source.bot_path)) as conn:
            self.assertEqual([("keep me",)], conn.execute("SELECT * FROM unrelated").fetchall())

    def test_deleted_anchor_does_not_rebase_or_block_new_arrivals(self):
        self.initialize()
        self.source.add_message(1, reaction=True)
        self.inbox.poll()
        self.inbox.dispatch_ready(lambda _: None)
        with self.source.connect() as conn:
            conn.execute("DELETE FROM message WHERE ROWID=1")
        self.assertEqual(0, self.inbox.poll())
        self.assertTrue(inbox_health(self.source.bot_path)["anchor_missing"])
        self.source.add_message(2)
        self.assertEqual(1, self.inbox.poll())
        seen = []
        self.inbox.dispatch_ready(seen.append)
        self.assertEqual([2], [m["ROWID"] for m in seen])
        self.assertFalse(inbox_health(self.source.bot_path)["anchor_missing"])

    def test_vacuum_preserving_file_and_message_identities_does_not_replay(self):
        self.initialize()
        self.source.add_message(1)
        self.inbox.poll()
        self.inbox.dispatch_ready(lambda _: None)
        identity = self.inbox._source_identity()
        with self.source.connect() as conn:
            conn.execute("VACUUM")
        if self.inbox._source_identity() != identity:
            with self.assertRaisesRegex(InboxSourceError, "source_identity_changed"):
                self.inbox.poll()
            return
        self.assertEqual(0, self.inbox.poll())
        self.assertIsNone(self.restart().claim_next())
        self.source.add_message(2)
        self.assertEqual(1, self.inbox.poll())
        self.assertEqual(2, self.inbox.claim_next()["ROWID"])

    def test_live_processing_age_is_distinct_from_handler_returned(self):
        self.initialize()
        self.source.add_message(1)
        self.inbox.poll()
        self.inbox.claim_next()
        health = inbox_health(self.source.bot_path, now=NOW + 30)
        self.assertEqual(30, health["oldest_processing_age_seconds"])
        self.assertIsNone(health["oldest_pending_age_seconds"])

    def test_schema_creation_uses_existing_migration_callback(self):
        calls = []
        initialize_schema(self.source.bot_path, migrate=lambda sql, description: calls.append((sql, description)))
        self.assertEqual(4, len(calls))
        self.assertTrue(all("CREATE" in sql and description for sql, description in calls))


if __name__ == "__main__":
    unittest.main()
