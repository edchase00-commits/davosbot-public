import ast
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _ClosingConnection:
    def __init__(self, *args, **kwargs):
        self._conn = sqlite3.connect(*args, **kwargs)

    def __enter__(self):
        self._conn.__enter__()
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._conn.__exit__(exc_type, exc, tb)
        finally:
            self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _load_owner_quality_helpers(is_owner_func=lambda sender: sender == "owner"):
    tree = ast.parse((ROOT / "davosbot" / "main.py").read_text(encoding="utf-8"))
    wanted_assigns = {"_OWNER_QUALITY_INTAKE_RE", "_COMPLEX_ANALYSIS_RE"}
    wanted_funcs = {
        "_is_owner_quality_intake",
        "_log_owner_quality_intake_if_needed",
        "_complex_analysis_preflight_reply",
    }
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id in wanted_assigns for target in node.targets):
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted_funcs:
            nodes.append(node)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "BOT_DB_PATH": "",
        "json": json,
        "re": __import__("re"),
        "sqlite3": type("_SQLite", (), {"connect": _ClosingConnection}),
        "is_owner": is_owner_func,
        "redact_secret": lambda text: text.replace("secret-token", "[redacted]"),
    }
    exec(compile(module, str(ROOT / "davosbot" / "main.py"), "exec"), namespace)
    return namespace


def _load_capability_gap_detector():
    tree = ast.parse((ROOT / "davosbot" / "brain.py").read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "_CAPABILITY_GAP_RE" for target in node.targets):
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "detect_capability_gap":
            nodes.append(node)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"re": __import__("re")}
    exec(compile(module, str(ROOT / "davosbot" / "brain.py"), "exec"), namespace)
    return namespace["detect_capability_gap"]


class OwnerQualityIntakeTests(unittest.TestCase):
    def test_owner_quality_intake_detects_dumb_brain_feedback(self):
        helpers = _load_owner_quality_helpers()

        self.assertTrue(helpers["_is_owner_quality_intake"]("dumb brain"))
        self.assertTrue(helpers["_is_owner_quality_intake"]("you got confused on that sheet"))
        self.assertTrue(helpers["_is_owner_quality_intake"]("that was wrong"))
        self.assertFalse(helpers["_is_owner_quality_intake"]("that was fun"))

    def test_owner_quality_intake_logs_review_only_row(self):
        helpers = _load_owner_quality_helpers()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "davosbot.db")
            helpers["BOT_DB_PATH"] = db_path
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE change_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request TEXT NOT NULL,
                        reason TEXT,
                        created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

            reply = helpers["_log_owner_quality_intake_if_needed"](
                "owner",
                "dumb brain on this business analysis secret-token",
            )

            self.assertIn("Logged bot-quality intake #1 [YELLOW]", reply)
            conn = sqlite3.connect(db_path)
            try:
                request, reason = conn.execute("SELECT request, reason FROM change_log").fetchone()
            finally:
                conn.close()
            self.assertIn("[BOT-QUALITY YELLOW]", request)
            self.assertIn("[redacted]", request)
            self.assertNotIn("secret-token", request)
            metadata = json.loads(reason)
            self.assertEqual("owner_quality_intake", metadata["source"])
            self.assertTrue(metadata["review_only"])

    def test_complex_analysis_preflight_requires_context(self):
        helpers = _load_owner_quality_helpers()

        reply = helpers["_complex_analysis_preflight_reply"]("analyze this spreadsheet for revenue problems")

        self.assertIn("actual file", reply)
        self.assertIsNone(
            helpers["_complex_analysis_preflight_reply"](
                "analyze this spreadsheet for revenue problems",
                has_context=True,
            )
        )
        self.assertIsNone(helpers["_complex_analysis_preflight_reply"]("what's up"))

    def test_capability_gap_detects_file_context_failures(self):
        detect = _load_capability_gap_detector()

        self.assertTrue(detect("I need the spreadsheet before I can analyze that."))
        self.assertTrue(detect("Please upload the file so I can inspect it."))
        self.assertTrue(detect("I don't have enough context to do that."))
        self.assertTrue(detect("I can't do that right now."))

    def test_analysis_preflight_keeps_supplied_data_and_new_plans(self):
        preflight = _load_owner_quality_helpers()["_complex_analysis_preflight_reply"]
        for prompt in (
            "help me build a budget", "model a business with 50 customers paying $20",
            "analyze this spreadsheet:\nrevenue,cost\n100,75",
            "review my spreadsheet: revenue=100 cost=75",
            "analyze the spreadsheet /tmp/revenue.csv",
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(preflight(prompt))
        self.assertIsNone(preflight("analyze this spreadsheet", history=[{"role": "user", "content": "Revenue 100, cost 75"}]))
        self.assertIsNotNone(preflight("analyze this spreadsheet", history=[{"role": "assistant", "content": "Send the sheet"}]))


if __name__ == "__main__":
    unittest.main()
