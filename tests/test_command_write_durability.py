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


ROOT = Path(__file__).resolve().parents[1]


def _load_commands(db_path):
    """Load the actual command bodies without touching runtime config or data."""
    tree = ast.parse((ROOT / "davosbot" / "commands.py").read_text(encoding="utf-8"))
    names = {
        "_log_group_persona_event", "create_skill", "_cmd_skill_manage",
        "_parse_bet_input", "_calc_payout", "_get_unit_size", "_cmd_bet_log",
        "_cmd_bet_settle", "_cmd_bets", "_parse_workout_input", "_guess_muscle_group",
        "_cmd_workout_log",
    }
    nodes = [
        node for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in names)
        or (isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_MUSCLE_MAP" for target in node.targets
        ))
    ]
    namespace = {
        "re": re, "sqlite3": sqlite3, "closing": closing, "BOT_DB_PATH": str(db_path),
        "normalize_handle": lambda value: value,
        "is_admin": lambda sender: sender in {"owner", "admin"},
        "check_action_permission": lambda sender, action: (
            None if sender in {"owner", "admin"} else "Admin access required."
        ),
        "logger": Mock(),
    }
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, str(ROOT / "davosbot" / "commands.py"), "exec"), namespace)
    return namespace


class _FailingCommitConnection(sqlite3.Connection):
    def commit(self):
        raise sqlite3.OperationalError("synthetic commit failure")


class CommandWriteDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "commands.sqlite"
        self.helpers = _load_commands(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript("""
                CREATE TABLE skills (
                    id INTEGER PRIMARY KEY, created_by TEXT, skill_name TEXT UNIQUE,
                    trigger_phrase TEXT, response_template TEXT, enabled INTEGER DEFAULT 1
                );
                CREATE TABLE sports_bets (
                    id INTEGER PRIMARY KEY, sender TEXT, event TEXT, bet_type TEXT,
                    odds INTEGER, stake REAL, unit_size REAL, notes TEXT,
                    result TEXT DEFAULT 'pending', payout REAL, settled_at TEXT
                );
                CREATE TABLE bet_config (sender TEXT, key TEXT, value TEXT);
                CREATE TABLE bets (
                    id INTEGER PRIMARY KEY, created_by TEXT, challenger TEXT,
                    opponent TEXT, description TEXT, amount REAL, status TEXT,
                    winner TEXT, settled_at TEXT
                );
                CREATE TABLE workout_entries (
                    id INTEGER PRIMARY KEY, sender TEXT, muscle_group TEXT,
                    exercise_name TEXT, sets_json TEXT, notes TEXT
                );
                CREATE TABLE bot_log (id INTEGER PRIMARY KEY, sender TEXT, event_type TEXT, payload TEXT);
            """)

    def rows(self, query, params=()):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(query, params).fetchall()

    def test_create_skill_persists_and_enable_disable_survive_reopen(self):
        reply = self.helpers["create_skill"]("admin", "warmup", "warm me up", "Five pushups")

        self.assertIn("created", reply)
        self.assertEqual([("admin", "warmup", "warm me up", "Five pushups", 1)], self.rows(
            "SELECT created_by, skill_name, trigger_phrase, response_template, enabled FROM skills",
        ))
        for action, enabled in (("disable", 0), ("enable", 1)):
            with self.subTest(action=action):
                reply = self.helpers["_cmd_skill_manage"](f"skill {action} warmup", "admin")
                self.assertIn(f"{action}d", reply)
                self.assertEqual([(enabled,)], self.rows("SELECT enabled FROM skills"))

    def test_sports_bet_log_and_settlement_persist(self):
        reply = self.helpers["_cmd_bet_log"]("/bet log Pacers +120 1.5u", "friend")

        self.assertIn("logged", reply)
        self.assertEqual([("friend", "Pacers", 120, 1.5, "pending")], self.rows(
            "SELECT sender, event, odds, stake, result FROM sports_bets",
        ))
        reply = self.helpers["_cmd_bet_settle"]("/bet settle 1 win", "friend")
        self.assertIn("cashed", reply)
        result, payout, settled_at = self.rows("SELECT result, payout, settled_at FROM sports_bets")[0]
        self.assertEqual("win", result)
        self.assertAlmostEqual(1.8, payout)
        self.assertTrue(settled_at)

    def test_social_bet_creation_and_settlement_persist(self):
        reply = self.helpers["_cmd_bets"]("bets new friend 20 next game", "admin")

        self.assertIn("created", reply)
        self.assertEqual([("admin", "friend", 20, "open")], self.rows(
            "SELECT created_by, opponent, amount, status FROM bets",
        ))
        reply = self.helpers["_cmd_bets"]("bets settle 1 friend", "admin")
        self.assertIn("settled", reply)
        status, winner, settled_at = self.rows("SELECT status, winner, settled_at FROM bets")[0]
        self.assertEqual(("settled", "friend"), (status, winner))
        self.assertTrue(settled_at)

    def test_workout_log_persists_all_sets_and_notes(self):
        reply = self.helpers["_cmd_workout_log"]("/workout log bench 185x5x3 felt good", "friend")

        self.assertIn("Logged", reply)
        sender, muscle, exercise, sets_json, notes = self.rows(
            "SELECT sender, muscle_group, exercise_name, sets_json, notes FROM workout_entries",
        )[0]
        self.assertEqual(("friend", "chest", "bench", "felt good"), (sender, muscle, exercise, notes))
        self.assertEqual([{"weight": 185, "reps": 5}] * 3, json.loads(sets_json))

    def test_group_persona_event_is_durable(self):
        payload = {"chat_id": "test-group", "persona": "coach", "note_len": 12}

        self.helpers["_log_group_persona_event"]("friend", "group_persona_updated", payload)

        sender, event, saved_payload = self.rows("SELECT sender, event_type, payload FROM bot_log")[0]
        self.assertEqual(("friend", "group_persona_updated"), (sender, event))
        self.assertEqual(payload, json.loads(saved_payload))

    def test_skill_and_social_bet_permission_denials_do_not_write(self):
        self.assertIn("required", self.helpers["create_skill"]("friend", "warmup", "warmup", "answer"))
        self.assertEqual([], self.rows("SELECT * FROM skills"))
        self.helpers["create_skill"]("admin", "warmup", "warmup", "answer")
        self.assertIn("required", self.helpers["_cmd_skill_manage"]("skill disable warmup", "friend"))
        self.assertEqual([(1,)], self.rows("SELECT enabled FROM skills"))
        self.assertIn("admin-only", self.helpers["_cmd_bets"]("bets new other 10 game", "friend"))
        self.assertEqual([], self.rows("SELECT * FROM bets"))

    def test_other_users_sports_bet_can_only_be_settled_by_admin(self):
        self.helpers["_cmd_bet_log"]("/bet log Pacers +120 1u", "other")

        self.assertIn("Only the bet owner", self.helpers["_cmd_bet_settle"]("/bet settle 1 loss", "friend"))
        self.assertEqual([("pending",)], self.rows("SELECT result FROM sports_bets"))
        self.assertIn("settled as loss", self.helpers["_cmd_bet_settle"]("/bet settle 1 loss", "admin"))
        self.assertEqual([("loss",)], self.rows("SELECT result FROM sports_bets"))

    def test_commit_failures_roll_back_each_mutator_before_success(self):
        self.helpers["create_skill"]("admin", "existing", "existing", "answer")
        self.helpers["_cmd_bet_log"]("/bet log Pacers +120 1u", "friend")
        self.helpers["_cmd_bets"]("bets new friend 10 game", "admin")
        cases = [
            ("create_skill", ("admin", "new", "new", "answer"), False),
            ("_cmd_skill_manage", ("skill disable existing", "admin"), False),
            ("_cmd_bet_log", ("/bet log Lakers -110 2u", "friend"), False),
            ("_cmd_bet_settle", ("/bet settle 1 win", "friend"), False),
            ("_cmd_bets", ("bets new friend 20 another game", "admin"), True),
            ("_cmd_bets", ("bets settle 1 friend", "admin"), True),
            ("_cmd_workout_log", ("/workout log bench 185x5x3", "friend"), False),
            ("_log_group_persona_event", ("friend", "group_persona_updated", {"note_len": 1}), False),
        ]
        failing_sqlite = SimpleNamespace(
            connect=lambda path: sqlite3.connect(path, factory=_FailingCommitConnection),
            IntegrityError=sqlite3.IntegrityError,
        )
        tables = ("skills", "sports_bets", "bets", "workout_entries", "bot_log")
        before = {table: self.rows(f"SELECT * FROM {table}") for table in tables}
        for name, args, raises in cases:
            with self.subTest(command=name, args=args):
                with patch.dict(self.helpers, {"sqlite3": failing_sqlite}):
                    if raises:
                        with self.assertRaises(sqlite3.OperationalError):
                            self.helpers[name](*args)
                    else:
                        reply = self.helpers[name](*args)
                        if reply is not None:
                            self.assertIn("fail", reply.lower())
                self.assertEqual(before, {table: self.rows(f"SELECT * FROM {table}") for table in tables})


if __name__ == "__main__":
    unittest.main()
