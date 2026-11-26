"""Exercise the cleanup runner's real Codex argument expansion without running Codex."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "nightly_safe_cleanup_codex.sh"


class ModelSelectionTests(unittest.TestCase):
    def invocation_args(self, value):
        source = SCRIPT.read_text(encoding="utf-8")
        selection_start = source.index("CODEX_CLEANUP_MODEL_ARGS=()")
        heredoc_start = source.index("<<'PROMPT'", selection_start)
        heredoc_end = source.index("\nPROMPT\n", heredoc_start) + len("\nPROMPT")
        invocation = source[selection_start:heredoc_end]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            codex_stub = tmp_path / "codex stub"
            codex_stub.write_text('#!/bin/bash\nprintf \'%s\\0\' "$@"\n', encoding="utf-8")
            codex_stub.chmod(0o755)

            run_dir = tmp_path / "run dir"
            env = dict(os.environ)
            env.update({"CODEX_BIN": str(codex_stub), "RUN_DIR": str(run_dir)})
            env.pop("CODEX_CLEANUP_MODEL", None)
            if value is not None:
                env["CODEX_CLEANUP_MODEL"] = value

            result = subprocess.run(
                ["/bin/bash", "-c", f"set -euo pipefail\n{invocation}"],
                env=env,
                check=True,
                capture_output=True,
            )

        return [arg.decode("utf-8") for arg in result.stdout.rstrip(b"\0").split(b"\0")]

    def expected_args(self, run_dir, *model_args):
        return [
            "--cd",
            run_dir,
            "--sandbox",
            "danger-full-access",
            "--ask-for-approval",
            "never",
            *model_args,
            "exec",
            "-",
        ]

    def test_default_and_empty_inherit_configured_model(self):
        for value in (None, ""):
            with self.subTest(value=value):
                args = self.invocation_args(value)
                self.assertEqual(args, self.expected_args(args[1]))

    def test_explicit_override_is_two_literal_arguments(self):
        model = "$(false); verified test model"
        args = self.invocation_args(model)
        self.assertEqual(args, self.expected_args(args[1], "--model", model))

    def test_no_retired_fallback_in_command(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("gpt-5.4", source)
        self.assertIn('"${CODEX_CLEANUP_MODEL_ARGS[@]:0}"', source)


if __name__ == "__main__":
    unittest.main()
