import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


autodeploy_log = _load_script("check_autodeploy_log", ROOT / "scripts" / "check_autodeploy_log.py")
wait_for_mini = _load_script("wait_for_mini_deploy", ROOT / "scripts" / "wait_for_mini_deploy.py")


class DevLoopHelperTests(unittest.TestCase):
    def test_autodeploy_log_check_ignores_old_errors_before_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "autodeploy-err.log"
            log.write_text(
                "\n".join(
                    [
                        "2026-06-01 18:00:00 Traceback (most recent call last):",
                        "2026-06-01 18:01:00 deployed: deployed abc1234",
                        "2026-06-01 18:01:01 up_to_date: already on latest commit",
                    ]
                ),
                encoding="utf-8",
            )

            result = autodeploy_log.check_logs_for_sha("abc1234", (log,))

        self.assertTrue(result.ok, result.detail)
        self.assertIn("no error markers", result.detail)

    def test_autodeploy_log_check_flags_errors_after_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "autodeploy-err.log"
            log.write_text(
                "\n".join(
                    [
                        "2026-06-01 18:01:00 deployed: deployed abc1234",
                        "2026-06-01 18:01:01 KeyboardInterrupt",
                    ]
                ),
                encoding="utf-8",
            )

            result = autodeploy_log.check_logs_for_sha("abc1234", (log,))

        self.assertFalse(result.ok)
        self.assertIn("KeyboardInterrupt", "\n".join(result.error_lines))

    def test_wait_for_head_stops_when_mini_reaches_target(self):
        seen = iter(["1111111", "abc1234"])
        with patch.object(wait_for_mini, "mini_head", side_effect=lambda *_args: next(seen)), \
             patch.object(wait_for_mini, "print", create=True), \
             patch.object(wait_for_mini.time, "sleep") as sleep:
            ok = wait_for_mini.wait_for_head("macmini", "/tmp/repo", "abc1234", timeout=60, interval=1)

        self.assertTrue(ok)
        sleep.assert_called_once_with(1)

    def test_wait_for_pm2_online_allows_restart_delay(self):
        wanted = {
            "davosbot": "online",
            "davosbot-autodeploy": "waiting restart",
            "davosbot-comfyui": "online",
            "davosbot-local-image-worker": "online",
        }
        settled = dict(wanted, **{"davosbot-autodeploy": "online"})
        with patch.object(wait_for_mini, "mini_pm2_statuses", side_effect=[wanted, settled]), \
             patch.object(wait_for_mini, "print", create=True), \
             patch.object(wait_for_mini.time, "sleep") as sleep:
            ok = wait_for_mini.wait_for_pm2_online("macmini", "/tmp/repo", timeout=60, interval=5)

        self.assertTrue(ok)
        sleep.assert_called_once_with(5)

    def test_public_publish_configures_temp_repo_line_endings(self):
        script_path = ROOT / "scripts" / "publish_public_snapshot.ps1"
        if not script_path.exists():
            self.skipTest("private publish script is not included in public snapshots")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("git config core.autocrlf false", script)
        self.assertIn("git config core.safecrlf false", script)
        self.assertIn("DAVOSBOT_SUPPRESS_CONFIG_WARNINGS", script)
        self.assertIn("PUBLIC_SNAPSHOT_TOKEN", script)
        self.assertIn("x-access-token", script)
        self.assertIn("DavosBot Public Snapshot", script)

    def test_public_export_uses_deterministic_text_writes(self):
        script_path = ROOT / "scripts" / "export_public_snapshot.ps1"
        if not script_path.exists():
            self.skipTest("private export script is not included in public snapshots")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("function Set-PublicTextContent", script)
        self.assertIn("[System.Text.UTF8Encoding]::new($false)", script)

    def test_dev_commit_requires_intentional_staging(self):
        script = (ROOT / "scripts" / "dev_commit.ps1").read_text(encoding="utf-8")

        self.assertIn("Pass -All or one or more -Path values", script)
        self.assertIn("git push $Remote $Branch", script)


if __name__ == "__main__":
    unittest.main()
