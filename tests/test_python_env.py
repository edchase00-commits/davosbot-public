import tempfile
import unittest
from pathlib import Path

from scripts.python_env import resolve_python_bin


class PythonEnvTests(unittest.TestCase):
    def test_prefers_repo_virtualenv_before_env_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_python = root / "venv" / "bin" / "python"
            repo_python.parent.mkdir(parents=True)
            repo_python.write_text("", encoding="utf-8")
            repo_python.chmod(0o755)

            resolved = resolve_python_bin(
                root,
                {
                    "HOME": str(root / "home"),
                    "PYTHON": "python3",
                },
            )

        self.assertEqual(repo_python.resolve(), Path(resolved).resolve())

    def test_falls_back_to_home_production_virtualenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "codex-work" / "davosbot"
            root.mkdir(parents=True)
            prod_python = Path(tmp) / "projects" / "davosbot" / "venv" / "bin" / "python"
            prod_python.parent.mkdir(parents=True)
            prod_python.write_text("", encoding="utf-8")
            prod_python.chmod(0o755)

            resolved = resolve_python_bin(
                root,
                {
                    "HOME": tmp,
                },
            )

        self.assertEqual(prod_python.resolve(), Path(resolved).resolve())


if __name__ == "__main__":
    unittest.main()
