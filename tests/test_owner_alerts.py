import unittest
from unittest.mock import Mock, patch

from davosbot import alerts
from davosbot import commands
class OwnerAlertTests(unittest.TestCase):
    def test_no_webhook_is_fail_closed_without_posting(self):
        with patch.object(alerts, "OWNER_ALERT_WEBHOOK_URL", ""), patch.object(alerts.requests, "post") as post:
            self.assertFalse(alerts.send_owner_alert("test", "hello"))
        post.assert_not_called()

    def test_webhook_payload_is_redacted(self):
        response = Mock(status_code=204)
        with (
            patch.object(alerts, "OWNER_ALERT_WEBHOOK_URL", "https://example.test/hook"),
            patch.object(alerts, "OWNER_ALERT_WEBHOOK_TIMEOUT", 3),
            patch("davosbot.permissions.ADMIN_PASSWORD", "supersecret"),
            patch.object(alerts.requests, "post", return_value=response) as post,
        ):
            self.assertTrue(
                alerts.send_owner_alert(
                    "main_loop_error",
                    "password: supersecret token=abc123",
                    {"detail": "AIza" + "A" * 24},
                )
            )

        payload = post.call_args.kwargs["json"]
        self.assertEqual("davosbot", payload["source"])
        self.assertNotIn("supersecret", str(payload))
        self.assertNotIn("abc123", str(payload))
        self.assertIn("[redacted", str(payload))

    def test_discord_webhook_uses_content_payload(self):
        response = Mock(status_code=204)
        with (
            patch.object(alerts, "OWNER_ALERT_WEBHOOK_URL", "https://discord.com/api/webhooks/1/secret"),
            patch.object(alerts.requests, "post", return_value=response) as post,
        ):
            self.assertTrue(alerts.send_owner_alert("deploy_failed", "token=abc123", {"sha": "abc"}))

        kwargs = post.call_args.kwargs
        self.assertIn("content", kwargs["json"])
        self.assertNotIn("abc123", kwargs["json"]["content"])
        self.assertNotIn("source", kwargs["json"])

    def test_slack_webhook_uses_text_payload(self):
        response = Mock(status_code=200)
        with (
            patch.object(alerts, "OWNER_ALERT_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/C"),
            patch.object(alerts.requests, "post", return_value=response) as post,
        ):
            self.assertTrue(alerts.send_owner_alert("budget_warning", "hello", {"cost": "1.23"}))

        self.assertIn("text", post.call_args.kwargs["json"])
        self.assertIn("budget_warning", post.call_args.kwargs["json"]["text"])

    def test_ntfy_webhook_uses_text_body(self):
        response = Mock(status_code=200)
        with (
            patch.object(alerts, "OWNER_ALERT_WEBHOOK_URL", "https://ntfy.sh/private-topic"),
            patch.object(alerts.requests, "post", return_value=response) as post,
        ):
            self.assertTrue(alerts.send_owner_alert("runtime_warning", "hello", {"service": "pm2"}))

        kwargs = post.call_args.kwargs
        self.assertIn("data", kwargs)
        self.assertIn("Title", kwargs["headers"])
        self.assertNotIn("json", kwargs)

    def test_http_error_returns_false(self):
        response = Mock(status_code=500)
        with (
            patch.object(alerts, "OWNER_ALERT_WEBHOOK_URL", "https://example.test/hook"),
            patch.object(alerts.requests, "post", return_value=response),
            patch.object(alerts.logger, "warning"),
        ):
            self.assertFalse(alerts.send_owner_alert("test", "hello"))

    def test_owner_alert_status_never_prints_url(self):
        with (
            patch.object(commands, "check_action_permission", return_value=None),
            patch("davosbot.config.OWNER_ALERT_WEBHOOK_URL", "https://secret.example.test/hook"),
        ):
            reply = commands._cmd_owner_alert("alert status", "owner")

        self.assertIn("configured", reply)
        self.assertNotIn("secret.example", reply)

    def test_owner_alert_test_uses_send_owner_alert(self):
        with (
            patch.object(commands, "check_action_permission", return_value=None),
            patch("davosbot.config.OWNER_ALERT_WEBHOOK_URL", "https://example.test/hook"),
            patch("davosbot.alerts.send_owner_alert", return_value=True) as send_alert,
        ):
            reply = commands._cmd_owner_alert("alert test", "owner")

        self.assertEqual("Owner alert webhook test sent.", reply)
        send_alert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
