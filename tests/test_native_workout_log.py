"""Synthetic native logging boundary tests; no runtime DB or messages."""

import ast
from copy import deepcopy
import json
import math
from pathlib import Path
import sqlite3
from types import ModuleType
from typing import Callable
import unittest
from unittest.mock import MagicMock, Mock, patch


def load_logger():
    source = Path(__file__).resolve().parents[1] / "davosbot" / "workouts.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name in {"workout_log_tool", "format_weight"}]
    namespace = {"json": json, "math": math, "Callable": Callable,
                 "BOT_DB_PATH": "synthetic-journal", "connect_bot_db": Mock(),
                 "__package__": "davosbot"}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    return namespace["workout_log_tool"]


class NativeWorkoutLogTests(unittest.TestCase):
    def setUp(self):
        self.log = load_logger()
        self.connect = MagicMock()
        self.conn = self.connect.return_value.__enter__.return_value
        self.payload = {"exercise_name": "bench press", "muscle_group": "chest",
                        "sets": [{"weight": 100, "reps": 10}], "notes": "RIR 2"}

    def run_log(self, payload):
        return self.log(payload, "synthetic-owner", db_path="synthetic-journal",
                        connect_fn=self.connect)

    def rejected(self, payload):
        before = deepcopy(payload)
        reply = self.run_log(payload)
        self.assertIsInstance(reply, str)
        self.assertNotIn("Logged", reply)
        self.assertNotIn("failed", reply)
        self.connect.assert_not_called()
        # NaN does not compare equal to itself; serialization also proves that
        # malformed numeric values were not normalized in place.
        self.assertEqual(repr(payload), repr(before))

    def test_malformed_payloads(self):
        for value in (None, [], "bench", 3, True):
            with self.subTest(value=value):
                self.rejected(value)

    def test_invalid_text_fields(self):
        for field, limit in (("exercise_name", 120), ("muscle_group", 40), ("notes", 500)):
            for value in (None, False, 123, [], {}, "x" * (limit + 1)):
                with self.subTest(field=field, value=value):
                    self.rejected({**self.payload, field: value})
        for value in ("", "   ", "\t\n"):
            self.rejected({**self.payload, "exercise_name": value})
        payload = deepcopy(self.payload)
        del payload["exercise_name"]
        self.rejected(payload)

    def test_invalid_set_containers(self):
        for value in (None, {}, "100x10", 1, True, (), [], [None], [10], [[]],
                      [{"weight": 0, "reps": 1}] * 31):
            with self.subTest(value=value):
                self.rejected({**self.payload, "sets": value})
        payload = deepcopy(self.payload)
        del payload["sets"]
        self.rejected(payload)

    def test_missing_or_extra_set_fields(self):
        for value in ({}, {"weight": 100}, {"reps": 10},
                      {"weight": 100, "reps": 10, "seconds": 30}):
            with self.subTest(value=value):
                self.rejected({**self.payload, "sets": [value]})

    def test_invalid_numeric_values(self):
        shared = [None, True, False, "", "10", "bad", [], {}, float("nan"),
                  float("inf"), -float("inf"), -1, 10 ** 400]
        for field, values in (("weight", shared + [5000.01]),
                              ("reps", shared + [0, 0.9, 10.5, 1001])):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.rejected({**self.payload, "sets": [{"weight": 100, "reps": 10, field: value}]})

    def test_invalid_later_set_does_not_write_or_mutate_earlier_sets(self):
        self.rejected({**self.payload, "sets": [{"weight": 100, "reps": 10},
                                                {"weight": 120, "reps": 5.5}]})

    def test_exact_payload_preserves_notes_and_converted_fraction(self):
        weight = 12.5 / 0.45359237
        payload = {"exercise_name": "  shoulder press  ", "muscle_group": "shoulders",
                   "sets": [{"weight": weight, "reps": 10.0}],
                   "notes": "source load: 12.5kg; RIR 2; per hand"}
        original = deepcopy(payload)
        reply = self.run_log(payload)
        self.assertTrue(reply.startswith("Logged"))
        self.assertEqual(payload, original)
        self.connect.assert_called_once_with("synthetic-journal")
        self.conn.execute.assert_called_once_with(
            "INSERT INTO workout_entries (sender, muscle_group, exercise_name, sets_json, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            ("synthetic-owner", "shoulders", "shoulder press",
             json.dumps([{"weight": weight, "reps": 10}]), payload["notes"]))

    def test_truthful_independent_totals_for_varied_sets(self):
        payload = {**self.payload, "sets": [{"weight": 100, "reps": 12},
                                            {"weight": 120, "reps": 5},
                                            {"weight": 0, "reps": 8}]}
        reply = self.run_log(payload)
        self.assertEqual(reply, "Logged — bench press: 3 sets, 25 total reps, up to 120lbs")
        self.assertEqual(json.loads(self.conn.execute.call_args.args[1][3]), payload["sets"])

    def test_explicit_bodyweight(self):
        reply = self.run_log({**self.payload, "sets": [{"weight": 0, "reps": 12}] * 2})
        self.assertEqual(reply, "Logged — bench press: 2 sets, 24 total reps (bodyweight)")

    def test_sqlite_transaction_stores_exact_synthetic_payload(self):
        # A fresh in-memory database cannot touch the real journal. Exercise
        # sqlite's own parameter binding, transaction and JSON round trip.
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE workout_entries (sender TEXT, muscle_group TEXT, "
                     "exercise_name TEXT, sets_json TEXT, notes TEXT)")
        payload = {**self.payload, "sets": [{"weight": 12.5 / 0.45359237, "reps": 10},
                                            {"weight": 0, "reps": 12}],
                   "notes": "source load: 12.5kg; RIR 2; per hand"}
        connect = Mock(return_value=conn)
        reply = self.log(payload, "synthetic-owner", db_path=":memory:", connect_fn=connect)
        self.assertTrue(reply.startswith("Logged"))
        self.assertFalse(conn.in_transaction)
        rows = conn.execute("SELECT * FROM workout_entries").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:3], ("synthetic-owner", "chest", "bench press"))
        self.assertEqual(json.loads(rows[0][3]), payload["sets"])
        self.assertEqual(rows[0][4], payload["notes"])
        connect.reset_mock()
        invalid = {**payload, "sets": payload["sets"] + [{"weight": 100, "reps": 5.5}]}
        self.assertTrue(self.log(invalid, "synthetic-owner", connect_fn=connect).startswith("Invalid"))
        connect.assert_not_called()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM workout_entries").fetchone()[0], 1)

    def test_inclusive_limits(self):
        payload = {"exercise_name": "e" * 120, "muscle_group": "m" * 40,
                   "notes": "n" * 500, "sets": [{"weight": 5000, "reps": 1000}] * 30}
        self.assertTrue(self.run_log(payload).startswith("Logged"))
        self.assertEqual(len(json.loads(self.conn.execute.call_args.args[1][3])), 30)

    def test_missing_optional_fields_and_muscle_inference(self):
        commands = ModuleType("davosbot.commands")
        commands._guess_muscle_group = Mock(return_value="chest")
        with patch.dict("sys.modules", {"davosbot.commands": commands}):
            reply = self.run_log({"exercise_name": "bench", "sets": [{"weight": 1, "reps": 1}]})
        self.assertTrue(reply.startswith("Logged"))
        commands._guess_muscle_group.assert_called_once_with("bench")
        self.assertEqual(self.conn.execute.call_args.args[1][1], "chest")
        self.assertEqual(self.conn.execute.call_args.args[1][4], "")

    def test_database_failures_never_claim_success(self):
        for stage in ("connect", "insert", "commit"):
            with self.subTest(stage=stage):
                self.setUp()
                error = sqlite3.OperationalError("synthetic failure")
                if stage == "connect":
                    self.connect.side_effect = error
                elif stage == "insert":
                    self.conn.execute.side_effect = error
                else:
                    self.connect.return_value.__exit__.side_effect = error
                self.assertEqual(self.run_log(self.payload), "Workout log failed: synthetic failure")


if __name__ == "__main__":
    unittest.main()
