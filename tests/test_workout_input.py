"""Pure/mocked workout logging regressions; never use a runtime journal or send."""

import ast
from contextlib import closing
import json
from pathlib import Path
import re
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]


def load_commands():
    source = ROOT / "davosbot" / "commands.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {"_parse_workout_input", "_cmd_workout_log"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    sqlite = Mock()
    namespace = {
        "re": re, "_guess_muscle_group": lambda name: "other", "sqlite3": sqlite,
        "BOT_DB_PATH": "not-a-real-journal", "closing": closing,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


class WorkoutInputTests(unittest.TestCase):
    def setUp(self):
        self.commands = load_commands()
        self.parse = self.commands["_parse_workout_input"]

    def parsed(self, text):
        result = self.parse(text)
        self.assertIsInstance(result, dict, (text, result))
        result["sets"] = json.loads(result["sets_json"])
        return result

    def rejected(self, text):
        result = self.commands["_cmd_workout_log"](text, "synthetic-owner")
        self.assertIsInstance(result, str)
        self.assertNotIn("Logged", result)
        self.assertNotIn("Workout log failed", result)
        self.commands["sqlite3"].connect.assert_not_called()
        return result

    def test_explicit_pound_aliases_never_fall_back_to_bodyweight(self):
        for unit in ("lb", "lbs", "pound", "pounds", "LBS"):
            for separator in ("", " "):
                for sets in ("2x10", "x10x2"):
                    with self.subTest(unit=unit, separator=separator, sets=sets):
                        parsed = self.parsed(f"workout log leg press 100{separator}{unit} {sets}")
                        self.assertEqual(parsed["exercise_name"], "leg press")
                        self.assertEqual(parsed["sets"], [{"weight": 100.0, "reps": 10}] * 2)
                        self.assertEqual(parsed["notes"], "")
                        self.assertIn("100lbs", parsed["summary"])

    def test_explicit_kg_converts_and_retains_source_with_notes(self):
        for unit in ("kg", "kgs", "KG"):
            for sets in ("2x10", "x10x2"):
                with self.subTest(unit=unit, sets=sets):
                    parsed = self.parsed(f"workout log leg press 100{unit} {sets} RIR 2 felt easy")
                    self.assertEqual(parsed["exercise_name"], "leg press")
                    self.assertEqual(parsed["sets"], [{"weight": 220.462262, "reps": 10}] * 2)
                    self.assertIn(f"source load: 100{unit}", parsed["notes"])
                    self.assertIn("RIR 2 felt easy", parsed["notes"])

    def test_decimal_loads_and_per_hand_basis_are_preserved(self):
        pounds = self.parsed("shoulder press 22.5 lbs x10x2 RIR 3 per hand")
        self.assertEqual(pounds["sets"], [{"weight": 22.5, "reps": 10}] * 2)
        self.assertEqual(pounds["notes"], "RIR 3 per hand")
        kg = self.parsed("shoulder press 12.50 kg 2x10 RPE 8 per hand")
        self.assertEqual(kg["sets"], [{"weight": 27.557783, "reps": 10}] * 2)
        self.assertIn("12.50kg", kg["notes"])
        self.assertIn("RPE 8 per hand", kg["notes"])

    def test_legacy_unitless_weighted_formats_still_mean_pounds(self):
        for text, weight, reps, count, notes in (
            ("/workout log bench 185x5x3", 185, 5, 3, ""),
            ("squat 225 5x5 felt heavy", 225, 5, 5, "felt heavy"),
            ("shoulder press 20x10x2 RIR 3 per hand", 20, 10, 2, "RIR 3 per hand"),
        ):
            with self.subTest(text=text):
                parsed = self.parsed(text)
                self.assertEqual(parsed["sets"], [{"weight": weight, "reps": reps}] * count)
                self.assertEqual(parsed["notes"], notes)

    def test_varied_sets_preserve_complete_effort_tail(self):
        for notes in ("RIR 2 felt easy", "RPE 8.5, fatigue high; per hand", "last 2 reps hard", "RPE 8, 2 RIR"):
            with self.subTest(notes=notes):
                parsed = self.parsed(f"bench 3 sets: 185x5, 190x5, 195x4 {notes}")
                self.assertEqual(parsed["sets"], [
                    {"weight": 185, "reps": 5}, {"weight": 190, "reps": 5}, {"weight": 195, "reps": 4},
                ])
                self.assertEqual(parsed["notes"], notes)

    def test_varied_explicit_units_convert_each_set_independently(self):
        parsed = self.parsed("bench 3 sets: 100kg x5, 225lbs x5, 105kgs x4 RIR 2")
        self.assertEqual(parsed["sets"], [
            {"weight": 220.462262, "reps": 5}, {"weight": 225, "reps": 5},
            {"weight": 231.485375, "reps": 4},
        ])
        self.assertIn("100kg, 105kgs", parsed["notes"])
        self.assertTrue(parsed["notes"].startswith("RIR 2;"))

    def test_varied_partial_unit_annotation_is_ambiguous(self):
        for text in ("bench 2 sets: 100kg x5, 105x5", "bench 2 sets: 185x5, 190lbs x5"):
            with self.subTest(text=text):
                self.assertIn("every load", self.rejected(text))

    def test_varied_count_mismatch_or_skipped_fragments_never_write(self):
        for text in (
            "bench 3 sets: 185x5, 190x5", "bench 2 sets: 185x5, 190x5, 195x4",
            "bench 2 sets: 185x5, bad 190x5", "bench 2 sets: 185x5 190x5",
            "bench 2 sets: 185x5, 190x5, 195", "bench 2 sets: 185x5, 190x5 195",
            "bench 2 sets: 185x5, 190x5 RIR 2, 195x4", "bench 2 sets: 185x5, 190x5.5",
            "bench 2 sets: 185x5, 190x5 RIR 2 195",
        ):
            with self.subTest(text=text):
                self.rejected(text)

    def test_malformed_weight_syntax_never_becomes_bodyweight(self):
        for text in (
            "leg press 100stones 2x10", "leg press 100 kilograms 2x10",
            "leg press 100lbskg 2x10", "leg press 100kgkg 2x10",
            "leg press 100..5lbs 2x10", "leg press -100lbs 2x10",
            "leg press 100/200lbs 2x10", "leg press 100 lbs per hand 2x10",
            "leg press 100 200 2x10", "bench 185x5x3kg", "bench 185x5x3 kg",
            "bench 185x5x3 225", "bench 185x5x3 and 225x3", "pushups 3x20lbs",
            "leg press lbs 2x10", "leg press kg 2x10", "leg press 2x10 kg100",
            "bench 185lbs x5x3e2", "bench 185lbs x5x3RIR 2", "pushups 3x20e2",
            "bench 1 set: 185x5e2",
            "bench 100 2x10 (kg)", "bench 100 2x10 in kg",
            "bench 2 sets: 100x5, 105x5 RIR 2 (kg)",
            "leg press .5lbs 2x10", "leg press NaNlbs 2x10", "leg press Infinitylbs 2x10",
            "leg press (100lbs) 2x10", "leg press [100kg] 2x10", "leg press (.5lbs) 2x10",
            "leg press lbs100 2x10", "leg press kg100 2x10",
        ):
            with self.subTest(text=text):
                self.rejected(text)

    def test_timed_holds_are_not_repetitions(self):
        for text in (
            "side plank 2x20 seconds per side", "plank 2x20s", "plank 2x20sec",
            "plank 2x20 mins", "plank 2x20 minutes", "plank 2x20seconds",
            "side plank 2x20-second holds",
            "plank 2x20 (seconds)", "plank 2x20 holds of 20 seconds",
            "plank 2x20 rest 60 seconds, hold 20 seconds",
            "plank (seconds) 2x20", "plank (20 seconds) 2x1",
            "plank 2 sets: 0x20 seconds, 0x20 seconds", "plank 0x20x2 seconds",
        ):
            with self.subTest(text=text):
                self.rejected(text)

    def test_rest_notes_are_not_mistaken_for_timed_reps(self):
        for text in (
            "pushups 3x20 rest 60 seconds", "bench 185lbs x5x3 rest 2 minutes",
            "bench 2 sets: 185x5, 190x5 RIR 2; rest 90 seconds",
            "bench 185x5x3 90 seconds rest", "pushups 3x20 RIR 2; 90s rest",
        ):
            with self.subTest(text=text):
                parsed = self.parsed(text)
                self.assertIn("rest", parsed["notes"])

    def test_rep_based_tempo_notes_are_preserved(self):
        parsed = self.parsed("pushups 3x20 with a 3 second eccentric")
        self.assertEqual(parsed["notes"], "with a 3 second eccentric")

    def test_successful_command_preserves_parsed_payload_at_mock_sql_boundary(self):
        for text in (
            "shoulder press 12.5kg 2x10 RIR 2 per hand",
            "bench 3 sets: 185x5, 190x5, 195x4 RPE 8, 2 RIR",
        ):
            with self.subTest(text=text):
                expected = self.parsed(text)
                self.commands["sqlite3"].reset_mock()
                reply = self.commands["_cmd_workout_log"](text, "synthetic-owner")
                self.assertTrue(reply.startswith("Logged"), reply)
                connection = self.commands["sqlite3"].connect.return_value
                sql, args = connection.execute.call_args.args
                self.assertIn("INSERT INTO workout_entries", sql)
                self.assertEqual(args, ("synthetic-owner", "other", expected["exercise_name"],
                                        expected["sets_json"], expected["notes"]))
                connection.commit.assert_called_once()
                connection.close.assert_called_once()

    def test_bodyweight_legacy_heuristic_stays_compatible(self):
        for text, count, reps in (("pushups 3x20", 3, 20), ("situps 20x3", 3, 20), ("pullups 5x5", 5, 5)):
            with self.subTest(text=text):
                parsed = self.parsed(text)
                self.assertEqual(parsed["sets"], [{"weight": 0, "reps": reps}] * count)
                self.assertIn("bodyweight", parsed["summary"])

    def test_numeric_exercise_descriptors_remain_supported(self):
        for exercise in ("1-arm row", "1 arm row", "2 arm curl", "2-leg curl", "5/3/1 bench",
                         "45 degree leg press", "45-degree back extension"):
            with self.subTest(exercise=exercise):
                parsed = self.parsed(f"{exercise} 100lbs 2x10")
                self.assertEqual(parsed["exercise_name"], exercise)
                self.assertEqual(parsed["sets"], [{"weight": 100, "reps": 10}] * 2)

    def test_invalid_or_unbounded_quantities_never_write(self):
        for text in (
            "bench 185x5x0", "bench 185x0x3", "bench 185x5x999999999",
            "bench 5001lbs x5x3", "bench 3000kg x5x3", "pushups 2x1001",
            "bench 0 sets: 185x5", "bench 31 sets: 185x5",
            "bench 185lbs x5x" + "9" * 4500,
        ):
            with self.subTest(text=text):
                self.rejected(text)

    def test_bridge_schema_describes_canonical_units_and_notes(self):
        from davosbot.work_actions_extra import EXTRA_ACTIONS
        bridge = EXTRA_ACTIONS["workouts.log"]
        description = bridge["description"]
        for concept in ("pounds (lbs)", "0.45359237", "ambiguous conversational", "seconds",
                        "RIR/RPE", "fatigue", "per-hand", "kg"):
            self.assertIn(concept, description)


if __name__ == "__main__":
    unittest.main()
