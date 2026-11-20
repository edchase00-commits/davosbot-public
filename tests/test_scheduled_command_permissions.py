import ast
import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


from davosbot.runtime_locks import schedule_locked

ROOT = Path(__file__).resolve().parents[1]


def _load_commands(db_path):
    """Run actual scheduled command bodies with an isolated SQLite database."""
    tree = ast.parse((ROOT / "davosbot" / "commands.py").read_text(encoding="utf-8"))
    module = ast.Module(body=[
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"register_cron", "register_morning_message", "_cmd_cancel"}
    ], type_ignores=[])
    namespace = {
        "schedule_locked": schedule_locked,
        "__package__": "scheduled_command_test",
        "re": re, "sqlite3": sqlite3, "closing": closing, "BOT_DB_PATH": str(db_path),
        "check_action_permission": Mock(side_effect=lambda sender, action: (
            None if sender == "owner" else "Owner access required."
        )),
    }
    exec(compile(module, str(ROOT / "davosbot" / "commands.py"), "exec"), namespace)
    return namespace


class _FailingCommitConnection(sqlite3.Connection):
    def commit(self):
        raise sqlite3.OperationalError("synthetic commit failure")


class ScheduledCommandPermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "scheduled.sqlite"
        self.helpers = _load_commands(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript("""
                CREATE TABLE cron_jobs (
                    id INTEGER PRIMARY KEY, cron_expression TEXT, action_type TEXT,
                    action_payload TEXT, enabled INTEGER, created_by TEXT
                );
                CREATE TABLE scheduled_tasks (
                    id INTEGER PRIMARY KEY, recipient TEXT, message TEXT, scheduled_at TEXT,
                    status TEXT, chat_id TEXT, sender TEXT
                );
                INSERT INTO scheduled_tasks VALUES
                    (1, 'recipient-a', 'first', '2030-01-01 09:00:00', 'pending', NULL, 'owner'),
                    (2, 'recipient-b', 'finished', '2030-01-01 10:00:00', 'done', NULL, 'owner'),
                    (3, 'recipient-c', 'group', '2030-01-01 11:00:00', 'pending', 'group-id', 'owner');
            """)

    def rows(self, query):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(query).fetchall()

    def morning_message(self, sender, recipient, time):
        config = SimpleNamespace(normalize_handle=lambda value: value)
        with patch.dict("sys.modules", {"scheduled_command_test.config": config}):
            return self.helpers["register_morning_message"](sender, recipient, time)

    def test_owner_registers_cron_durably_without_changing_payload(self):
        payload = {"recipient": "group-id", "intro": "Morning", "extra": {"value": 1}}

        reply = self.helpers["register_cron"]("owner", " 08:15 ", "morning_message", payload)

        self.assertIn("registered", reply)
        self.helpers["check_action_permission"].assert_called_once_with("owner", "schedule_cron")
        expr, action, saved_payload, enabled, creator = self.rows(
            "SELECT cron_expression, action_type, action_payload, enabled, created_by FROM cron_jobs",
        )[0]
        self.assertEqual(("08:15", "morning_message", 1, "owner"), (expr, action, enabled, creator))
        self.assertEqual(payload, json.loads(saved_payload))

    def test_owner_cancels_only_requested_pending_task_durably(self):
        before = self.rows("SELECT * FROM scheduled_tasks ORDER BY id")

        self.assertEqual("Cancelled #1.", self.helpers["_cmd_cancel"]("cancel 1", "owner"))

        self.helpers["check_action_permission"].assert_called_once_with("owner", "view_session")
        after = self.rows("SELECT * FROM scheduled_tasks ORDER BY id")
        expected_first = list(before[0])
        expected_first[4] = "cancelled"
        self.assertEqual([tuple(expected_first), *before[1:]], after)

    def test_invalid_clock_values_do_not_persist_even_with_optional_parser(self):
        validator = Mock(return_value=True)
        optional_parser = SimpleNamespace(croniter=SimpleNamespace(is_valid=validator))
        for expression in ("24:00", "99:99", "08:60", "23:99"):
            with self.subTest(expression=expression), patch.dict("sys.modules", {"croniter": optional_parser}):
                reply = self.helpers["register_cron"]("owner", expression, "morning_message", {})
                self.assertIn("Couldn't parse", reply)
                self.assertEqual([], self.rows("SELECT * FROM cron_jobs"))
        validator.assert_not_called()

    def test_optional_cron_parser_must_validate_expression(self):
        validator = Mock(side_effect=[False, True])
        optional_parser = SimpleNamespace(croniter=SimpleNamespace(is_valid=validator))
        with patch.dict("sys.modules", {"croniter": optional_parser}):
            rejected = self.helpers["register_cron"]("owner", "not a schedule", "morning_message", {})
            accepted = self.helpers["register_cron"]("owner", "0 8 * * *", "morning_message", {})

        self.assertIn("Couldn't parse", rejected)
        self.assertIn("registered", accepted)
        self.assertEqual([("0 8 * * *",)], self.rows("SELECT cron_expression FROM cron_jobs"))
        self.assertEqual([("not a schedule",), ("0 8 * * *",)], [call.args for call in validator.call_args_list])

    def test_missing_or_failing_optional_parser_cannot_authorize_schedule(self):
        for optional_parser in (None, SimpleNamespace(croniter=SimpleNamespace(is_valid=Mock(side_effect=ValueError)))):
            with self.subTest(parser=optional_parser), patch.dict("sys.modules", {"croniter": optional_parser}):
                reply = self.helpers["register_cron"]("owner", "0 8 * * *", "morning_message", {})
                self.assertIn("Couldn't parse", reply)
                self.assertEqual([], self.rows("SELECT * FROM cron_jobs"))

    def test_valid_clock_boundaries_and_morning_ampm_conversion(self):
        for time, expected in (("12am", "00:00"), ("12pm", "12:00"), ("8:15pm", "20:15"), ("00:00", "00:00"), ("23:59", "23:59")):
            with self.subTest(time=time):
                self.assertIn("registered", self.morning_message("owner", "recipient-a", time))
                expr, payload = self.rows("SELECT cron_expression, action_payload FROM cron_jobs ORDER BY id DESC LIMIT 1")[0]
                self.assertEqual(expected, expr)
                self.assertEqual({"recipient": "recipient-a"}, json.loads(payload))

    def test_invalid_morning_times_are_rejected_before_inserting(self):
        for time in ("0am", "00:30pm", "13pm", "24:00", "9:60am", "99", "noonish"):
            with self.subTest(time=time):
                self.assertIn("Couldn't parse", self.morning_message("owner", "recipient-a", time))
                self.assertEqual([], self.rows("SELECT * FROM cron_jobs"))

    def test_morning_helper_keeps_owner_gate(self):
        for sender in ("admin", "friend", "unknown"):
            with self.subTest(sender=sender):
                self.assertEqual("Owner access required.", self.morning_message(sender, "recipient-a", "8am"))
                self.assertEqual([], self.rows("SELECT * FROM cron_jobs"))

    def test_admin_friend_and_unknown_cannot_register_or_cancel(self):
        before = self.rows("SELECT * FROM scheduled_tasks ORDER BY id")
        for sender in ("admin", "friend", "unknown"):
            with self.subTest(sender=sender):
                self.helpers["check_action_permission"].reset_mock()
                self.assertEqual("Owner access required.", self.helpers["register_cron"](
                    sender, "08:00", "morning_message", {"recipient": sender},
                ))
                self.helpers["check_action_permission"].assert_called_once_with(sender, "schedule_cron")
                self.helpers["check_action_permission"].reset_mock()
                self.assertEqual("Owner access required.", self.helpers["_cmd_cancel"]("cancel 1", sender))
                self.helpers["check_action_permission"].assert_called_once_with(sender, "view_session")
                self.assertEqual([], self.rows("SELECT * FROM cron_jobs"))
                self.assertEqual(before, self.rows("SELECT * FROM scheduled_tasks ORDER BY id"))

    def test_cancel_finished_missing_or_invalid_id_leaves_all_rows_unchanged(self):
        before = self.rows("SELECT * FROM scheduled_tasks ORDER BY id")
        for command in ("cancel 2", "cancel 99", "cancel invalid", "cancel"):
            with self.subTest(command=command):
                self.assertNotIn("Cancelled", self.helpers["_cmd_cancel"](command, "owner"))
                self.assertEqual(before, self.rows("SELECT * FROM scheduled_tasks ORDER BY id"))

    def test_commit_failure_rolls_back_registration_and_cancellation(self):
        before = self.rows("SELECT * FROM scheduled_tasks ORDER BY id")
        failing_sqlite = SimpleNamespace(
            connect=lambda path: sqlite3.connect(path, factory=_FailingCommitConnection),
        )
        with patch.dict(self.helpers, {"sqlite3": failing_sqlite}):
            register_reply = self.helpers["register_cron"](
                "owner", "08:00", "morning_message", {"recipient": "owner"},
            )
            cancel_reply = self.helpers["_cmd_cancel"]("cancel 1", "owner")

        self.assertIn("Couldn't register cron", register_reply)
        self.assertIn("cancel failed", cancel_reply)
        self.assertEqual([], self.rows("SELECT * FROM cron_jobs"))
        self.assertEqual(before, self.rows("SELECT * FROM scheduled_tasks ORDER BY id"))


if __name__ == "__main__":
    unittest.main()
