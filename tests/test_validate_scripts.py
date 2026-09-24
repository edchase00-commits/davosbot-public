import unittest
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ValidateScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell unavailable")
    def test_powershell_validator_returns_test_failure_without_compiling(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        with tempfile.TemporaryDirectory() as tmp:
            fake_python = Path(tmp) / "fake-python.ps1"
            calls = Path(tmp) / "calls.txt"
            fake_python.write_text(
                "Add-Content -LiteralPath $env:DAVOS_VALIDATION_TEST_CALLS -Value ($args -join ' ')\n"
                "$global:LASTEXITCODE = 23\n", encoding="utf-8",
            )
            env = dict(os.environ, PYTHON=str(fake_python), DAVOS_VALIDATION_TEST_CALLS=str(calls))
            result = subprocess.run([shell, "-NoProfile", "-File", str(ROOT / "scripts" / "validate.ps1")],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(23, result.returncode, result.stderr)
            self.assertEqual(["-m unittest discover -s tests"], calls.read_text().splitlines())

    def test_bash_validator_prefers_repo_virtualenv(self):
        script = (ROOT / "scripts" / "validate.sh").read_text(encoding="utf-8")

        self.assertIn("${PYTHON:-}", script)
        self.assertIn("DAVOSBOT_PROD_DIR", script)
        self.assertIn('$HOME/projects/davosbot', script)
        self.assertIn("/.auto_deploy/worktrees/", script)
        self.assertIn("venv/bin/python", script)
        self.assertIn(".venv/bin/python", script)
        self.assertIn('"$PYTHON_BIN" -m unittest discover -s tests', script)

    def test_powershell_validator_prefers_repo_virtualenv(self):
        script = (ROOT / "scripts" / "validate.ps1").read_text(encoding="utf-8")

        self.assertIn("$env:PYTHON", script)
        self.assertIn("DAVOSBOT_PROD_DIR", script)
        self.assertIn('projects/davosbot', script)
        self.assertIn("venv\\Scripts\\python.exe", script)
        self.assertIn(".venv\\Scripts\\python.exe", script)
        self.assertIn("venv/bin/python", script)
        self.assertIn("& $pythonBin -m unittest discover -s tests", script)


if __name__ == "__main__":
    unittest.main()
