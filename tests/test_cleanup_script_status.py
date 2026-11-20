"""Regression coverage for cleanup-script failure status and owner reporting."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "nightly_safe_cleanup_codex.sh"


class CleanupScriptStatusTests(unittest.TestCase):
    def test_cleanup_trap_reexits_original_pre_codex_status(self):
        source = SCRIPT.read_text(encoding="utf-8")
        function_start = source.index("cleanup() {")
        function_end = source.index("\ntrap cleanup EXIT", function_start)
        cleanup_function = source[function_start:function_end]

        with tempfile.TemporaryDirectory() as tmp:
            captured_status = Path(tmp) / "notified-status"
            command = f"""\
set -uo pipefail
notify_owner() {{ printf '%s' "$1" > "$CAPTURED_STATUS"; }}
{cleanup_function}
set +e
false
cleanup
"""
            result = subprocess.run(
                ["/bin/bash", "-c", command],
                env={**os.environ, "CAPTURED_STATUS": str(captured_status)},
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(captured_status.read_text(encoding="utf-8"), "1")

    def test_missing_workspace_exits_nonzero_and_reports_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            prod_dir = tmp_path / "prod"
            capture_file = tmp_path / "owner-message"
            python_path = prod_dir / "venv" / "bin" / "python"
            python_path.parent.mkdir(parents=True)
            python_path.symlink_to(sys.executable)

            package = prod_dir / "davosbot"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "commands.py").write_text(
                "def _fetch_change_log_rows():\n"
                "    return []\n\n"
                "def _bucket_change_log_rows(rows):\n"
                "    return {'green': [], 'yellow': [], 'red': []}\n",
                encoding="utf-8",
            )
            (package / "config.py").write_text(
                f"OWNER_ID = 'test-owner'\nPROJECT_ROOT = {str(prod_dir)!r}\n",
                encoding="utf-8",
            )
            (package / "imessage.py").write_text(
                "import os\n"
                "from pathlib import Path\n\n"
                "def send_message(owner, message):\n"
                "    Path(os.environ['OWNER_CAPTURE_FILE']).write_text(message, encoding='utf-8')\n",
                encoding="utf-8",
            )

            env = dict(os.environ)
            env.update(
                {
                    "DAVOSBOT_CLEANUP_SUPERVISED": "1",
                    "DAVOSBOT_PROD_DIR": str(prod_dir),
                    "DAVOSBOT_CODEX_WORK_DIR": str(tmp_path / "missing-workspace"),
                    "OWNER_CAPTURE_FILE": str(capture_file),
                }
            )
            result = subprocess.run(
                ["/bin/bash", str(SCRIPT), "--confirmed", "--notify"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn(
                "Codex safe cleanup stopped (confirmed).",
                capture_file.read_text(encoding="utf-8"),
            )
            logs = list((prod_dir / ".auto_deploy" / "codex_cleanup_logs").glob("*.log"))
            self.assertEqual(len(logs), 1)
            self.assertIn("Missing Codex workspace", logs[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
