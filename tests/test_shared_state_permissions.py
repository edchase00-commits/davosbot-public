"""Real thread boundaries with synthetic files/DBs and mocked outbound work."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from davosbot import billing, brain, group_chat, main, memory, personality, soul, tools
from davosbot.runtime_locks import PERSONALITY_FILE_LOCK, SCHEDULE_LOCK
from test_scheduled_command_permissions import _load_commands
from test_scheduler_retry import _load_scheduler_helpers
import test_work_actions as work_fixture


class SharedStatePermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def background(self, function):
        started, done = threading.Event(), threading.Event()
        result, errors = [], []

        def run():
            started.set()
            try:
                result.append(function())
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(started.wait(2))
        return thread, done, result, errors

    def finished(self, job):
        thread, done, result, errors = job
        self.assertTrue(done.wait(3), "thread did not finish after release")
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        return result[0]

    def test_group_read_cannot_replace_state_during_other_chat_save(self):
        entered, release = threading.Event(), threading.Event()
        original_save = group_chat._save

        def pause_save():
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release writer")
            original_save()

        with (patch.object(group_chat, "_STATE_FILE", self.root / "group.json"),
              patch.object(group_chat, "_BACKUPS_DIR", self.root / "backups"),
              patch.object(group_chat, "_state", group_chat._fresh_state())):
            group_chat.enable_gc("existing")
            with patch.object(group_chat, "_save", pause_save):
                writer = self.background(lambda: group_chat.enable_gc("new-chat"))
                self.assertTrue(entered.wait(2))
                reader = self.background(group_chat.get_state_snapshot)
                other = self.background(lambda: group_chat.approve_user("+15550000002"))
                try:
                    self.assertFalse(reader[1].wait(.1))
                    self.assertFalse(other[1].wait(.1))
                finally:
                    release.set()
                self.finished(writer)
                self.finished(reader)
                self.finished(other)
            state = json.loads((self.root / "group.json").read_text())
            self.assertEqual(state["enabled_chats"], ["existing", "new-chat"])
            self.assertEqual(state["approved_users"], ["+15550000002"])

    def test_group_snapshots_are_detached_and_editor_gate_is_unchanged(self):
        with (patch.object(group_chat, "_STATE_FILE", self.root / "group.json"),
              patch.object(group_chat, "_BACKUPS_DIR", self.root / "backups"),
              patch.object(group_chat, "_state", group_chat._fresh_state())):
            group_chat.create_group_persona("chat", "Coach", "Use concise sporting metaphors.", "owner")
            group_chat.set_persona("chat", "gc:chat:coach")
            for snapshot in (group_chat.get_group_persona("chat", "coach"),
                             group_chat.list_group_personas("chat")[0]):
                snapshot["editors"].append("intruder")
            group_chat.get_state_snapshot()["approved_users"].append("intruder")
            self.assertFalse(group_chat.is_group_persona_editor("chat", "intruder"))
            with self.assertRaises(PermissionError):
                group_chat.append_group_persona_note("chat", "intruder", "Be more concise.")
            self.assertEqual(group_chat.get_group_persona("chat", "coach")["editors"], [])

    def test_soul_writers_keep_unique_backups_monotonic_versions_and_owner_gate(self):
        path = self.root / "identity.md"
        path.write_text("Synthetic identity.\n")
        with (patch.object(soul, "_SOUL_PATH", path),
              patch.object(soul, "_BACKUPS_DIR", self.root / "backups"),
              patch.object(soul, "is_owner", lambda sender: sender == "owner"),
              patch.object(soul, "_log_soul_write")):
            with self.assertRaises(PermissionError):
                soul.write_soul("forbidden", "test", "friend")
            self.assertEqual(path.read_text(), "Synthetic identity.\n")
            with ThreadPoolExecutor(max_workers=3) as pool:
                paths = list(pool.map(lambda n: soul.write_soul(f"Synthetic version {n}.", "test", "owner"), range(12)))
            self.assertEqual(len(set(paths)), 12)
            self.assertIn("<!-- v12 |", soul.read_soul())
            backups = [Path(name).read_text() for name in paths]
            self.assertTrue(any(text == "Synthetic identity.\n" for text in backups))
            for n in range(1, 12):
                self.assertTrue(any(f"<!-- v{n} |" in text for text in backups))

    def test_prompt_read_waits_for_file_io_but_model_rewrite_does_not_hold_lock(self):
        path = self.root / "personalities" / "coach.md"
        path.parent.mkdir()
        path.write_text("old synthetic voice")
        with patch.object(personality, "SOUL_PATH", str(path)):
            with PERSONALITY_FILE_LOCK:
                read = self.background(personality.load_soul)
                self.assertFalse(read[1].wait(.1))
                path.write_text("complete synthetic voice")
            self.assertEqual(self.finished(read), "complete synthetic voice")

        entered, release = threading.Event(), threading.Event()
        def rewrite(_):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release model")
            return "updated synthetic voice"
        with patch.object(tools, "_PROJECT_DIR", str(self.root)), patch.object(tools, "_gemini_rewrite", rewrite):
            edit = self.background(lambda: tools._edit_persona("coach", "shorter"))
            self.assertTrue(entered.wait(2))
            read = self.background(lambda: personality._read_nonempty_text(path))
            try:
                self.assertEqual(self.finished(read), "complete synthetic voice")
            finally:
                release.set()
            self.finished(edit)
            self.assertEqual(path.read_text(), "updated synthetic voice")

    def scheduled_db(self):
        path = self.root / "scheduled.sqlite"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE scheduled_tasks(id INTEGER PRIMARY KEY, task_type TEXT, recipient TEXT, "
                         "message TEXT, scheduled_at TEXT, status TEXT, error TEXT, chat_id TEXT, sent_at TEXT)")
            conn.execute("INSERT INTO scheduled_tasks(id,task_type,recipient,message,scheduled_at,status) "
                         "VALUES(1,'send_imessage','owner','synthetic',datetime('now','-1 minute'),'pending')")
        return path

    def test_model_persona_save_preserves_intervening_file_update(self):
        path = self.root / "personalities" / "coach.md"
        path.parent.mkdir()
        for operation in (lambda: tools._edit_persona("coach", "shorter"),
                          lambda: tools._create_persona("coach", "concise coach")):
            path.write_text("original synthetic voice")
            entered, release = threading.Event(), threading.Event()
            def rewrite(_):
                entered.set()
                if not release.wait(3):
                    raise AssertionError("test did not release rewrite")
                return "stale model rewrite"
            with patch.object(tools, "_PROJECT_DIR", str(self.root)), patch.object(tools, "_gemini_rewrite", rewrite):
                job = self.background(operation)
                self.assertTrue(entered.wait(2))
                try:
                    with PERSONALITY_FILE_LOCK:
                        path.write_text("newer legitimate update")
                finally:
                    release.set()
                self.assertIn("kept the newer file", self.finished(job))
                self.assertEqual(path.read_text(), "newer legitimate update")

    def test_cancel_before_timer_selection_prevents_send_and_nonowner_stays_denied(self):
        path = self.scheduled_db()
        commands = _load_commands(path)
        sender = Mock(return_value=True)
        helpers = _load_scheduler_helpers(sender)
        helpers["BOT_DB_PATH"] = str(path)
        self.assertEqual(commands["_cmd_cancel"]("cancel 1", "friend"), "Owner access required.")
        self.assertEqual(commands["_cmd_cancel"]("cancel 1", "owner"), "Cancelled #1.")
        helpers["_check_scheduled_tasks"]()
        sender.assert_not_called()

    def test_unrelated_text_rejects_cron_routes_while_a_timer_holds_schedule_lock(self):
        routes = (tools._schedule_cron_from_text, tools._edit_cron_from_text,
                  tools._cancel_cron_from_text, tools._sports_recap_cron_from_text)
        with SCHEDULE_LOCK:
            jobs = [self.background(lambda route=route: route("friend", "hello", "chat")) for route in routes]
            for job in jobs:
                self.assertIsNone(self.finished(job))

    def test_sports_preview_releases_schedule_lock_after_creation_commits(self):
        fixture = work_fixture.WorkActionsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute("CREATE TABLE bot_log(sender TEXT,event_type TEXT,payload TEXT)")
        entered, release = threading.Event(), threading.Event()
        def preview():
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release preview")
            return "synthetic scoreboard"
        with patch.object(tools, "BOT_DB_PATH", fixture.db), patch.object(tools, "_get_sports_recap", side_effect=preview):
            create = self.background(lambda: tools._sports_recap_cron_from_text(
                work_fixture.OWNER, "create sports recap cron daily at 6pm", work_fixture.OWNER))
            self.assertTrue(entered.wait(2))
            try:
                with closing(sqlite3.connect(fixture.db)) as conn:
                    rid = conn.execute("SELECT id FROM cron_jobs").fetchone()[0]
                cancel = self.background(lambda: tools._cancel_cron_by_id(rid, sender=work_fixture.OWNER))
                self.assertIn("Disabled cron", self.finished(cancel))
            finally:
                release.set()
            self.assertIn("synthetic scoreboard", self.finished(create))

    def test_cancel_waits_for_running_send_then_reports_already_done(self):
        path = self.scheduled_db()
        commands = _load_commands(path)
        entered, release = threading.Event(), threading.Event()
        def send(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release send")
            return True
        helpers = _load_scheduler_helpers(send)
        helpers["BOT_DB_PATH"] = str(path)
        timer = self.background(helpers["_check_scheduled_tasks"])
        self.assertTrue(entered.wait(2))
        cancel = self.background(lambda: commands["_cmd_cancel"]("cancel 1", "owner"))
        try:
            self.assertFalse(cancel[1].wait(.1))
        finally:
            release.set()
        self.finished(timer)
        self.assertEqual(self.finished(cancel), "#1 not found or already done.")

    def test_work_snapshot_rechecks_after_waiting_for_native_schedule_mutation(self):
        fixture = work_fixture.WorkActionsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        rid = fixture.reminder()
        snapshot = fixture.snapshot("reminders")
        with SCHEDULE_LOCK:
            job = self.background(lambda: fixture.execute("reminders.cancel", {"id": rid, "snapshot": snapshot}))
            self.assertFalse(job[1].wait(.1))
            with closing(sqlite3.connect(fixture.db)) as conn, conn:
                conn.execute("UPDATE reminders SET message='changed natively' WHERE id=?", (rid,))
        result = self.finished(job)
        self.assertEqual(result["evidence"]["code"], "stale_snapshot")
        with closing(sqlite3.connect(fixture.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM reminders").fetchone()[0], 1)

    def test_work_cancel_waits_for_native_reminder_and_rejects_outdated_snapshot(self):
        fixture = work_fixture.WorkActionsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        rid = fixture.reminder(due="2020-01-01 00:00:00")
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute("ALTER TABLE reminders ADD COLUMN last_attempt_ts TEXT")
        snapshot = fixture.snapshot("reminders")
        entered, release = threading.Event(), threading.Event()
        def send(*args, **kwargs):
            self.assertEqual(args[0], work_fixture.OWNER)
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release reminder")
            return True
        with patch.object(memory, "BOT_DB_PATH", fixture.db), patch.object(main, "send_message", side_effect=send):
            timer = self.background(main._check_reminders)
            self.assertTrue(entered.wait(2))
            cancel = self.background(lambda: fixture.execute("reminders.cancel", {"id": rid, "snapshot": snapshot}))
            try:
                self.assertFalse(cancel[1].wait(.1))
            finally:
                release.set()
            self.finished(timer)
            self.assertEqual(self.finished(cancel)["evidence"]["code"], "stale_snapshot")
        with closing(sqlite3.connect(fixture.db)) as conn:
            self.assertEqual(conn.execute("SELECT sent FROM reminders WHERE id=?", (rid,)).fetchone()[0], 1)

    def test_successful_health_probe_cannot_erase_newer_handler_failure(self):
        entered, release = threading.Event(), threading.Event()
        def probe():
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release probe")
            return True
        with (patch.object(brain, "_ollama_down", True),
              patch.object(brain, "_last_ollama_check", 0),
              patch.object(brain, "_ollama_down_alerted", False),
              patch.object(brain, "_ollama_state_epoch", 0),
              patch.object(brain, "_ollama_health_check", side_effect=probe),
              patch.object(brain, "_log_ollama_state") as log,
              patch.object(brain, "_notify_owner") as notify):
            health = self.background(lambda: brain.check_ollama_recovery(now=10000))
            self.assertTrue(entered.wait(2))
            try:
                brain._mark_ollama_down()
            finally:
                release.set()
            self.assertFalse(self.finished(health))
            self.assertTrue(brain._ollama_down)
            log.assert_not_called()
            notify.assert_not_called()

    def test_concurrent_usage_inserts_remain_committed_with_separate_connections(self):
        path = self.root / "usage.sqlite"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE gemini_usage(prompt_tokens INTEGER,candidates_tokens INTEGER,"
                         "total_tokens INTEGER,source TEXT)")
        with patch.object(billing, "BOT_DB_PATH", str(path)):
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda n: billing.log_gemini_usage(10, 2, 12, "synthetic"), range(24)))
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*),SUM(total_tokens) FROM gemini_usage").fetchone(), (24, 288))

    def test_budget_alert_cooldown_is_atomic_without_serializing_requests(self):
        entered, release = threading.Event(), threading.Event()
        def alert(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release alert")
        with (patch.object(billing, "_LAST_GEMINI_BUDGET_ALERT_AT", 0),
              patch.object(billing.time, "time", return_value=10000),
              patch("davosbot.alerts.send_owner_alert", side_effect=alert) as notify):
            first = self.background(lambda: billing._maybe_send_gemini_budget_alert(billing.GeminiUsageSummary(), "test", "test"))
            self.assertTrue(entered.wait(2))
            second = self.background(lambda: billing._maybe_send_gemini_budget_alert(billing.GeminiUsageSummary(), "test", "test"))
            try:
                self.finished(second)
                self.assertEqual(notify.call_count, 1)
            finally:
                release.set()
            self.finished(first)


if __name__ == "__main__":
    unittest.main()
