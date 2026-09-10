import importlib.util
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from davosbot import config


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("runtime_smoke", ROOT / "scripts" / "runtime_smoke.py")
runtime_smoke = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = runtime_smoke
SPEC.loader.exec_module(runtime_smoke)


def _create_bot_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE reminders (
                id INTEGER PRIMARY KEY,
                message TEXT,
                due_ts TEXT,
                sent INTEGER DEFAULT 0,
                send_attempts INTEGER DEFAULT 0
            );
            CREATE TABLE scheduled_tasks (
                id INTEGER PRIMARY KEY,
                status TEXT
            );
            CREATE TABLE cron_jobs (
                id INTEGER PRIMARY KEY,
                enabled INTEGER DEFAULT 1,
                action_payload TEXT
            );
            CREATE TABLE change_log (
                id INTEGER PRIMARY KEY,
                request TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO reminders (message, due_ts, sent, send_attempts) VALUES (?, datetime('now', '+1 hour'), 0, 0)",
            ("future",),
        )
        conn.execute("INSERT INTO scheduled_tasks (status) VALUES ('pending')")
        conn.execute("INSERT INTO cron_jobs (enabled, action_payload) VALUES (1, ?)", ('{"ok": true}',))
        conn.commit()
    finally:
        conn.close()


class RuntimeSmokeTests(unittest.TestCase):
    def test_heartbeat_uses_latest_session_and_fails_for_stale_invalid_or_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heartbeat.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute("CREATE TABLE bot_sessions (id INTEGER PRIMARY KEY, last_heartbeat TEXT)")
                conn.execute("INSERT INTO bot_sessions VALUES (1, datetime('now'))")
                conn.commit()
                with patch.object(config, "BOT_DB_PATH", str(path)):
                    self.assertTrue(runtime_smoke.check_session_heartbeat().ok)
                    conn.execute("INSERT INTO bot_sessions VALUES (2, datetime('now', '-10 minutes'))")
                    conn.commit()
                    self.assertFalse(runtime_smoke.check_session_heartbeat().ok)
                    for value in (None, "invalid", "2999-01-01 00:00:00"):
                        conn.execute("UPDATE bot_sessions SET last_heartbeat=? WHERE id=2", (value,))
                        conn.commit()
                        self.assertFalse(runtime_smoke.check_session_heartbeat().ok)
            finally:
                conn.close()

    def test_smoke_connections_cannot_write_or_create_databases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "readonly.db"
            path.touch()
            conn = runtime_smoke._db_conn(str(path))
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("CREATE TABLE should_not_exist (id INTEGER)")
            finally:
                conn.close()
            missing = Path(tmp) / "missing.db"
            with self.assertRaises(sqlite3.OperationalError):
                runtime_smoke._db_conn(str(missing))
            self.assertFalse(missing.exists())

    def test_run_prepends_homebrew_paths_for_pm2_node(self):
        with patch.object(runtime_smoke.subprocess, "run") as run:
            run.return_value = runtime_smoke.subprocess.CompletedProcess(["pm2"], 0, "", "")
            runtime_smoke._run(["pm2", "jlist"])

        env_path = run.call_args.kwargs["env"]["PATH"]
        self.assertTrue(env_path.startswith("/opt/homebrew/bin:/usr/local/bin"))

    def test_resolve_executable_uses_fallback_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "pm2"
            candidate.write_text("#!/bin/sh\n", encoding="utf-8")
            with patch.object(runtime_smoke.shutil, "which", return_value=None):
                resolved = runtime_smoke._resolve_executable("pm2", (str(candidate),))

        self.assertEqual(str(candidate), resolved)

    def test_bot_db_check_passes_for_clean_runtime_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bot.db"
            _create_bot_db(db_path)
            with patch.object(config, "BOT_DB_PATH", str(db_path)):
                result = runtime_smoke.check_bot_db()

        self.assertTrue(result.ok)
        self.assertEqual(1, result.data["unsent_reminders"])
        self.assertEqual(0, result.data["overdue_reminders"])
        self.assertEqual([], result.data["malformed_crons"])

    def test_bot_db_check_fails_on_failed_schedule_and_bad_cron_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bot.db"
            _create_bot_db(db_path)
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("INSERT INTO scheduled_tasks (status) VALUES ('failed')")
                conn.execute("INSERT INTO cron_jobs (enabled, action_payload) VALUES (1, ?)", ("{bad",))
                conn.commit()
            finally:
                conn.close()
            with patch.object(config, "BOT_DB_PATH", str(db_path)):
                result = runtime_smoke.check_bot_db()

        self.assertFalse(result.ok)
        self.assertEqual(1, result.data["scheduled_failed"])
        self.assertEqual([2], result.data["malformed_crons"])

    def test_messages_db_check_uses_pm2_fallback_for_privacy_blocked_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "chat.db"
            db_path.write_bytes(b"")
            with (
                patch.object(config, "DB_PATH", str(db_path)),
                patch.object(
                    runtime_smoke,
                    "_db_conn",
                    side_effect=sqlite3.OperationalError("unable to open database file"),
                ),
                patch.object(runtime_smoke, "_recent_pm2_messages_db_errors", return_value=[]),
            ):
                result = runtime_smoke.check_messages_db()

        self.assertTrue(result.ok)
        self.assertEqual("pm2_logs", result.data["verified_via"])
        self.assertIn("macOS privacy", result.detail)

    def test_messages_db_check_fails_when_pm2_reports_chat_db_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "chat.db"
            db_path.write_bytes(b"")
            with (
                patch.object(config, "DB_PATH", str(db_path)),
                patch.object(
                    runtime_smoke,
                    "_db_conn",
                    side_effect=sqlite3.OperationalError("unable to open database file"),
                ),
                patch.object(
                    runtime_smoke,
                    "_recent_pm2_messages_db_errors",
                    return_value=["0|davosbot | poll_new_messages error: unable to open database file"],
                ),
            ):
                result = runtime_smoke.check_messages_db()

        self.assertFalse(result.ok)
        self.assertEqual(
            ["0|davosbot | poll_new_messages error: unable to open database file"],
            result.data["recent_errors"],
        )

    def test_messages_applescript_check_passes_on_macos_bridge_response(self):
        completed = runtime_smoke.subprocess.CompletedProcess(["osascript"], 0, "6\n", "")
        with patch.object(runtime_smoke.sys, "platform", "darwin"), patch.object(
            runtime_smoke, "_run", return_value=completed
        ) as run:
            result = runtime_smoke.check_messages_applescript()

        self.assertTrue(result.ok)
        self.assertIn("services=6", result.detail)
        self.assertEqual(["osascript"], run.call_args.args[0])

    def test_messages_applescript_check_uses_pm2_fallback_for_timeout_without_recent_errors(self):
        completed = runtime_smoke.subprocess.CompletedProcess(
            ["osascript"],
            1,
            "",
            'execution error: Messages got an error: AppleEvent timed out. (-1712)',
        )
        with (
            patch.object(runtime_smoke.sys, "platform", "darwin"),
            patch.object(runtime_smoke, "_run", return_value=completed),
            patch.object(runtime_smoke, "_recent_pm2_messages_applescript_errors", return_value=[]),
            patch.object(runtime_smoke.time, "sleep"),
        ):
            result = runtime_smoke.check_messages_applescript()

        self.assertTrue(result.ok)
        self.assertEqual("pm2_logs", result.data["verified_via"])

    def test_messages_applescript_check_retries_timeout_then_passes(self):
        timed_out = runtime_smoke.subprocess.CompletedProcess(
            ["osascript"],
            1,
            "",
            'execution error: Messages got an error: AppleEvent timed out. (-1712)',
        )
        success = runtime_smoke.subprocess.CompletedProcess(["osascript"], 0, "6\n", "")
        with (
            patch.object(runtime_smoke.sys, "platform", "darwin"),
            patch.object(runtime_smoke, "_run", side_effect=[timed_out, success]) as run,
            patch.object(runtime_smoke.time, "sleep") as sleep,
        ):
            result = runtime_smoke.check_messages_applescript()

        self.assertTrue(result.ok)
        self.assertIn("after 2 attempts", result.detail)
        self.assertEqual(2, run.call_count)
        sleep.assert_called_once_with(runtime_smoke.MESSAGES_APPLESCRIPT_RETRY_DELAY_SECONDS)

    def test_messages_applescript_check_fails_after_retry_budget_when_pm2_has_recent_errors(self):
        timed_out = runtime_smoke.subprocess.CompletedProcess(
            ["osascript"],
            1,
            "",
            'execution error: Messages got an error: AppleEvent timed out. (-1712)',
        )
        recent_errors = ["0|davosbot | AppleScript timed out for message send to +15550000001 (group=False) after 0.25s"]
        with (
            patch.object(runtime_smoke.sys, "platform", "darwin"),
            patch.object(runtime_smoke, "_run", side_effect=[timed_out, timed_out, timed_out]) as run,
            patch.object(runtime_smoke.time, "sleep") as sleep,
            patch.object(runtime_smoke, "_recent_pm2_messages_applescript_errors", return_value=recent_errors),
        ):
            result = runtime_smoke.check_messages_applescript()

        self.assertFalse(result.ok)
        self.assertEqual(3, run.call_count)
        self.assertEqual(2, sleep.call_count)
        self.assertEqual(recent_errors, result.data["recent_errors"])

    def test_format_results_is_high_signal(self):
        results = [
            runtime_smoke.CheckResult("git", True, "clean"),
            runtime_smoke.CheckResult("pm2", False, "missing worker"),
        ]

        text = runtime_smoke.format_results(results)

        self.assertIn("PASS git: clean", text)
        self.assertIn("FAIL pm2: missing worker", text)
        self.assertIn("Overall: FAIL (pm2)", text)

    def test_send_image_mode_uses_one_async_delivery_check(self):
        ok = runtime_smoke.CheckResult("ok", True, "ok")
        patches = (
            patch.object(runtime_smoke, "check_git", return_value=runtime_smoke.CheckResult("git", True, "ok")),
            patch.object(runtime_smoke, "check_pm2", return_value=runtime_smoke.CheckResult("pm2", True, "ok")),
            patch.object(runtime_smoke, "check_messages_db", return_value=runtime_smoke.CheckResult("messages_db", True, "ok")),
            patch.object(runtime_smoke, "check_messages_applescript", return_value=runtime_smoke.CheckResult("messages_applescript", True, "ok")),
            patch.object(runtime_smoke, "check_bot_db", return_value=runtime_smoke.CheckResult("bot_db", True, "ok")),
            patch.object(runtime_smoke, "check_image_routes", return_value=runtime_smoke.CheckResult("image_routes", True, "ok")),
            patch.object(runtime_smoke, "smoke_async_image_job", return_value=runtime_smoke.CheckResult("async_image_job", True, "ok")),
            patch.object(runtime_smoke, "smoke_send_image", return_value=ok),
            patch.object(runtime_smoke, "check_session_heartbeat", return_value=runtime_smoke.CheckResult("heartbeat", True, "ok")),
            patch.object(runtime_smoke, "check_inbox", return_value=runtime_smoke.CheckResult("inbox", True, "ok")),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7] as send_image, patches[8]:
            results = runtime_smoke.run_checks(types.SimpleNamespace(send_image=True))

        self.assertEqual(
            ["git", "pm2", "messages_db", "messages_applescript", "bot_db", "heartbeat", "inbox", "image_routes", "async_image_job"],
            [result.name for result in results],
        )
        send_image.assert_not_called()

    def test_runtime_smoke_image_is_deterministic_private_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch("davosbot.config.PROJECT_ROOT", str(root)),
                patch("davosbot.config.GENERATED_DIR", str(root / "generated")),
            ):
                image = runtime_smoke._runtime_smoke_image()
                size = image.stat().st_size

            self.assertEqual("davosbot_runtime_smoke.png", image.name)
            self.assertIn("runtime_smoke", str(image))
            self.assertGreater(size, 0)


if __name__ == "__main__":
    unittest.main()
