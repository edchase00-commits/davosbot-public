"""Classification regressions using source bodies only; no runtime or live logs."""

import ast
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_commands():
    source = ROOT / "davosbot" / "commands.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {"_guess_muscle_group", "_parse_workout_input"}
    nodes = [
        node for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in names)
        or (isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_MUSCLE_MAP"
            for target in node.targets
        ))
    ]
    namespace = {"re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


class WorkoutMuscleGroupTests(unittest.TestCase):
    def setUp(self):
        self.commands = load_commands()
        self.guess = self.commands["_guess_muscle_group"]

    def test_all_plan_movement_names(self):
        cases = {
            "Seated leg press": "legs",
            "Machine chest press": "chest",
            "Seated cable row": "back",
            "Seated hamstring curl": "legs",
            "Supported standing calf raise": "legs",
            "Dead bug": "core",
            "Light goblet squat": "legs",
            "Glute bridge": "legs",
            "Seated lat pulldown": "back",
            "Light incline dumbbell chest press": "chest",
            "Light seated dumbbell shoulder press": "shoulders",
            "Bird dog": "core",
            "Low step-up": "legs",
            "Side plank": "core",
        }
        for exercise, expected in cases.items():
            with self.subTest(exercise=exercise):
                self.assertEqual(self.guess(exercise), expected)
                self.assertEqual(self.guess(exercise.upper()), expected)

    def test_specific_movements_precede_generic_keywords(self):
        cases = {
            "leg press": "legs", "single leg press": "legs",
            "shoulder press": "shoulders", "overhead press": "shoulders",
            "hamstring curl": "legs", "seated leg curl": "legs",
            "single leg glute bridge": "legs", "low step up": "legs",
            "single leg calf raise": "legs", "bird dog row": "back",
        }
        for exercise, expected in cases.items():
            with self.subTest(exercise=exercise):
                self.assertEqual(self.guess(exercise), expected)

    def test_existing_generic_matches_and_unknown_fallback(self):
        cases = {
            "press": "chest", "bench press": "chest", "incline press": "chest",
            "dumbbell fly": "chest", "curl": "arms", "bicep curl": "arms",
            "hammer curl": "arms", "tricep extension": "arms", "dip": "arms",
            "squat": "legs", "lunge": "legs", "deadlift": "legs",
            "row": "back", "pullup": "back", "lat pulldown": "back",
            "shoulder raise": "shoulders", "shrug": "shoulders", "ohp": "shoulders",
            "plank": "core", "crunch": "core", "situp": "core",
            "run": "cardio", "bike": "cardio", "swim": "cardio",
            "unknown movement": "other", "": "other",
        }
        for exercise, expected in cases.items():
            with self.subTest(exercise=exercise):
                self.assertEqual(self.guess(exercise), expected)

    def test_parser_uses_correct_group_without_changing_loads(self):
        for exercise, expected in (("leg press", "legs"), ("shoulder press", "shoulders"),
                                   ("hamstring curl", "legs")):
            with self.subTest(exercise=exercise):
                result = self.commands["_parse_workout_input"](f"{exercise} 20 lbs 2x10")
                self.assertIsInstance(result, dict)
                self.assertEqual(result["muscle_group"], expected)
                self.assertEqual(result["exercise_name"], exercise)
                self.assertEqual(json.loads(result["sets_json"]),
                                 [{"weight": 20.0, "reps": 10}] * 2)


if __name__ == "__main__":
    unittest.main()
