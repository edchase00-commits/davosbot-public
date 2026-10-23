import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from davosbot import tools
from davosbot import permissions


def _init_workout_db(path: str) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            CREATE TABLE workout_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL DEFAULT (date('now')),
                sender TEXT NOT NULL,
                muscle_group TEXT,
                exercise_name TEXT NOT NULL,
                sets_json TEXT NOT NULL,
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE workouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exercise TEXT NOT NULL,
                sets INTEGER,
                reps INTEGER,
                weight_lbs REAL DEFAULT 0,
                notes TEXT DEFAULT '',
                ts TEXT NOT NULL
            )
            """
        )
        conn.commit()


class WorkoutSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = str(Path(self.tmp.name) / "workouts.db")
        _init_workout_db(self.db_path)
        self.db_patch = patch.object(tools, "BOT_DB_PATH", self.db_path)
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_legacy_log_workout_writes_canonical_entries(self):
        reply = tools.execute_tool(
            "log_workout",
            {"exercise": "bench press", "sets": 3, "reps": 5, "weight_lbs": 185, "notes": "solid"},
            sender="+15550000001",
        )

        self.assertIn("Logged: bench press 3x5 @ 185lbs", reply)
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT sender, muscle_group, exercise_name, sets_json, notes FROM workout_entries"
            ).fetchall()
            legacy_count = conn.execute("SELECT COUNT(*) FROM workouts").fetchone()[0]

        self.assertEqual(legacy_count, 0)
        self.assertEqual(rows[0][0], "+15550000001")
        self.assertEqual(rows[0][1], "chest")
        self.assertEqual(rows[0][2], "bench press")
        self.assertEqual(rows[0][4], "solid")
        sets = json.loads(rows[0][3])
        self.assertEqual(len(sets), 3)
        self.assertEqual(sets[0], {"weight": 185.0, "reps": 5})

    def test_query_workout_recent_is_sender_scoped(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO workout_entries (sender, muscle_group, exercise_name, sets_json, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                ("owner", "chest", "bench", json.dumps([{"weight": 185, "reps": 5}]), "owner note"),
            )
            conn.execute(
                "INSERT INTO workout_entries (sender, muscle_group, exercise_name, sets_json, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                ("friend", "legs", "squat", json.dumps([{"weight": 225, "reps": 3}]), "friend note"),
            )
            conn.commit()

        reply = tools._query_workout({"query_type": "recent"}, sender="owner")

        self.assertIn("bench", reply)
        self.assertIn("185lbs x5", reply)
        self.assertIn("owner note", reply)
        self.assertNotIn("squat", reply)
        self.assertNotIn("friend note", reply)

    def test_execute_tool_query_workout_uses_sender_scope(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO workout_entries (sender, muscle_group, exercise_name, sets_json, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                ("owner", "chest", "bench", json.dumps([{"weight": 185, "reps": 5}]), "owner note"),
            )
            conn.execute(
                "INSERT INTO workout_entries (sender, muscle_group, exercise_name, sets_json, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                ("friend", "legs", "squat", json.dumps([{"weight": 225, "reps": 3}]), "friend note"),
            )
            conn.commit()

        reply = tools.execute_tool("query_workout", {"query_type": "recent"}, sender="friend")

        self.assertIn("squat", reply)
        self.assertIn("friend note", reply)
        self.assertNotIn("bench", reply)
        self.assertNotIn("owner note", reply)

    def test_workout_log_tool_writes_patched_tools_db_path(self):
        reply = tools.execute_tool(
            "workout_log",
            {
                "exercise_name": "pull ups",
                "sets": [{"weight": 0, "reps": 8}, {"weight": 0, "reps": 7}],
                "notes": "clean reps",
            },
            sender="friend",
        )

        self.assertIn("Logged", reply)
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT sender, exercise_name, sets_json, notes FROM workout_entries"
            ).fetchone()

        self.assertEqual(row[0], "friend")
        self.assertEqual(row[1], "pull ups")
        self.assertEqual(json.loads(row[2]), [{"weight": 0.0, "reps": 8}, {"weight": 0.0, "reps": 7}])
        self.assertEqual(row[3], "clean reps")

    def test_query_workout_summary_uses_canonical_sets(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO workout_entries (sender, muscle_group, exercise_name, sets_json, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                ("owner", "chest", "bench", json.dumps([{"weight": 185, "reps": 5}]), ""),
            )
            conn.execute(
                "INSERT INTO workout_entries (sender, muscle_group, exercise_name, sets_json, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                ("owner", "chest", "bench", json.dumps([{"weight": 195, "reps": 3}]), ""),
            )
            conn.commit()

        reply = tools._query_workout({"query_type": "summary"}, sender="owner")

        self.assertIn("bench: 2 sessions, max 195lbs", reply)

    def test_legacy_fallback_is_owner_only_because_rows_are_unscoped(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO workouts (exercise, sets, reps, weight_lbs, notes, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("deadlift", 1, 3, 315, "legacy", "2026-05-01T00:00:00"),
            )
            conn.commit()
        with patch.object(permissions, "is_owner", side_effect=lambda sender: sender == "owner"):
            owner_reply = tools._query_workout({"query_type": "recent"}, sender="owner")
            friend_reply = tools._query_workout({"query_type": "recent"}, sender="friend")

        self.assertIn("deadlift", owner_reply)
        self.assertIn("legacy", owner_reply)
        self.assertEqual(friend_reply, "No workouts logged yet.")

    def test_workout_tool_definitions_and_permissions_stay_public(self):
        names = [tool["name"] for tool in tools.TOOL_DEFINITIONS]

        self.assertEqual(1, names.count("workout_log"))
        self.assertEqual(1, names.count("log_workout"))
        self.assertEqual(1, names.count("query_workout"))
        self.assertNotIn("workout_log", tools._OWNER_ONLY_TOOLS)
        self.assertNotIn("log_workout", tools._OWNER_ONLY_TOOLS)
        self.assertNotIn("query_workout", tools._OWNER_ONLY_TOOLS)


if __name__ == "__main__":
    unittest.main()
