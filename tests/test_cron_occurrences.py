"""Real temporary SQLite claims/triggers, with no providers or live sends."""

from datetime import datetime, timedelta, timezone
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from davosbot import cron_occurrences as cron
from davosbot import db, permissions, tools


GROUP = "0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 9, 9, 13, 31, tzinfo=timezone.utc)


class CronOccurrenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "test.sqlite")
        self.sql("""CREATE TABLE cron_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            cron_expression TEXT, action_type TEXT, action_payload TEXT,
            enabled INTEGER DEFAULT 1, created_by TEXT, last_run TEXT)""")
        # Give simulated edits the same effective time as the simulated clock.
        with patch.object(db, "_CRON_SQL_NOW", "'2026-09-09 13:31:00.000'"):
            db.init_legacy_cron_schema(self.path, lambda sql, _description: self.sql(sql))
        self.now = NOW
        self.send = Mock(return_value=True)
        self.history = Mock()
        self.prepare = patch.object(cron, "_prepare", return_value=(GROUP, "Synthetic report."))
        self.prepare_mock = self.prepare.start()
        self.addCleanup(self.prepare.stop)

    def sql(self, sql, args=(), *, path=None):
        with closing(sqlite3.connect(path or self.path)) as conn:
            with conn:
                cursor = conn.execute(sql, args)
                return cursor.fetchall()

    def job(self, expression="06:30", *, action="morning_message", existing=True):
        self.sql("INSERT INTO cron_jobs(cron_expression,action_type,action_payload) VALUES (?,?,?)",
                 (expression, action, json.dumps({"recipient": GROUP})))
        job_id = self.sql("SELECT max(id) FROM cron_jobs")[0][0]
        if existing:
            self.sql("UPDATE cron_schedule_state SET effective_at=? WHERE job_id=?",
                     ("2026-01-01 00:00:00", job_id))
        return job_id

    def run_job(self, job_id):
        return cron.run_due(self.path, job_id, self.now, self.send, self.history,
                            owner_id="+15550000001", clock=lambda: self.now)

    def status(self):
        return self.sql("SELECT status,attempts FROM cron_occurrences ORDER BY rowid")

    def test_daily_and_weekly_missed_minute_catch_up_once_across_restart(self):
        for expression in ("06:30", "06:30 wed"):
            with self.subTest(expression=expression):
                job_id = self.job(expression)
                self.now = NOW - timedelta(seconds=70)
                self.assertFalse(self.run_job(job_id))
                self.now = NOW
                self.assertTrue(self.run_job(job_id))
                self.assertFalse(self.run_job(job_id))
                db.init_legacy_cron_schema(self.path, lambda sql, _description: self.sql(sql))
                self.assertFalse(self.run_job(job_id))
                self.assertEqual(1, self.sql("SELECT count(*) FROM cron_occurrences WHERE job_id=?", (job_id,))[0][0])
        self.assertEqual(2, self.send.call_count)
        self.assertEqual(2, self.history.call_count)
        self.assertEqual("none", self.send.call_args.kwargs["recovery_mode"])

    def test_new_or_edited_past_schedule_does_not_fire_retroactively(self):
        self.assertFalse(self.run_job(self.job(existing=False)))
        edited = self.job("06:40")
        self.sql("UPDATE cron_jobs SET cron_expression='06:30' WHERE id=?", (edited,))
        self.assertFalse(self.run_job(edited))
        self.send.assert_not_called()
        self.now = NOW + timedelta(days=1)
        self.assertTrue(self.run_job(edited))

    def test_trigger_edits_are_atomic_noops_preserve_boundary_and_reenable_advances(self):
        job_id = self.job()
        initial = self.sql("SELECT * FROM cron_schedule_state")[0]
        self.sql("UPDATE cron_jobs SET cron_expression=cron_expression,action_payload=action_payload,enabled=enabled WHERE id=?", (job_id,))
        self.sql("UPDATE cron_jobs SET last_run='2020-01-01 00:00:00' WHERE id=?", (job_id,))
        self.assertEqual(initial, self.sql("SELECT * FROM cron_schedule_state")[0])
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("UPDATE cron_jobs SET enabled=0 WHERE id=?", (job_id,))
            conn.rollback()
        finally:
            conn.close()
        self.assertEqual(initial, self.sql("SELECT * FROM cron_schedule_state")[0])
        self.sql("UPDATE cron_jobs SET enabled=0 WHERE id=?", (job_id,))
        self.sql("UPDATE cron_jobs SET enabled=1 WHERE id=?", (job_id,))
        self.assertEqual(initial[1] + 2, self.sql("SELECT revision FROM cron_schedule_state")[0][0])
        self.assertFalse(self.run_job(job_id))

    def test_payload_destination_and_action_changes_advance_effective_boundary(self):
        job_id = self.job()
        for column, value in (("action_payload", '{"recipient":"+15550000003"}'), ("action_type", "sports_recap")):
            previous = self.sql("SELECT revision FROM cron_schedule_state")[0][0]
            self.sql(f"UPDATE cron_jobs SET {column}=? WHERE id=?", (value, job_id))
            self.assertEqual(previous + 1, self.sql("SELECT revision FROM cron_schedule_state")[0][0])
            self.assertFalse(self.run_job(job_id))

    def test_research_claim_payload_updates_are_outside_legacy_state(self):
        job_id = self.job(action="research_report")
        self.sql("UPDATE cron_jobs SET action_payload=? WHERE id=?", ('{"run":{"status":"sending"}}', job_id))
        self.assertEqual([], self.sql("SELECT * FROM cron_schedule_state"))
        self.assertFalse(self.run_job(job_id))
        self.send.assert_not_called()

    def test_native_edit_commits_new_effective_boundary_with_existing_permission_gate(self):
        job_id = self.job("06:40")
        with (
            patch.object(tools, "BOT_DB_PATH", self.path),
            patch.object(permissions, "is_owner", return_value=True),
            patch.object(tools, "_cron_recipient_label", return_value="synthetic group"),
        ):
            reply = tools._edit_cron(job_id, sender="+15550000001", time_pt="06:30")
        self.assertIn("Updated cron", reply)
        self.assertEqual((2, "2026-09-09 13:31:00.000"), self.sql("SELECT revision,effective_at FROM cron_schedule_state")[0])
        self.assertFalse(self.run_job(job_id))

    def test_normal_migration_backs_up_bootstraps_once_and_keeps_legacy_rows_unchanged(self):
        path = str(Path(self.path).with_name("migration.sqlite"))
        definition = self.sql("SELECT sql FROM sqlite_master WHERE name='cron_jobs'")[0][0]
        self.sql(definition, path=path)
        self.sql("INSERT INTO cron_jobs(cron_expression,action_type,action_payload) VALUES ('06:30','morning_message','{}')", path=path)
        original = self.sql("SELECT * FROM cron_jobs", path=path)
        with (
            patch.object(db, "BOT_DB_PATH", path),
            patch.object(db, "_DB_PATH", Path(path)),
            patch.object(db, "_BACKUPS_DIR", Path(path).parent / "synthetic-backups"),
            patch.object(db, "backup_database", wraps=db.backup_database) as backup,
            patch.object(db, "_CRON_SQL_NOW", "'2026-09-09 13:31:00.000'"),
        ):
            db.init_legacy_cron_schema(path, db.run_migration)
            self.assertGreater(backup.call_count, 0)
            backup.reset_mock()
            state = self.sql("SELECT * FROM cron_schedule_state", path=path)
            db.init_legacy_cron_schema(path, db.run_migration)
            backup.assert_not_called()
        self.assertEqual(original, self.sql("SELECT * FROM cron_jobs", path=path))
        self.assertEqual(state, self.sql("SELECT * FROM cron_schedule_state", path=path))
        self.assertIsNone(cron.claim_due(path, 1, NOW))

    def test_edit_cancel_or_delete_during_preparation_prevents_send(self):
        for change in ("UPDATE cron_jobs SET enabled=0 WHERE id=?", "DELETE FROM cron_jobs WHERE id=?",
                       "UPDATE cron_jobs SET action_payload='{}' WHERE id=?"):
            with self.subTest(change=change):
                job_id = self.job()
                def prepare(*args):
                    self.sql(change, (job_id,))
                    return GROUP, "Synthetic report."
                self.prepare_mock.side_effect = prepare
                self.assertFalse(self.run_job(job_id))
        self.send.assert_not_called()
        self.history.assert_not_called()

    def test_preparation_failure_retries_after_delay_with_attempt_limit(self):
        job_id = self.job()
        self.prepare_mock.side_effect = RuntimeError("synthetic provider failure")
        for attempt in range(1, 4):
            self.assertFalse(self.run_job(job_id))
            self.assertEqual([("preparation_failed", attempt)], self.status())
            self.assertFalse(self.run_job(job_id))
            self.now += timedelta(seconds=61)
        self.assertFalse(self.run_job(job_id))
        self.assertEqual(3, self.prepare_mock.call_count)
        self.send.assert_not_called()

    def test_abandoned_preparation_can_be_reclaimed_but_old_claim_cannot_send(self):
        job_id = self.job()
        old = cron.claim_due(self.path, job_id, self.now)
        self.assertIsNone(cron.claim_due(self.path, job_id, self.now))
        self.now += cron.PREPARATION_LEASE
        replacement = cron.claim_due(self.path, job_id, self.now)
        self.assertNotEqual(old["token"], replacement["token"])
        self.assertFalse(cron._transition(self.path, old, "sending", self.now,
                                        expected_status="preparing", verify_job=True))

    def test_false_none_or_raised_sends_are_uncertain_and_never_retried(self):
        for result in (False, None, RuntimeError("synthetic ambiguous send")):
            with self.subTest(result=type(result).__name__):
                job_id = self.job()
                self.send.reset_mock()
                self.send.side_effect = result if isinstance(result, Exception) else None
                self.send.return_value = result
                self.assertFalse(self.run_job(job_id))
                self.assertFalse(self.run_job(job_id))
                self.send.assert_called_once()
                self.assertEqual("uncertain", self.status()[-1][0])
        self.history.assert_not_called()

    def test_crash_after_send_boundary_is_not_reclaimed(self):
        job_id = self.job()
        claim = cron.claim_due(self.path, job_id, self.now)
        self.assertTrue(cron._transition(self.path, claim, "sending", self.now,
                                       expected_status="preparing", verify_job=True))
        self.now += timedelta(minutes=5)
        self.assertFalse(self.run_job(job_id))
        self.send.assert_not_called()

    def test_post_send_checkpoint_failure_rolls_back_and_never_repeats_or_saves_history(self):
        job_id = self.job()
        self.sql("""CREATE TRIGGER reject_checkpoint BEFORE UPDATE OF last_run ON cron_jobs
            BEGIN SELECT RAISE(ABORT,'synthetic checkpoint failure'); END""")
        self.assertFalse(self.run_job(job_id))
        self.assertEqual([("uncertain", 1)], self.status())
        self.assertIsNone(self.sql("SELECT last_run FROM cron_jobs")[0][0])
        self.sql("DROP TRIGGER reject_checkpoint")
        self.assertFalse(self.run_job(job_id))
        self.send.assert_called_once()
        self.history.assert_not_called()

    def test_history_failure_cannot_reopen_completed_occurrence(self):
        job_id = self.job()
        self.history.side_effect = RuntimeError("synthetic history failure")
        self.assertTrue(self.run_job(job_id))
        self.assertFalse(self.run_job(job_id))
        self.assertEqual([("completed", 1)], self.status())
        self.send.assert_called_once()

    def test_legacy_last_run_survives_rollback_without_duplicate(self):
        job_id = self.job()
        self.sql("UPDATE cron_jobs SET last_run=? WHERE id=?", (cron._stamp(NOW), job_id))
        self.assertFalse(self.run_job(job_id))
        self.assertEqual([], self.status())

    def test_grace_weekly_rollover_dst_and_clock_boundaries(self):
        effective = "2025-01-01 00:00:00"
        due = cron.due_occurrence("06:30 wed", NOW, effective)
        self.assertIsNotNone(due)
        self.assertIsNone(cron.due_occurrence("06:30 wed", NOW + timedelta(minutes=15), effective))
        self.assertIsNone(cron.due_occurrence("06:30", NOW - timedelta(minutes=2), effective))
        midnight = datetime(2026, 9, 12, 7, 5, tzinfo=timezone.utc)
        self.assertEqual("2026-09-11 23:59", cron.due_occurrence("23:59 fri", midnight, effective)[0])
        self.assertIsNotNone(cron.due_occurrence("01:30 sun", datetime(2026, 11, 1, 8, 35, tzinfo=timezone.utc), effective))
        self.assertIsNone(cron.due_occurrence("01:30 sun", datetime(2026, 11, 1, 9, 35, tzinfo=timezone.utc), effective))
        self.assertIsNone(cron.due_occurrence("02:30 sun", datetime(2026, 3, 8, 10, 35, tzinfo=timezone.utc), effective))


if __name__ == "__main__":
    unittest.main()
