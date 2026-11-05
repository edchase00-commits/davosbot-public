"""Bounded real workers, real synthetic inbox/source DBs, and no live actions."""

import ast
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from davosbot import inbox as inbox_module
from davosbot.config import normalize_handle
from davosbot.inbox import MessageInbox, inbox_health
from davosbot.inbox_workers import InboxWorkers, InboxWorkerError
from inbox_fixtures import SourceFixture, NOW


class InboxWorkerPermissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = SourceFixture(temporary.name)
        self.inbox = self.consumer()
        self.inbox.poll()
        self.release = threading.Event()
        # Always unblock fake handlers before joining, including a failed test.
        self.pools = []
        self.addCleanup(self.cleanup_workers)

    def consumer(self, **kwargs):
        return MessageInbox(self.source.path, self.source.bot_path, now=lambda: NOW,
                            normalize_sender=normalize_handle, **kwargs)

    def cleanup_workers(self):
        self.release.set()
        for pool in self.pools:
            self.assertEqual(pool.stop(timeout=3), 0)

    def pool(self, handler, inbox=None):
        pool = InboxWorkers(inbox or self.inbox, handler)
        self.pools.append(pool)
        pool.start()
        return pool

    def block(self):
        if not self.release.wait(4):
            raise AssertionError("test did not release handler")

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.005)
        self.fail("condition did not become true")

    def counts(self):
        return inbox_health(self.source.bot_path, now=NOW)["counts"]

    def add_actor(self, actor):
        with self.source.connect() as conn:
            conn.execute("INSERT INTO handle VALUES (?,?)", (actor, f"sender-{actor}"))
            conn.execute("INSERT INTO chat VALUES (?,?)", (actor, f"chat-{actor}"))

    def test_independent_chat_completes_while_actual_main_loop_polls_and_ticks(self):
        first, second, later_tick = threading.Event(), threading.Event(), threading.Event()
        seen = []
        def handler(message):
            seen.append(message["ROWID"])
            if message["ROWID"] == 1:
                first.set()
                self.block()
            else:
                second.set()
        pool = self.pool(handler)
        path = Path(__file__).resolve().parents[1] / "davosbot" / "main.py"
        node = next(n for n in ast.parse(path.read_text(encoding="utf-8")).body
                    if isinstance(n, ast.FunctionDef) and n.name == "_run_main_loop")
        timers = ("_check_reminders", "_check_scheduled_tasks", "_check_cron_jobs",
                  "check_ollama_recovery", "_check_session_heartbeat")
        calls = []
        def tick(name):
            calls.append(name)
            if first.is_set() and len(calls) >= 10:
                later_tick.set()
        stopped = threading.Event()
        class StopLoop(BaseException):
            pass
        def pause(_):
            if stopped.wait(.005):
                raise StopLoop()
        namespace = {name: (lambda name=name: tick(name)) for name in timers}
        namespace.update(logger=Mock(), traceback=SimpleNamespace(format_exc=lambda: "synthetic"),
                         InboxWorkerError=InboxWorkerError,
                         redact_secret=lambda value: value, send_owner_alert=Mock(),
                         time=SimpleNamespace(time=time.time, sleep=pause), POLL_INTERVAL=.005,
                         _LAST_MAIN_LOOP_ALERT=0, _MAIN_LOOP_ALERT_INTERVAL=300)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
        def run():
            try:
                namespace["_run_main_loop"](self.inbox, pool)
            except StopLoop:
                pass
        self.source.add_message(1)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            self.assertTrue(first.wait(2))
            self.source.add_message(2, sender_id=2, chat_id=2)
            self.assertTrue(second.wait(2))
            self.assertTrue(later_tick.wait(2))
            self.assertFalse(self.release.is_set())
            self.assertEqual([1, 2], seen)
            self.wait_for(lambda: self.counts().get("handler_returned") == 1)
            self.assertEqual(1, self.counts().get("processing"))
            self.assertTrue(all(calls.count(name) >= 2 for name in timers))
            namespace["logger"].error.assert_not_called()
        finally:
            stopped.set()
            thread.join(2)
            self.release.set()
        self.assertFalse(thread.is_alive())

    def test_pool_has_only_three_claimed_handlers_and_no_executor_queue(self):
        for actor in (4, 5):
            self.add_actor(actor)
        for rowid, actor in enumerate((1, 2, 4, 5), 1):
            self.source.add_message(rowid, sender_id=actor, chat_id=actor)
        self.inbox.poll()
        lock = threading.Lock()
        active = []
        def handler(message):
            with lock:
                active.append(message["ROWID"])
            self.block()
        pool = self.pool(handler)
        pool.wake()
        self.wait_for(lambda: len(active) == 3)
        self.assertEqual({"processing": 3, "pending": 1}, self.counts())
        self.assertEqual(3, len(pool._threads))

    def test_worker_failure_runs_existing_timers_then_exits_for_supervisor(self):
        path = Path(__file__).resolve().parents[1] / "davosbot" / "main.py"
        node = next(n for n in ast.parse(path.read_text(encoding="utf-8")).body
                    if isinstance(n, ast.FunctionDef) and n.name == "_run_main_loop")
        timers = ("_check_reminders", "_check_scheduled_tasks", "_check_cron_jobs",
                  "check_ollama_recovery", "_check_session_heartbeat")
        namespace = {name: Mock() for name in timers}
        namespace.update(logger=Mock(), InboxWorkerError=InboxWorkerError)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
        workers = Mock()
        workers.raise_if_failed.side_effect = [None, InboxWorkerError("inbox_worker_stopped:RuntimeError")]
        intake = Mock()
        intake.poll.side_effect = OSError("source is also unavailable")
        with self.assertRaises(InboxWorkerError):
            namespace["_run_main_loop"](intake, workers)
        for name in timers:
            namespace[name].assert_called_once()
        namespace["logger"].error.assert_called_once()

    def test_dead_pool_exits_even_when_a_timer_persistently_throws(self):
        path = Path(__file__).resolve().parents[1] / "davosbot" / "main.py"
        node = next(n for n in ast.parse(path.read_text(encoding="utf-8")).body
                    if isinstance(n, ast.FunctionDef) and n.name == "_run_main_loop")
        timer = Mock(side_effect=OSError("synthetic timer failure"))
        namespace = dict(_check_reminders=timer, logger=Mock(), InboxWorkerError=InboxWorkerError,
                         traceback=SimpleNamespace(format_exc=lambda: "synthetic timer failure"),
                         redact_secret=lambda value: value, send_owner_alert=Mock(),
                         time=SimpleNamespace(time=lambda: NOW, sleep=Mock()), POLL_INTERVAL=1,
                         _LAST_MAIN_LOOP_ALERT=0, _MAIN_LOOP_ALERT_INTERVAL=300)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
        workers = Mock()
        workers.raise_if_failed.side_effect = [None, InboxWorkerError("inbox_worker_stopped:RuntimeError")]
        with self.assertRaises(InboxWorkerError):
            namespace["_run_main_loop"](Mock(), workers)
        timer.assert_called_once()
        namespace["time"].sleep.assert_called_once()

    def test_same_chat_or_sender_fifo_propagates_through_overlap_chain(self):
        self.add_actor(4)
        for rowid, sender, chat in ((1, 1, 1), (2, 2, 1), (3, 2, 2), (4, 4, 4)):
            self.source.add_message(rowid, sender_id=sender, chat_id=chat)
        self.inbox.poll()
        started, independent = threading.Event(), threading.Event()
        seen = []
        def handler(message):
            seen.append(message["ROWID"])
            if message["ROWID"] == 1:
                started.set()
                self.block()
            elif message["ROWID"] == 4:
                independent.set()
        pool = self.pool(handler)
        pool.wake()
        self.assertTrue(started.wait(2))
        self.assertTrue(independent.wait(2))
        self.assertEqual({1, 4}, set(seen))
        self.release.set()
        self.wait_for(lambda: self.counts().get("handler_returned") == 4)
        self.assertLess(seen.index(2), seen.index(3))

    def test_formatted_sender_cannot_bypass_cross_chat_reservation(self):
        with self.source.connect() as conn:
            conn.execute("INSERT INTO handle VALUES(3,'+1 (555) 000-0001')")
        self.source.add_message(1)
        self.source.add_message(2, sender_id=3, chat_id=3)
        self.inbox.poll()
        started = threading.Event()
        seen = []
        def handler(message):
            seen.append(message["ROWID"])
            if message["ROWID"] == 1:
                started.set()
                self.block()
        pool = self.pool(handler)
        pool.wake()
        self.assertTrue(started.wait(2))
        self.assertEqual({"processing": 1, "pending": 1}, self.counts())
        self.release.set()
        self.wait_for(lambda: self.counts().get("handler_returned") == 2)
        self.assertEqual([1, 2], seen)

    def test_late_sender_reserves_fifo_while_chat_join_is_still_missing(self):
        self.source.add_message(1, sender_id=4, chat_id=None)
        self.inbox.poll()
        with self.source.connect() as conn:
            conn.execute("INSERT INTO handle VALUES(4,'+1 (555) 000-0001')")
        self.source.add_message(2)
        self.inbox.poll()
        self.assertIsNone(self.inbox.claim_next())
        self.source.add_join(1, 3)
        seen = []
        self.inbox.dispatch_ready(seen.append)
        self.assertEqual([1, 2], [message["ROWID"] for message in seen])

    def test_late_chat_reserves_fifo_while_sender_handle_is_still_missing(self):
        self.source.add_message(1, sender_id=4, chat_id=None)
        self.inbox.poll()
        self.source.add_join(1, 3)
        self.source.add_message(2, sender_id=2, chat_id=3)
        self.inbox.poll()
        self.assertIsNone(self.inbox.claim_next())
        with self.source.connect() as conn:
            conn.execute("INSERT INTO handle VALUES(4,'another-synthetic-sender')")
        seen = []
        self.inbox.dispatch_ready(seen.append)
        self.assertEqual([1, 2], [message["ROWID"] for message in seen])

    def test_stop_does_not_recover_or_replace_still_running_handler(self):
        self.source.add_message(1)
        self.source.add_message(2)
        self.inbox.poll()
        entered = threading.Event()
        def handler(_):
            entered.set()
            self.block()
        pool = self.pool(handler)
        pool.wake()
        self.assertTrue(entered.wait(2))
        self.assertGreater(pool.stop(timeout=.02), 0)
        self.assertEqual({"processing": 1, "pending": 1}, self.counts())
        with self.assertRaisesRegex(RuntimeError, "inbox_workers_still_running"):
            self.consumer()
        with self.assertRaisesRegex(RuntimeError, "inbox_workers_still_running"):
            InboxWorkers(self.inbox, Mock()).start()
        self.release.set()
        self.assertEqual(pool.stop(timeout=3), 0)
        self.assertEqual({"handler_returned": 1, "pending": 1}, self.counts())
        self.consumer()  # Only actual thread exit releases replacement ownership.

    def test_stop_barrier_rejects_claim_still_reading_source(self):
        self.source.add_message(1)
        self.inbox.poll()
        entered = threading.Event()
        inspect = self.inbox._inspect_pending
        def read(*args):
            result = inspect(*args)
            entered.set()
            self.block()
            return result
        handler = Mock()
        with patch.object(self.inbox, "_inspect_pending", side_effect=read):
            pool = self.pool(handler)
            pool.wake()
            self.assertTrue(entered.wait(2))
            self.assertGreater(pool.stop(timeout=.02), 0)
            self.release.set()
            self.assertEqual(pool.stop(timeout=3), 0)
        handler.assert_not_called()
        self.assertEqual({"pending": 1}, self.counts())

    def test_handler_exception_stops_pool_without_repeating_uncertain_effect(self):
        self.source.add_message(1)
        self.source.add_message(2)
        self.inbox.poll()
        effects = []
        def handler(message):
            effects.append(message["ROWID"])
            raise RuntimeError("synthetic sensitive error must not be reported")
        pool = self.pool(handler)
        pool.wake()
        self.wait_for(lambda: pool._failure is not None)
        self.assertEqual(pool.stop(timeout=3), 0)
        with self.assertRaisesRegex(InboxWorkerError, "^inbox_worker_stopped:RuntimeError$"):
            pool.raise_if_failed()
        self.assertEqual([1], effects)
        self.assertEqual({"uncertain": 1, "pending": 1}, self.counts())

    def test_failed_acknowledgement_keeps_processing_until_real_recovery(self):
        self.source.add_message(1)
        self.source.add_message(2)
        self.inbox.poll()
        handler = Mock()
        with patch.object(self.inbox, "finish", side_effect=sqlite3.OperationalError("synthetic")):
            pool = self.pool(handler)
            pool.wake()
            self.wait_for(lambda: pool._failure is not None)
            self.assertEqual(pool.stop(timeout=3), 0)
        self.assertEqual(1, handler.call_count)
        self.assertEqual({"processing": 1, "pending": 1}, self.counts())
        self.consumer()
        self.assertEqual({"uncertain": 1, "pending": 1}, self.counts())

    def test_failure_stops_admission_before_uncertain_commit_releases_sender(self):
        self.source.add_message(1)
        self.source.add_message(2, "confirmation")
        self.inbox.poll()
        committed = threading.Event()
        finish = self.inbox.finish
        def paused_finish(*args, **kwargs):
            finish(*args, **kwargs)
            committed.set()
            self.block()
        handler = Mock(side_effect=RuntimeError("synthetic handler failure"))
        with patch.object(self.inbox, "finish", side_effect=paused_finish):
            pool = self.pool(handler)
            pool.wake()
            self.assertTrue(committed.wait(2))
            self.assertTrue(pool._stopping)
            pool.wake()
            self.assertEqual({"uncertain": 1, "pending": 1}, self.counts())
            self.assertEqual(handler.call_count, 1)
            self.release.set()
            self.assertEqual(pool.stop(timeout=3), 0)

    def test_recovered_private_confirmation_waits_for_sender_fence(self):
        self.source.add_message(1, "prepare request", chat_id=3)
        restored = self.consumer(confirmation_guard=lambda message: message["text"] == "synthetic password")
        restored.poll()
        entered = threading.Event()
        seen = []
        def handler(message):
            seen.append(message["ROWID"])
            entered.set()
            self.block()
        pool = self.pool(handler, restored)
        pool.wake()
        self.assertTrue(entered.wait(2))
        self.source.add_message(2, "synthetic password", chat_id=1)
        restored.poll()
        pool.wake()
        self.assertEqual({"processing": 1, "pending": 1}, self.counts())
        self.release.set()
        self.wait_for(lambda: self.counts().get("held") == 1)
        self.assertEqual([1], seen)
        self.assertEqual({"fresh_confirmation_required": 1}, inbox_health(self.source.bot_path)["held_reasons"])

    def test_backpressure_keeps_cursor_and_releases_space_after_completion(self):
        for rowid in range(1, 6):
            self.source.add_message(rowid)
        with patch.object(inbox_module, "MAX_PENDING_MESSAGES", 3):
            self.assertEqual(3, self.inbox.poll())
            self.assertEqual(0, self.inbox.poll())
            health = inbox_health(self.source.bot_path)
            self.assertEqual("intake_backpressure", health["source_error"])
            with closing(sqlite3.connect(self.source.bot_path)) as conn:
                self.assertEqual(3, conn.execute("SELECT cursor_rowid FROM inbound_source").fetchone()[0])
            self.inbox.dispatch_ready(lambda _: None, limit=1)
            self.assertEqual(1, self.inbox.poll())
            self.assertIsNone(inbox_health(self.source.bot_path)["source_error"])
            self.assertEqual({"handler_returned": 1, "pending": 3}, self.counts())

    def test_partial_worker_start_failure_releases_ownership_after_threads_exit(self):
        start = threading.Thread.start
        calls = []
        def sometimes_start(thread):
            calls.append(thread)
            if len(calls) == 2:
                raise RuntimeError("synthetic thread failure")
            return start(thread)
        pool = InboxWorkers(self.inbox, Mock())
        self.pools.append(pool)
        with patch.object(threading.Thread, "start", sometimes_start):
            with self.assertRaisesRegex(RuntimeError, "synthetic thread failure"):
                pool.start()
        self.assertEqual(pool.stop(timeout=3), 0)
        self.consumer()


if __name__ == "__main__":
    unittest.main()
