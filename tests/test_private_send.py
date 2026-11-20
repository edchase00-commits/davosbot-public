import unittest
from unittest.mock import patch

from davosbot import brain
from davosbot import config
from davosbot import permissions
from davosbot import tools
OWNER = "+15550000001"
ADMIN = "+15550000002"
FRIEND = "+15550000003"


class PrivateSendGateTests(unittest.TestCase):
    def setUp(self):
        tools._pending_private_sends.clear()
        self.sent = []
        self.logs = []
        self.stored_facts = []

        self.patchers = [
            patch.object(config, "ADMIN_PASSWORD", "swordfish"),
            patch.object(permissions, "ADMIN_PASSWORD", "swordfish"),
            patch.object(permissions, "is_owner", lambda sender: sender == OWNER),
            patch.object(permissions, "is_admin", lambda sender: sender in {OWNER, ADMIN}),
            patch.object(
                brain,
                "resolve_contact",
                lambda name: {
                    "cole": "+13369701212",
                }.get((name or "").strip().lower()),
            ),
            patch.object(
                brain,
                "store_user_fact",
                lambda key, value, source="": self.stored_facts.append((key, value, source)),
            ),
            patch.object(tools, "_send_private_imessage", self._send_private_imessage),
            patch.object(tools, "_log_send_imessage_call", self._log_send_imessage_call),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        tools._pending_private_sends.clear()

    def _send_private_imessage(self, destination, message):
        self.sent.append((destination, message))
        return True

    def _log_send_imessage_call(
        self,
        recipient,
        message,
        scheduled_time_utc,
        sender,
        resolution_path="",
        event_type="send_imessage_call",
        label="",
        extra=None,
    ):
        self.logs.append(
            {
                "recipient": recipient,
                "message": message,
                "scheduled_time_utc": scheduled_time_utc,
                "sender": sender,
                "resolution_path": resolution_path,
                "event_type": event_type,
                "label": label,
                "extra": extra or {},
            }
        )

    def test_private_request_prompts_without_sending(self):
        reply = tools.handle_private_send_request(
            OWNER,
            "@davos msg cole 1on1 penis",
            originating_chat_id="group-chat-guid",
        )

        self.assertIn("Confirm 1-on-1 message to cole", reply)
        self.assertIn("***-***-1212", reply)
        self.assertIn("DM me the admin password", reply)
        self.assertEqual([], self.sent)
        self.assertEqual("private_send_confirmation_requested", self.logs[-1]["event_type"])

    def test_wrong_password_denies_and_clears_pending_send(self):
        tools.handle_private_send_request(ADMIN, "dm cole private hello")

        reply = tools.handle_private_send_confirmation(ADMIN, "wrong-password")

        self.assertIn("Denied", reply)
        self.assertEqual([], self.sent)
        self.assertIsNone(tools.handle_private_send_confirmation(ADMIN, "swordfish"))
        self.assertEqual("private_send_denied", self.logs[-1]["event_type"])
        self.assertEqual("bad_password", self.logs[-1]["extra"]["reason"])

    def test_correct_password_sends_after_confirmation(self):
        tools.handle_private_send_request(ADMIN, "dm cole private hello")

        reply = tools.handle_private_send_confirmation(ADMIN, "password: swordfish")

        self.assertEqual([("+13369701212", "hello")], self.sent)
        self.assertIn("Sent 1-on-1 message to cole", reply)
        self.assertEqual("private_send_sent", self.logs[-1]["event_type"])

    def test_missing_contact_requires_phone_then_password(self):
        reply = tools.handle_private_send_request(OWNER, "msg chapman 1on1 hello")
        self.assertIn("I don't have chapman's number", reply)
        self.assertEqual([], self.sent)

        reply = tools.handle_private_send_confirmation(OWNER, "he's in here +1 (336) 970-9999")
        self.assertIn("Confirm 1-on-1 message to chapman", reply)
        self.assertIn("***-***-9999", reply)
        self.assertEqual([], self.sent)

        reply = tools.handle_private_send_confirmation(OWNER, "pw: swordfish")
        self.assertEqual([("+13369709999", "hello")], self.sent)
        self.assertIn("Sent 1-on-1 message to chapman", reply)
        self.assertEqual(
            [("contact:chapman", "+13369709999", "private_send_confirmation")],
            self.stored_facts,
        )

    def test_group_password_reply_never_sends(self):
        tools.handle_private_send_request(
            OWNER,
            "@davos msg cole 1on1 hello",
            originating_chat_id="group-chat-guid",
        )

        reply = tools.handle_private_send_confirmation(OWNER, "swordfish", allow_password=False)

        self.assertIn("Not sending from a group password reply", reply)
        self.assertEqual([], self.sent)

        dm_reply = tools.handle_private_send_confirmation(OWNER, "swordfish", allow_password=True)
        self.assertIn("Sent 1-on-1 message to cole", dm_reply)
        self.assertEqual([("+13369701212", "hello")], self.sent)

    def test_llm_send_imessage_tool_is_owner_only(self):
        denied = tools.execute_tool(
            "send_imessage",
            {"recipient": "cole", "message": "hello"},
            sender=FRIEND,
            originating_chat_id=FRIEND,
        )

        self.assertIn("Permission denied", denied)
        self.assertEqual([], self.sent)

    def test_private_message_body_is_redacted_from_logs(self):
        raw = "@davos msg chapman 1on1 the secret phrase"

        redacted = tools.redact_private_send_text_for_log(raw)
        safe_args = tools._safe_tool_args_for_log(
            "send_imessage",
            {"recipient": "+13369701212", "message": "the secret phrase"},
        )

        self.assertNotIn("the secret phrase", redacted)
        self.assertIn("message_hash=", redacted)
        self.assertNotIn("the secret phrase", str(safe_args))
        self.assertNotIn("message", safe_args)
        self.assertEqual(17, safe_args["message_len"])
        self.assertEqual("***-***-1212", safe_args["recipient_masked"])

    def test_formatted_phone_still_requires_password_before_delivery(self):
        reply = tools.handle_private_send_request(ADMIN, "text +1 (555) 000-0004 1234 is the code")
        self.assertIn("Confirm 1-on-1", reply)
        self.assertEqual([], self.sent)
        pending = tools._get_pending_private_send(ADMIN)
        self.assertEqual("+15550000004", pending["destination"])
        self.assertEqual("1234 is the code", pending["message"])

        tools.handle_private_send_confirmation(ADMIN, "password: swordfish")
        self.assertEqual([("+15550000004", "1234 is the code")], self.sent)

    def test_formatted_phone_does_not_expand_sender_permissions(self):
        reply = tools.handle_private_send_request(FRIEND, "text +1 (555) 000-0004 hello")
        self.assertIn("Permission denied", reply)
        self.assertEqual([], self.sent)
        self.assertIsNone(tools._get_pending_private_send(FRIEND))

    def test_multiple_numbers_are_rejected_without_pending_send(self):
        for message in (
            "text +15550000004 +15550000005 hello",
            "text +1 (555) 000-0004 and +1 (555) 000-0005 hello",
            "text +15550000004, +15550000005 hello",
            "text +15550000004 +15550000005: hello",
            'text "+15550000004 +15550000005" hello',
            "text Cole +15550000004 +15550000005 hello",
            "text Cole 5550000004 +15550000005 hello",
        ):
            with self.subTest(message=message):
                reply = tools.handle_private_send_request(OWNER, message)
                self.assertIn("one complete phone number", reply)
                self.assertIsNone(tools._get_pending_private_send(OWNER))
                self.assertEqual([], self.sent)

    def test_numeric_alias_message_preserves_contact_and_confirmation(self):
        body = "1234567 is the code"
        reply = tools.handle_private_send_request(OWNER, f"text Cole {body}")
        self.assertIn("Confirm 1-on-1", reply)
        pending = tools._get_pending_private_send(OWNER)
        self.assertEqual("+13369701212", pending["destination"])
        self.assertEqual(body, pending["message"])
        self.assertEqual([], self.sent)
        self.assertEqual([], self.stored_facts)
        tools.handle_private_send_confirmation(OWNER, "password: swordfish")
        self.assertEqual([("+13369701212", body)], self.sent)
        self.assertEqual([], self.stored_facts)

    def test_explicit_malformed_alias_phone_does_not_create_pending_send(self):
        for raw in ("+1 hello", "(555) 000 hello", "+15550000004oops hello"):
            with self.subTest(raw=raw):
                reply = tools.handle_private_send_request(OWNER, f"text Cole {raw}")
                self.assertIn("one complete phone number", reply)
                self.assertIsNone(tools._get_pending_private_send(OWNER))
                self.assertEqual([], self.sent)
                self.assertEqual([], self.stored_facts)


class PrivatePhoneParsingTests(unittest.TestCase):
    def test_numeric_message_body_after_alias_stays_message_text(self):
        for body in ("1234567 is the code", "12345678 is the code", "123456789 is the code", "1234-5678 is the code", "2026-09-05 meet at noon"):
            with self.subTest(body=body):
                parsed = tools.parse_private_send_command(f"text Cole {body}")
                self.assertEqual("Cole", parsed["recipient"])
                self.assertEqual(body, parsed["message"])
                self.assertFalse(parsed.get("store_alias"))

    def test_us_phone_formats_have_complete_recipient_and_body(self):
        for phone in ("+15550000004", "+1 (555) 000-0004", "(555) 000-0004", "555.000.0004", "1 555 000 0004"):
            with self.subTest(phone=phone):
                parsed = tools.parse_private_send_command(f"text {phone} 1234 is the code; meet at 7")
                self.assertEqual("+15550000004", parsed["recipient"])
                self.assertEqual("1234 is the code; meet at 7", parsed["message"])

    def test_explicit_boundary_preserves_phone_numbers_and_markers_in_body(self):
        for message in (
            "text +1 (555) 000-0004: +15550000005 is their number",
            'text "+1 (555) 000-0004" +15550000005 is their number',
        ):
            with self.subTest(message=message):
                parsed = tools.parse_private_send_command(message)
                self.assertEqual("+15550000004", parsed["recipient"])
                self.assertEqual("+15550000005 is their number", parsed["message"])
        parsed = tools.parse_private_send_command("text +15550000004: that is private")
        self.assertEqual("that is private", parsed["message"])

    def test_existing_marker_and_alias_shapes_remain_supported(self):
        for marker in ("1on1", "1:1", "one on one", "private", "saying", "-"):
            with self.subTest(marker=marker):
                parsed = tools.parse_private_send_command(f"text +1 (555) 000-0004 {marker} hello")
                self.assertEqual("+15550000004", parsed["recipient"])
                self.assertEqual("hello", parsed["message"])
        for message, target in (
            ("msg Cole 1on1 hello", "Cole"),
            ('msg "Cole Chase" hello', "Cole Chase"),
            ('msg "7-Eleven" hello', "7-Eleven"),
            ("msg 7-Eleven hello", "7-Eleven"),
            ("msg 1800Flowers hello", "1800Flowers"),
            ("msg Cole Chase: hello", "Cole Chase"),
            ("msg friend@example.com hello", "friend@example.com"),
            ("msg 123@example.com hello", "123@example.com"),
        ):
            with self.subTest(message=message):
                parsed = tools.parse_private_send_command(message)
                self.assertEqual(target, parsed["recipient"])
                self.assertEqual("hello", parsed["message"])

    def test_alias_with_formatted_phone_keeps_contact_storage_metadata(self):
        for phone in ("+1 (555) 000-0004", "(555) 000-0004"):
            with self.subTest(phone=phone):
                parsed = tools.parse_private_send_command(f"msg Cole Chase {phone} 1234 is the code")
                self.assertEqual("+15550000004", parsed["recipient"])
                self.assertEqual("Cole Chase", parsed["label"])
                self.assertEqual("1234 is the code", parsed["message"])
                self.assertTrue(parsed["store_alias"])

    def test_supported_international_numbers_have_explicit_boundaries(self):
        for message in ("text +442079460958 hello", "text +44 (20) 7946 0958: hello"):
            with self.subTest(message=message):
                parsed = tools.parse_private_send_command(message)
                self.assertEqual("+442079460958", parsed["recipient"])
                self.assertEqual("hello", parsed["message"])

    def test_invalid_phone_or_ambiguous_international_boundary_asks_for_clarity(self):
        for message in (
            "text +1 hello", "text 12345 hello", "text +15550000004123 hello", "text +15550000004oops hello",
            "text +44 20 7946 0958 1234 is the code", "text +4930123456 hello",
        ):
            with self.subTest(message=message):
                parsed = tools.parse_private_send_command(message)
                self.assertIn("error", parsed)


if __name__ == "__main__":
    unittest.main()
