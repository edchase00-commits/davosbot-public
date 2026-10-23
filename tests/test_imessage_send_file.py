import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from davosbot import imessage, personality


class IMessageSendFileTests(unittest.TestCase):
    def test_group_file_send_matches_chat_id_suffix(self):
        calls = []

        def fake_run(cmd, capture_output, text, timeout):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stderr="")

        with TemporaryDirectory() as tmp:
            staged = Path(tmp) / "staged.png"
            staged.write_bytes(b"png")
            with (
                patch.object(imessage, "_stage_outbound_attachment", return_value=staged),
                patch.object(imessage, "_latest_message_rowid", return_value=100),
                patch.object(imessage, "_verify_file_send", return_value=True),
                patch.object(imessage.subprocess, "run", fake_run),
            ):
                ok = imessage.send_file("abc123", "image.png", is_group=True)

        self.assertTrue(ok)
        script = calls[0][-1]
        self.assertIn('ends with "abc123"', script)
        self.assertNotIn('contains "abc123"', script)
        self.assertIn(str(staged).replace("\\", "\\\\"), script)

    def test_stage_outbound_attachment_copies_into_messages_cache(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source image.png"
            source.write_bytes(b"image-bytes")

            with patch.object(imessage.Path, "home", return_value=root):
                staged = imessage._stage_outbound_attachment(str(source))

            self.assertTrue(staged.exists())
            self.assertEqual(staged.read_bytes(), b"image-bytes")
            self.assertIn("Library", staged.parts)
            self.assertIn("Messages", staged.parts)
            self.assertIn("Attachments", staged.parts)
            self.assertIn("DavosBotSendCache", staged.parts)
            self.assertEqual(staged.name, "source_image.png")

    def test_send_file_returns_false_when_db_verification_fails(self):
        calls = []

        def fake_run(cmd, capture_output, text, timeout):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stderr="")

        with TemporaryDirectory() as tmp:
            staged = Path(tmp) / "staged.png"
            staged.write_bytes(b"png")
            with (
                patch.object(imessage, "_stage_outbound_attachment", return_value=staged),
                patch.object(imessage, "_latest_message_rowid", return_value=100),
                patch.object(imessage, "_verify_file_send", return_value=False),
                patch.object(imessage.subprocess, "run", fake_run),
            ):
                ok = imessage.send_file("abc123", "image.png", is_group=False)

        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)

    def test_send_message_preserves_full_emoji_pack(self):
        scripts = []

        def fake_recovery(script, **_kwargs):
            scripts.append(script)
            return True

        text = "Decatur behavior emojis:\n" + personality.DECATUR_BEHAVIOR_EMOJIS
        with (
            patch.object(imessage, "_latest_message_rowid", return_value=100),
            patch.object(imessage, "_run_applescript_with_recovery", fake_recovery),
        ):
            ok = imessage.send_message("+15550000001", text)

        self.assertTrue(ok)
        self.assertEqual(1, len(scripts))
        self.assertIn(personality.DECATUR_BEHAVIOR_EMOJIS, scripts[0])

    def test_send_message_relaunches_messages_and_retries_after_timeout(self):
        calls = []

        def fake_run(cmd, capture_output, text, timeout):
            calls.append(cmd)
            if len(calls) == 1:
                raise imessage.subprocess.TimeoutExpired(cmd, timeout)
            return SimpleNamespace(returncode=0, stderr="")

        with (
            patch.object(imessage.subprocess, "run", fake_run),
            patch.object(imessage, "_hard_relaunch_messages", return_value=True) as relaunch,
        ):
            ok = imessage.send_message("+15550000001", "hello", recovery_mode="inline")

        self.assertTrue(ok)
        self.assertEqual(2, len(calls))
        relaunch.assert_called_once()

    def test_send_message_default_timeout_schedules_background_recovery(self):
        calls = []

        def fake_run(cmd, capture_output, text, timeout):
            calls.append((cmd, timeout))
            raise imessage.subprocess.TimeoutExpired(cmd, timeout)

        with (
            patch.object(imessage.subprocess, "run", fake_run),
            patch.object(imessage, "_latest_message_rowid", return_value=100),
            patch.object(imessage, "_verify_message_send", return_value=False),
            patch.object(imessage, "_schedule_applescript_recovery_retry", return_value=True) as schedule,
            patch.object(imessage, "_hard_relaunch_messages", return_value=True) as relaunch,
        ):
            ok = imessage.send_message("+15550000001", "hello")

        self.assertFalse(ok)
        self.assertEqual(1, len(calls))
        self.assertEqual(imessage._APPLESCRIPT_MESSAGE_TIMEOUT_SECONDS, calls[0][1])
        schedule.assert_called_once()
        relaunch.assert_not_called()

    def test_send_message_timeout_returns_success_if_db_verifies_send(self):
        calls = []

        def fake_run(cmd, capture_output, text, timeout):
            calls.append((cmd, timeout))
            raise imessage.subprocess.TimeoutExpired(cmd, timeout)

        with (
            patch.object(imessage.subprocess, "run", fake_run),
            patch.object(imessage, "_latest_message_rowid", return_value=100),
            patch.object(imessage, "_verify_message_send", return_value=True) as verify,
            patch.object(imessage, "_hard_relaunch_messages", return_value=True) as relaunch,
        ):
            ok = imessage.send_message("+15550000001", "hello")

        self.assertTrue(ok)
        self.assertEqual(1, len(calls))
        self.assertEqual(imessage._APPLESCRIPT_MESSAGE_TIMEOUT_SECONDS, calls[0][1])
        verify.assert_called_once_with(
            100,
            "+15550000001",
            is_group=False,
            timeout_seconds=imessage._MESSAGE_SEND_VERIFY_TIMEOUT_SECONDS,
        )
        relaunch.assert_not_called()

    def test_send_message_relaunches_messages_and_retries_after_bridge_error(self):
        calls = []
        responses = [
            SimpleNamespace(
                returncode=1,
                stderr="execution error: Messages got an error: AppleEvent timed out. (-1712)",
            ),
            SimpleNamespace(returncode=0, stderr=""),
        ]

        def fake_run(cmd, capture_output, text, timeout):
            calls.append(cmd)
            return responses[len(calls) - 1]

        with (
            patch.object(imessage.subprocess, "run", fake_run),
            patch.object(imessage, "_hard_relaunch_messages", return_value=True) as relaunch,
        ):
            ok = imessage.send_message("+15550000001", "hello", recovery_mode="inline")

        self.assertTrue(ok)
        self.assertEqual(2, len(calls))
        relaunch.assert_called_once()

    def test_send_message_relaunches_messages_after_connection_invalid_error(self):
        calls = []
        responses = [
            SimpleNamespace(
                returncode=1,
                stderr="Connection Invalid error for service com.apple.hiservices-xpcservice.",
            ),
            SimpleNamespace(returncode=0, stderr=""),
        ]

        def fake_run(cmd, capture_output, text, timeout):
            calls.append(cmd)
            return responses[len(calls) - 1]

        with (
            patch.object(imessage.subprocess, "run", fake_run),
            patch.object(imessage, "_hard_relaunch_messages", return_value=True) as relaunch,
        ):
            ok = imessage.send_message("+15550000001", "hello", recovery_mode="inline")

        self.assertTrue(ok)
        self.assertEqual(2, len(calls))
        relaunch.assert_called_once()

    def test_send_message_does_not_relaunch_for_regular_applescript_error(self):
        calls = []

        def fake_run(cmd, capture_output, text, timeout):
            calls.append(cmd)
            return SimpleNamespace(returncode=1, stderr='no chat with id ending in "abc123"')

        with (
            patch.object(imessage.subprocess, "run", fake_run),
            patch.object(imessage, "_hard_relaunch_messages", return_value=True) as relaunch,
        ):
            ok = imessage.send_message("abc123", "hello", is_group=True)

        self.assertFalse(ok)
        self.assertEqual(1, len(calls))
        relaunch.assert_not_called()

    def test_run_osascript_paces_consecutive_sends(self):
        calls = []
        sleeps = []
        clock = [100.0]

        def fake_monotonic():
            return clock[0]

        def fake_sleep(seconds):
            sleeps.append(round(seconds, 3))
            clock[0] += seconds

        def fake_run(cmd, capture_output, text, timeout):
            calls.append((cmd, timeout, round(clock[0], 3)))
            clock[0] += 0.05
            return SimpleNamespace(returncode=0, stderr="")

        with (
            patch.object(imessage, "_APPLESCRIPT_MIN_SEND_INTERVAL_SECONDS", 0.25),
            patch.object(imessage, "_last_osascript_started_at", 0.0),
            patch.object(imessage.time, "monotonic", fake_monotonic),
            patch.object(imessage.time, "sleep", fake_sleep),
            patch.object(imessage.subprocess, "run", fake_run),
        ):
            first = imessage._run_osascript("send one", 5)
            second = imessage._run_osascript("send two", 5)

        self.assertEqual(0, first.returncode)
        self.assertEqual(0, second.returncode)
        self.assertEqual(2, len(calls))
        self.assertEqual([0.2], sleeps)

    def test_send_file_relaunches_messages_and_retries_after_timeout(self):
        calls = []

        def fake_run(cmd, capture_output, text, timeout):
            calls.append(cmd)
            if len(calls) == 1:
                raise imessage.subprocess.TimeoutExpired(cmd, timeout)
            return SimpleNamespace(returncode=0, stderr="")

        with TemporaryDirectory() as tmp:
            staged = Path(tmp) / "staged.png"
            staged.write_bytes(b"png")
            with (
                patch.object(imessage, "_stage_outbound_attachment", return_value=staged),
                patch.object(imessage, "_latest_message_rowid", return_value=100),
                patch.object(imessage, "_verify_file_send", return_value=True) as verify,
                patch.object(imessage.subprocess, "run", fake_run),
                patch.object(imessage, "_hard_relaunch_messages", return_value=True) as relaunch,
            ):
                ok = imessage.send_file("+15550000001", "image.png", is_group=False)

        self.assertTrue(ok)
        self.assertEqual(2, len(calls))
        relaunch.assert_called_once()
        verify.assert_called_once()

    def test_hard_relaunch_messages_kills_messages_sync_stack(self):
        calls = []

        def fake_run(cmd, capture_output, text, timeout):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stderr="")

        with (
            patch.object(imessage.subprocess, "run", fake_run),
            patch.object(imessage.time, "sleep", lambda _seconds: None),
            patch.object(imessage.time, "time", return_value=1000.0),
        ):
            imessage._last_messages_restart_at = 0.0
            ok = imessage._hard_relaunch_messages("test")

        self.assertTrue(ok)
        self.assertEqual(
            [
                ["killall", "Messages"],
                ["pkill", "-f", "Messages Assistant Extension"],
                ["pkill", "-f", "MessagesBlastDoorService"],
                ["pkill", "-f", "IMDPersistenceAgent"],
                ["pkill", "-f", "IMDMessageServicesAgent"],
                ["pkill", "-f", "imagent"],
                ["pkill", "-f", "identityservicesd"],
                ["open", "-a", "Messages"],
            ],
            calls,
        )


if __name__ == "__main__":
    unittest.main()
