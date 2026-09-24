import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import quality_check


class QualityCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "review"
        (self.root / "tests").mkdir(parents=True)
        package = self.root / "davosbot"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "config.py").write_text(
            "from pathlib import Path\nPROJECT_ROOT = Path(__file__).resolve().parents[1]\n", encoding="utf-8",
        )
        (package / "imessage.py").write_text(
            "def send_message(*args, **kwargs): pass\ndef send_file(*args, **kwargs): pass\n", encoding="utf-8",
        )
        caller = Path.cwd()
        os.chdir(self.root)
        # Restore before TemporaryDirectory cleanup, including a watcher caller
        # whose checkout lives below the production directory.
        self.addCleanup(os.chdir, caller)

    def child(self, source):
        filename = "test_synthetic.py"
        (self.root / "tests" / filename).write_text(source, encoding="utf-8")
        real_run = subprocess.run
        outputs = []

        def capture(*args, **kwargs):
            result = real_run(*args, **kwargs, capture_output=True, text=True)
            outputs.append(result)
            return result

        with patch.object(quality_check.subprocess, "run", side_effect=capture):
            code = quality_check.run_tests(self.root, [filename], 20)
        return code, outputs[0].stdout + outputs[0].stderr

    def test_list_never_launches_a_subprocess(self):
        with patch.object(quality_check.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, quality_check.main(["--list"]))
        run.assert_not_called()
        for name in quality_check.SUITES:
            self.assertIn(name + ":", output.getvalue())

    def test_missing_file_or_empty_selection_cannot_report_success(self):
        with patch.object(quality_check, "SUITES", {"images": ("test_missing.py",)}):
            with self.assertRaisesRegex(ValueError, "missing"):
                quality_check.selected_files(["images"], self.root)
        with patch.object(quality_check.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "missing"):
                quality_check.run_tests(self.root, ["test_missing.py"], 20)
            with self.assertRaisesRegex(ValueError, "No tests"):
                quality_check.run_tests(self.root, [], 20)
        run.assert_not_called()

    def test_shared_regression_runs_once_but_each_selected_file_is_required(self):
        for filename in ("test_shared.py", "test_access.py"):
            (self.root / "tests" / filename).write_text("", encoding="utf-8")
        suites = {
            "schedules": ("test_shared.py",),
            "access": ("test_shared.py", "test_access.py"),
        }
        with patch.object(quality_check, "SUITES", suites):
            self.assertEqual(
                ["test_shared.py", "test_access.py"],
                quality_check.selected_files(["schedules", "access", "schedules"], self.root),
            )
            (self.root / "tests" / "test_access.py").unlink()
            with self.assertRaisesRegex(ValueError, "test_access.py"):
                quality_check.selected_files(["schedules", "access"], self.root)

    def test_production_source_or_working_directory_is_refused_before_subprocess(self):
        production = Path(self.temp.name) / "production"
        (self.root / "tests" / "test_synthetic.py").write_text("", encoding="utf-8")
        with patch.dict(os.environ, {"DAVOSBOT_PROD_DIR": str(production)}), patch.object(quality_check.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "production"):
                quality_check.run_tests(production, ["test_synthetic.py"], 20)
            with patch.object(Path, "cwd", return_value=production):
                with self.assertRaisesRegex(ValueError, "production"):
                    quality_check.run_tests(self.root, ["test_synthetic.py"], 20)
        run.assert_not_called()

    def test_fixture_runs_from_review_and_restores_synthetic_deploy_caller(self):
        production = self.root / "synthetic-production"
        caller = production / ".auto_deploy" / "worktrees" / "synthetic-candidate"
        caller.mkdir(parents=True)
        previous = Path.cwd()
        result = unittest.TestResult()
        try:
            os.chdir(caller)
            with patch.dict(os.environ, {"DAVOSBOT_PROD_DIR": str(production)}):
                for name in (
                    "test_real_child_has_synthetic_state_and_no_parent_secrets",
                    "test_missing_file_or_empty_selection_cannot_report_success",
                ):
                    QualityCheckTests(name).run(result)
                    self.assertEqual(caller.resolve(), Path.cwd().resolve())
            self.assertEqual(2, result.testsRun)
            self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        finally:
            os.chdir(previous)

    def test_real_child_has_synthetic_state_and_no_parent_secrets(self):
        source = '''import os, socket, sqlite3, unittest
from pathlib import Path
from davosbot import config, imessage
class Isolation(unittest.TestCase):
    def test_isolation(self):
        root = config.PROJECT_ROOT.resolve()
        self.assertEqual(root, Path.cwd().resolve())
        self.assertNotEqual(root, Path(__file__).resolve().parents[1])
        for key in ("BOT_DB_PATH", "DB_PATH", "SOUL_PATH", "MEMORY_PATH", "GENERATED_DIR",
                    "IMAGE_OUTPUT_DIR", "OPENAI_IMAGE_OUTPUT_DIR", "FANTASY_ACCESS_PRIVATE_KEY_PATH"):
            self.assertTrue(Path(os.environ[key]).resolve().is_relative_to(root), key)
        self.assertEqual("1", os.environ["PYTHON_DOTENV_DISABLED"])
        self.assertEqual("", os.environ["GEMINI_API_KEY"])
        self.assertIsNone(os.environ.get("QUALITY_TEST_PARENT_SECRET"))
        self.assertIn("Synthetic", Path(os.environ["SOUL_PATH"]).read_text())
        self.assertEqual("", Path(os.environ["MEMORY_PATH"]).read_text())
        with sqlite3.connect(os.environ["BOT_DB_PATH"]) as conn:
            conn.execute("CREATE TABLE synthetic_test (id INTEGER)")
'''
        original_db = Path(self.temp.name) / "must-not-exist.sqlite"
        with patch.dict(os.environ, {
            "BOT_DB_PATH": str(original_db), "GEMINI_API_KEY": "synthetic-test-value",
            "QUALITY_TEST_PARENT_SECRET": "synthetic-test-value",
        }):
            code, output = self.child(source)
        self.assertEqual(0, code, output)
        self.assertFalse(original_db.exists())

    def test_caught_live_io_attempts_still_fail_the_run(self):
        code, output = self.child('''import socket, unittest
from davosbot import imessage
class CatchingProvider(unittest.TestCase):
    def test_fallback(self):
        with self.assertRaisesRegex(RuntimeError, "mocked network"):
            socket.getaddrinfo("quality-check.invalid", 80)
        with self.assertRaisesRegex(RuntimeError, "mocked message sends"):
            imessage.send_message("+15550000001", "Synthetic blocked send")
''')
        self.assertEqual(1, code)
        self.assertIn("Unmocked I/O attempts blocked: imessage.send, socket.getaddrinfo", output)
        self.assertNotIn("quality-check.invalid", output)
        self.assertNotIn("+15550000001", output)

    def test_failed_or_zero_test_child_returns_nonzero(self):
        code, output = self.child("import unittest\nclass Failure(unittest.TestCase):\n    def test_failure(self): self.fail('synthetic failure')\n")
        self.assertEqual(1, code)
        self.assertIn("synthetic failure", output)
        code, output = self.child("# Intentionally no tests.\n")
        self.assertNotEqual(0, code)
        self.assertIn("contains no tests", output)

    def test_timeout_is_reported_with_distinct_nonzero_exit(self):
        (self.root / "tests" / "test_synthetic.py").write_text("", encoding="utf-8")
        with patch.object(quality_check.subprocess, "run", side_effect=subprocess.TimeoutExpired("test", 1)):
            with contextlib.redirect_stderr(io.StringIO()) as output:
                self.assertEqual(124, quality_check.run_tests(self.root, ["test_synthetic.py"], 1))
        self.assertIn("timed out", output.getvalue())


if __name__ == "__main__":
    unittest.main()
