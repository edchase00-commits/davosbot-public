import types
import unittest
from unittest.mock import patch

from davosbot import commands


class StatusCommandTests(unittest.TestCase):
    def test_status_reports_pm2_failure_as_degraded(self):
        failed = types.SimpleNamespace(returncode=1, stdout="", stderr="pm2 boom")

        with (
            patch.object(commands, "check_action_permission", return_value=None),
            patch.object(commands.subprocess, "run", return_value=failed),
            patch.object(commands, "get_status", return_value="session ok"),
        ):
            reply = commands._cmd_status("owner")

        self.assertIn("DEGRADED: pm2 show failed: pm2 boom", reply)
        self.assertIn("DB Session:\nsession ok", reply)

    def test_status_still_includes_session_when_pm2_raises(self):
        with (
            patch.object(commands, "check_action_permission", return_value=None),
            patch.object(commands.subprocess, "run", side_effect=TimeoutError("slow pm2")),
            patch.object(commands, "get_status", return_value="session ok"),
        ):
            reply = commands._cmd_status("owner")

        self.assertIn("(pm2 error: slow pm2)", reply)
        self.assertIn("DB Session:\nsession ok", reply)

    def test_api_status_is_capability_only_no_secret_values(self):
        with (
            patch.object(commands, "check_action_permission", return_value=None),
            patch.object(commands, "GEMINI_API_KEY", "gemini-secret-value"),
            patch.object(commands, "TAVILY_API_KEY", "tavily-secret-value"),
            patch.object(commands, "OPENAI_API_KEY", "openai-secret-value"),
            patch.object(commands, "OWNER_ALERT_WEBHOOK_URL", "https://secret.example/hook"),
        ):
            reply = commands._cmd_api_status("owner")

        self.assertIn("API/tool status", reply)
        self.assertIn("ESPN sports", reply)
        self.assertIn("Image scan", reply)
        self.assertNotIn("gemini-secret-value", reply)
        self.assertNotIn("tavily-secret-value", reply)
        self.assertNotIn("openai-secret-value", reply)
        self.assertNotIn("secret.example", reply)


if __name__ == "__main__":
    unittest.main()
