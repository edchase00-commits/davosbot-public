import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from davosbot import fantasy_access


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, _limit):
        return json.dumps(self.payload).encode("utf-8")


class FantasyAccessClientTests(unittest.TestCase):
    def test_signed_request_uses_private_key_without_exposing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "fantasy.pem"
            key_path.write_text("private key fixture", encoding="utf-8")
            completed = MagicMock(returncode=0, stdout=b"signed-bytes")

            with (
                patch.object(
                    fantasy_access,
                    "FANTASY_DASHBOARD_URL",
                    "https://fantasy.example.test",
                ),
                patch.object(
                    fantasy_access, "FANTASY_ACCESS_PRIVATE_KEY_PATH", key_path
                ),
                patch.object(fantasy_access.time, "time", return_value=1234567890),
                patch.object(
                    fantasy_access.secrets,
                    "token_hex",
                    return_value="a" * 32,
                ),
                patch.object(
                    fantasy_access.subprocess, "run", return_value=completed
                ) as run,
                patch.object(
                    fantasy_access,
                    "urlopen",
                    return_value=_Response({"ok": True, "member": None}),
                ) as urlopen,
            ):
                result = fantasy_access.get_access_status("+15550000002")

        self.assertTrue(result["ok"])
        signing_input = run.call_args.kwargs["input"].decode("utf-8")
        self.assertIn("\nPOST\n/api/access/control\n", signing_input)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            base64.b64encode(b"signed-bytes").decode("ascii"),
            request.headers["X-davos-signature"],
        )
        self.assertEqual("DavosBot/1.0", request.headers["User-agent"])
        self.assertNotIn("private key fixture", signing_input)

    def test_request_rejects_invalid_email_before_network(self):
        with patch.object(fantasy_access, "_call_control") as call:
            with self.assertRaises(fantasy_access.FantasyAccessError):
                fantasy_access.request_access(
                    "+15550000002", "not-an-email", "group-chat-guid"
                )
        call.assert_not_called()

    def test_missing_private_key_fails_closed(self):
        missing = Path("Z:/definitely-missing/fantasy.pem")
        with self.assertRaisesRegex(
            fantasy_access.FantasyAccessError, "not configured"
        ):
            fantasy_access._sign("canonical", missing)

    def test_read_only_request_retries_transient_hosting_failures(self):
        hosting_error = HTTPError(
            "https://fantasy.example.test/api/access/control",
            500,
            "Internal Server Error",
            {},
            io.BytesIO(b"<html>temporary hosting failure</html>"),
        )
        with (
            patch.object(
                fantasy_access,
                "FANTASY_DASHBOARD_URL",
                "https://fantasy.example.test",
            ),
            patch.object(fantasy_access, "_sign", return_value="signed"),
            patch.object(
                fantasy_access,
                "urlopen",
                side_effect=[
                    hosting_error,
                    URLError("connection reset"),
                    _Response({"ok": True, "members": []}),
                ],
            ) as urlopen,
            patch.object(fantasy_access.time, "sleep") as sleep,
        ):
            result = fantasy_access.list_access(pending_only=True)

        self.assertTrue(result["ok"])
        self.assertEqual(3, urlopen.call_count)
        self.assertEqual(
            [0.35, 0.9],
            [sleep_call.args[0] for sleep_call in sleep.call_args_list],
        )

    def test_mutating_request_does_not_retry_ambiguous_network_failure(self):
        with (
            patch.object(
                fantasy_access,
                "FANTASY_DASHBOARD_URL",
                "https://fantasy.example.test",
            ),
            patch.object(fantasy_access, "_sign", return_value="signed"),
            patch.object(
                fantasy_access,
                "urlopen",
                side_effect=URLError("connection reset"),
            ) as urlopen,
            patch.object(fantasy_access.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(
                fantasy_access.FantasyAccessError,
                "temporarily unavailable",
            ):
                fantasy_access.grant_access(7, "viewer")

        self.assertEqual(1, urlopen.call_count)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
